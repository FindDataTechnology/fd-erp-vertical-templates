"""期间关账工具在 MCP 形态下的接线（2026-11-15 断链的回归钉）。

**断链形态**：内核侧 5 个期间工具（`close_period` / `reopen_period` /
`period_status` / `period_report` / `list_periods`，`add-period-close` 已归档、
`tools.py` 33-37）一直都在，api 的同进程形态也全都实现了；唯独模板层的
`McpKernelClient` **一个都没转发**。于是生产（`ERP_KERNEL_TRANSPORT=mcp`）上
`GET /fi/periods` 直接回：

    501 {"code":"NOT_WIRED","message":"当前传输形态未开期间关账工具（list_periods）"}

而同进程形态（单测、demo e2e）是好的 ⇒ 单测全绿、能力缺失只在真机现形。
这个断链能挂很久还有第二层原因：`NOT_WIRED` 是「如实报未接线」的**合法语义**，
于是「没实现」在日志里长得像「设计如此」，没人会把它当缺陷。

本文件钉三件事：

1. **五个方法都在**（少一个就是那一档能力继续 NOT_WIRED）；
2. **tool 名与内核工具名逐字相同**——不是 Python 方法名归一化后的猜测；
3. **参数形状**：`note=None` 必须**不出现**在报文里（`_call` 统一丢 None），
   给了 `note` 才出现。真实事故证明过「显式传 None」会让内核 pydantic 判
   `unexpected_keyword_argument` → 500。

并且断言**客户端不复制内核的判定**（顺序关账 / 只重开最近一期都在内核
`periods` 模块里）：客户端收到的内核错误原样抛出，不吞、不改码。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.kernel_client import KernelClient, McpKernelClient

PERIOD_TOOLS = {
    "close_period": ["period", "actor", "note"],
    "reopen_period": ["period", "actor", "reason"],
    "period_status": ["period"],
    "period_report": ["period"],
    "list_periods": [],
}


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict]] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        return _FakeResult({"ok": True, "data": [], "rows_returned": 0})


@pytest.fixture()
def fake_client(monkeypatch):
    import fastmcp

    _FakeClient.instances.clear()
    monkeypatch.setattr(fastmcp, "Client", _FakeClient)
    return _FakeClient


def _client() -> McpKernelClient:
    return McpKernelClient("http://kernel/mcp", "acme")


def test_all_period_tools_are_forwarded() -> None:
    """五个方法缺一个，生产和同进程形态就再分叉一次。"""
    missing = [n for n in PERIOD_TOOLS if not hasattr(McpKernelClient, n)]
    assert missing == [], f"McpKernelClient 缺期间关账方法：{missing}（会回 NOT_WIRED）"

    # 反命题在这一侧：协议（编排面）**不该**声明它们——模板编排层不代任何一方关账，
    # 这是 `shared/kernel-client-contract.json` 的 `intentionally_absent`。
    # 2026-11-15 订正：初版把这五方法同时加进了协议，契约闸当场判红（CLIENT_DRIFT），
    # 因为闸按「协议 + 传输类并集」比对。分层之后协议 = 编排面纪律、传输类 = 1:1 适配器。
    declared = [n for n in PERIOD_TOOLS if hasattr(KernelClient, n)]
    assert declared == [], (
        f"KernelClient 协议不该声明期间关账方法：{declared}（编排层不关账；"
        "它们只属于传输面 McpKernelClient）")


@pytest.mark.parametrize("method,kwargs", [
    ("close_period", {"period": "2026-11", "actor": "cfo-001", "note": "月结"}),
    ("reopen_period", {"period": "2026-11", "actor": "cfo-001", "reason": "补记"}),
    ("period_status", {"period": "2026-11"}),
    ("period_report", {"period": "2026-11"}),
    ("list_periods", {}),
])
def test_tool_name_and_args_are_exact(fake_client, method, kwargs) -> None:
    """tool 名 = 方法名逐字；参数名按内核签名；tenant 由客户端注入。"""
    out = getattr(_client(), method)(**kwargs)
    assert out == {"ok": True, "data": [], "rows_returned": 0}

    built = fake_client.instances[-1]
    tool, args = built.calls[-1]
    assert tool == method, f"tool 名被改写：{tool} != {method}（内核侧工具名就是方法名）"
    assert args["tenant_id"] == "acme", "tenant 必须由客户端注入（消费方不重复传）"
    for key, value in kwargs.items():
        assert args[key] == value


def test_none_note_is_omitted_not_sent_as_null(fake_client) -> None:
    """`note=None` ⇒ 报文里**没有** note 键；给值才出现（成对断言，缺一反命题无意义）。"""
    _client().close_period("2026-11", "cfo-001")
    tool, args = fake_client.instances[-1].calls[-1]
    assert tool == "close_period"
    assert "note" not in args, (
        f"none 被显式传下去了：{args} —— 在役内核会判 unexpected_keyword_argument → 500")

    _client().close_period("2026-11", "cfo-001", note="月结")
    _, args2 = fake_client.instances[-1].calls[-1]
    assert args2["note"] == "月结"


def test_client_does_not_judge_period_rules(monkeypatch) -> None:
    """客户端只转发：内核的拒绝原样抛出，不吞也不改码（顺序关账只在核心里判）。"""

    class _Rejecting:
        def __init__(self, url, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, tool, args):
            raise RuntimeError("PERIOD_CLOSED: 2026-10 未关，不能先关 2026-11")

    import fastmcp

    monkeypatch.setattr(fastmcp, "Client", _Rejecting)
    with pytest.raises(Exception) as excinfo:
        _client().close_period("2026-11", "cfo-001")
    assert "PERIOD_CLOSED" in repr(excinfo.value), (
        "内核的拒绝被吞或换码了——调用方会把它读成「关账成功」")

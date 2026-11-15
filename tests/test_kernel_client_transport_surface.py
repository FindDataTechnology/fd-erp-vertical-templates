"""传输面补齐的七个工具（2026-11-15 `decide_event` 生产 500 的回归钉）。

**事故**：api 的 SE 审核端点 `POST /se/invoice-requests/{id}/decide` 在生产上
每单必 500。链路上的真因不在 api 的转发代码（它调了），而在**传输适配器没有
这个方法**：`KernelProxy.decide_event` → `self._inner.decide_event` 而
`self._inner` 是 `McpKernelClient`，它当时没有 `decide_event` ⇒ AttributeError。
`faces.py` 只兜 `ApiError`，所以连业务错误都不是，是裸 500。

形态学：内核 37 工具、传输类 30 个 ⇒ 缺 7（`decide_event` +
`submit_posting_rule` / `approve_posting_rule` / `list_posting_rules` /
`list_posting_gaps` / `bill_period_snapshot` / `list_billing_rules`）。
其中只有 `decide_event` 有真调用面（其余是潜在缺口，但同一个缺口类）。

本文件钉四件事：

1. 七个方法都在 `McpKernelClient` 上；
2. tool 名 = 方法名逐字，参数名与内核签名逐字对齐（多一个少一个都会被打回：
   内核 pydantic 对未知参数判 `unexpected_keyword_argument`）；
3. `decide_event` **不收** `lane`/`actor`——内核工具内部硬编码 draft/human，
   这里收下再丢就是「静默改写审计留痕」；
4. 反命题：`KernelClient` 协议**不声明**它们（编排层不代拍板/不登记规则）。

全量 37/37 的判据不在这里，而在契约闸
（`fd-erp/tools/check_kernel_client_contract.py` 的 `transport` 块，唯一出处是
`fd-erp-ledger-mcp/tool-surface.json`）——本仓不复制第二份工具清单。
"""
from __future__ import annotations

import inspect

import pytest

from fd_erp_vertical_templates.kernel_client import KernelClient, McpKernelClient

#: 方法名 -> (位置参数名, 关键字参数默认值)。与 `fd-erp-ledger-mcp/tools.py` 同源抄录；
#: 内核改签名时这里**必须**跟着改（工具名漂移由契约闸兜，参数名漂移由本用例兜）。
TRANSPORT_ADDITIONS: dict[str, tuple[list[str], dict]] = {
    "decide_event": (["stable_id", "decision"], {"note": ""}),
    "submit_posting_rule": (["event_type", "lines"], {"source_ref": None}),
    "approve_posting_rule": (["event_type", "version", "approved_by"], {}),
    "list_posting_rules": ([], {"event_type": None}),
    "list_posting_gaps": ([], {}),
    "bill_period_snapshot": (["period", "snapshots"], {"source_ref": None}),
    "list_billing_rules": ([], {"status": None, "offset": 0, "limit": 100}),
}


class _RecordingMcp(McpKernelClient):
    """只换 `_call`：验「发哪个工具、带什么参数」，不碰 MCP 传输。"""

    def __init__(self):
        super().__init__("http://kernel.invalid/mcp", "tpl-supply")
        self.sent: list[tuple[str, dict]] = []

    def _call(self, tool, args):
        self.sent.append((tool, dict(args)))
        return {"ok": True, "data": [], "rows_returned": 0}


def test_all_seven_are_on_the_transport() -> None:
    missing = [n for n in TRANSPORT_ADDITIONS if not hasattr(McpKernelClient, n)]
    assert missing == [], (
        f"传输面缺方法：{missing} —— api 的 MCP 内层就是这个类，"
        "缺一个就是对应端点在生产的 AttributeError/500")


def test_protocol_still_does_not_declare_them() -> None:
    """编排面纪律：模板层不代拍板、不登记/审批规则。

    分层不是文字游戏——协议上有，编排代码就能调；协议上没有，类型检查与
    代码审查都还挡得住。传输面覆盖 ≠ 编排面获得能力。
    """
    leaked = [n for n in TRANSPORT_ADDITIONS if hasattr(KernelClient, n)]
    assert leaked == [], (
        f"KernelClient 协议不该声明：{leaked}（这些只属于传输面）")


@pytest.mark.parametrize("method", sorted(TRANSPORT_ADDITIONS))
def test_tool_name_equals_method_name(method) -> None:
    client = _RecordingMcp()
    params, kwargs = TRANSPORT_ADDITIONS[method]
    positional = {
        "decide_event": ("EV-1", "accepted"),
        "submit_posting_rule": ("goods_received", [{"account": "1405", "side": "debit"}]),
        "approve_posting_rule": ("goods_received", 2, "cfo-001"),
        "list_posting_rules": (),
        "list_posting_gaps": (),
        "bill_period_snapshot": ("2026-11", [{"sku": "A", "qty": 1}]),
        "list_billing_rules": (),
    }[method]
    getattr(client, method)(*positional, **kwargs)

    tool, args = client.sent[-1]
    assert tool == method, f"tool 名被改写：{tool} != {method}"


@pytest.mark.parametrize("method", sorted(TRANSPORT_ADDITIONS))
def test_parameter_names_match_kernel_signature(method) -> None:
    """参数名逐字对齐内核工具；租户由客户端统一注入。"""
    params, kwargs = TRANSPORT_ADDITIONS[method]
    sig = inspect.signature(getattr(McpKernelClient, method))
    declared = [p for p in sig.parameters if p != "self"]
    assert declared == params + list(kwargs), (
        f"{method} 的参数名与内核签名不一致：客户端 {declared}，"
        f"内核 {(params + list(kwargs))}。内核按名字收参，差一个字母就是 500")


def test_decide_event_sends_only_three_keys() -> None:
    """`decide_event` 报文里不得出现 `lane`/`actor`（内核工具不收，硬编码 draft/human）。"""
    client = _RecordingMcp()
    client.decide_event("EV-1", "accepted", note="审核通过")
    tool, args = client.sent[-1]
    assert tool == "decide_event"
    assert args == {"stable_id": "EV-1", "decision": "accepted",
                    "note": "审核通过"}, args
    assert "lane" not in args and "actor" not in args, (
        "lane/actor 被塞进报文：在役内核会判 unexpected_keyword_argument → 500；"
        "就算某天内核收下了，那也是绕开 api 的显式拒绝在静默改写审计留痕")


def test_decide_event_none_note_is_omitted(monkeypatch) -> None:
    """成对反命题：不给 note ⇒ 报文里没有 note 键。

    必须走**真 `_call`**（只换 fastmcp.Client）：None-strip 就在 `_call` 里，
    用 `_RecordingMcp` 替掉它等于把被测逻辑一起替掉了，断言恒真。
    """
    import fastmcp

    sent: list[tuple[str, dict]] = []

    class _Client:
        def __init__(self, url, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, tool, args):
            sent.append((tool, dict(args)))
            return type("R", (), {"data": {"ok": True}})()

    monkeypatch.setattr(fastmcp, "Client", _Client)
    McpKernelClient("http://kernel/mcp", "acme").decide_event(
        "EV-1", "rejected", note=None)

    tool, args = sent[-1]
    assert tool == "decide_event"
    assert "note" not in args, (
        f"note=None 被显式传下去了：{args} —— 在役内核会判 unexpected_keyword_argument")


def test_query_events_forwards_producer() -> None:
    """`query_events` 的内核签名有 `producer`，传输面 1:1 必须能带出去。"""
    client = _RecordingMcp()
    client.query_events(event_type="goods_received", producer="erp", limit=10)
    tool, args = client.sent[-1]
    assert tool == "query_events"
    assert args["producer"] == "erp"


def test_submit_proposal_accepts_and_forwards_booked_on() -> None:
    """`booked_on`（业务记账日）必须在传输面且真发出去（2026-11-15 第三个同族缺陷）。

    **红证（修前，本机直接调用，不需要起服务）**：

        TypeError: McpKernelClient.submit_proposal() got an unexpected keyword argument 'booked_on'

    而模板编排有 14 处调用点在传它（`opening` / `closing` / `bad_debt` / `cn_filing`×2 /
    `cn_invoice` / `cn_tax`×4 / `consolidation` / `fx_revaluation` / `manufacturing`×2），
    生产 api 跑的是 `ERP_KERNEL_TRANSPORT=mcp` ⇒ 这些模板的提交路径在生产形态下必炸，
    而同进程形态一直正常（`InProcessKernelClient` 早就加了这个参数）。与
    `ingest_event.provenance` 完全同族：**内部函数收、工具面不收**。
    """
    sig = inspect.signature(McpKernelClient.submit_proposal)
    assert "booked_on" in sig.parameters, (
        "传输面没有 booked_on ⇒ 14 处模板调用点在 MCP 形态下 TypeError")

    client = _RecordingMcp()
    client.submit_proposal("exp:2026-11",
                           [{"account_code": "5200", "direction": "debit",
                             "amount": "1"}],
                           source_ref="opening", kind="journal",
                           booked_on="2026-11-15")
    tool, args = client.sent[-1]
    assert tool == "submit_proposal"
    assert args["booked_on"] == "2026-11-15", f"收下却没发出去（静默不生效）：{args}"


def test_submit_proposal_omits_booked_on_when_not_given() -> None:
    """不给就不发：`None` 由内核按事件时刻推，别替调用方编一个业务日期。"""
    client = _RecordingMcp()
    client.submit_proposal("exp:2026-12", [])
    _, args = client.sent[-1]
    assert args.get("booked_on") is None

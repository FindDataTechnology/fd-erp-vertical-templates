"""内核 bearer 门的接线（2026-11-15 生产事故的回归钉）。

事故形态：内核 `fd-erp_ledger_mcp/auth.py` 在 `MCP_BEARER_TOKEN` 非空时给数据面
`/mcp` 挂 bearer 门（`/health`/`/metrics`/`/.well-known` 豁免）。而模板层的
`McpKernelClient` 是 `Client(self._url)`——一个头都不带 ⇒ 每个碰账本的端点 401，
被 api 的统一壳兜成 `500 INTERNAL: MCPError`；与此同时 `/health`、`/pagesets`
（不经内核）照常 200 ⇒ 「服务健康」与「取不到账本数据」同时成立，静默约 38 小时。

这里钉两件**分开**的事，缺一条都会留下缝隙：

1. **接缝**：`McpKernelClient(url, tenant, token=...)` 必须把 `BearerAuth` 带进
   `Client(...)`；不传 token 时**不得**多传 `auth=`（内核没开门的环境行为不变）。
2. **头上真的有值**：`BearerAuth` 产出的请求头必须是 `Authorization: Bearer <t>`。
   只断言「构造时传了 auth 对象」而不看头，等于相信库的实现——那正是这次事故的
   思路（「客户端类名里有 Client，应该会自己发」）。

第三条断言是**反命题**：不带 token 时报文里不含 Authorization——否则上一条在
「无论怎样都带头」的实现下也会绿。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from fd_erp_vertical_templates import kernel_client as kc
from fd_erp_vertical_templates.kernel_client import McpKernelClient

TOKEN = "test-bearer-token-abc123"


class _FakeResult:
    data = {"ok": True}


class _FakeClient:
    """替身：记录构造参数与调用，不碰网络。"""

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
        return _FakeResult()


@pytest.fixture()
def fake_client(monkeypatch):
    """把 `fastmcp.Client` 换成替身；模块内是惰性 `from fastmcp import Client`。"""
    import fastmcp

    _FakeClient.instances.clear()
    monkeypatch.setattr(fastmcp, "Client", _FakeClient)
    return _FakeClient


def test_token_is_passed_as_bearer_auth(fake_client) -> None:
    """接缝：token → `Client(url, auth=BearerAuth(token))`，且调用真的发生。"""
    client = McpKernelClient("http://kernel/mcp", "acme", token=TOKEN)
    assert client._call("query_balance", {"code": "1001"}) == {"ok": True}

    assert len(fake_client.instances) == 1
    built = fake_client.instances[0]
    assert built.url == "http://kernel/mcp"
    assert "auth" in built.kwargs, "带了 token 却没把 auth 传给 fastmcp Client"
    assert isinstance(built.kwargs["auth"], BearerAuthLike)
    # tenant 仍按原契约注入（别让这次改动顺手改掉 agent 的落款口径）
    assert built.calls[0][1]["tenant_id"] == "acme"


def test_without_token_no_auth_kwarg(fake_client) -> None:
    """反命题：不带 token 时不得传 `auth=`——内核没开门的环境要逐字节不变。"""
    client = McpKernelClient("http://kernel/mcp", "acme")
    client._call("query_balance", {"code": "1001"})

    built = fake_client.instances[0]
    assert "auth" not in built.kwargs


def test_blank_token_is_treated_as_absent(fake_client) -> None:
    """空串／纯空格 = 没配（部署里 `ERP_KERNEL_TOKEN=` 的形态），不得把空串当钥匙。"""
    for blank in ("", "   "):
        _FakeClient.instances.clear()
        McpKernelClient("http://kernel/mcp", "acme", token=blank)._call("t", {})
        assert "auth" not in _FakeClient.instances[0].kwargs


def test_bearer_auth_sets_authorization_header() -> None:
    """头上真有值：`Authorization: Bearer <token>`（用库自己的 auth_flow 实测）。

    用最小的 `headers` 容器而不是 httpx/httpx2 的 Request：fastmcp 4 走的是
    `httpx2`，本仓依赖声明的是 `httpx`，两个包名不能混用；而 `auth_flow` 只做
    `request.headers["Authorization"] = …`，容器足够。
    """
    from fastmcp.client import BearerAuth

    class _Req:
        headers: dict[str, str] = {}

    sent = list(BearerAuth(TOKEN).auth_flow(_Req()))
    assert len(sent) == 1
    assert sent[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_lazy_import_keeps_module_import_cheap() -> None:
    """惰性导入纪律：模块顶层不得 import fastmcp（内核包在 sys.path 里才可用）。"""
    src = inspect.getsource(kc)
    head = src.split("class KernelClient", 1)[0]
    assert "import fastmcp" not in head, (
        "模块顶层 import 了 fastmcp——本包在无 fastmcp 的环境（测试/演示）也要能 import")


#: `BearerAuth` 从 fastmcp 取（同一实例类型），单独命名让断言读起来是「它就是那个东西」。
from fastmcp.client import BearerAuth as _BearerAuth  # noqa: E402

BearerAuthLike = _BearerAuth

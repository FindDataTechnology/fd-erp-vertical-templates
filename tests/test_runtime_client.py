"""连接器运行时客户端：错误归因、封套、归一化、零出网旁路。

每条断言都对着「坏的那个本身」：不是「能调用」，而是「调用失败时归因对不对」。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.runtime_client import (
    ActionRef,
    RuntimeClient,
    normalize_record,
    stable_id,
)


def _client(status=200, body=None, *, capture=None, base="http://runtime.test"):
    calls = []

    def transport(method, url, headers, body_in):
        calls.append({"method": method, "url": url, "headers": headers,
                      "body": body_in, "captured": capture})
        if isinstance(status, Exception):
            raise status
        return status, (body if body is not None else {"records": []})

    return RuntimeClient(base, transport=transport), calls


def test_action_ref_rejects_path_injection():
    with pytest.raises(TemplateError) as ei:
        ActionRef("shopify_admin", "../../etc/passwd", "conn-1")
    assert ei.value.code == "INVALID_ARGUMENT"


def test_unconfigured_runtime_reports_not_wired():
    client = RuntimeClient(base_url="", transport=lambda *a: (200, {}))
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("shopify_admin", "list_orders", "c1"))
    assert ei.value.code == "NOT_WIRED"
    assert "ERP_CONNECTOR_BASE_URL" in ei.value.message


def test_call_posts_action_path_with_connection_alias():
    client, calls = _client(body={"records": [{"id": "o1", "amount": "10"}]})
    out = client.call(ActionRef("shopify_admin", "list_orders", "conn-a"),
                      {"since": "2026-11-01"})
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/v1/actions/shopify_admin.list_orders")
    assert calls[0]["body"]["connection"] == "conn-a"
    assert calls[0]["body"]["since"] == "2026-11-01"
    assert out["records"] == [{"id": "o1", "amount": "10"}]


def test_401_is_auth_failed_not_not_wired():
    client, _ = _client(status=401, body={"message": "token revoked"})
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("shopify_admin", "list_orders", "c"))
    assert ei.value.code == "AUTH_FAILED"
    assert "重新授权" in ei.value.message


def test_404_is_action_not_found_and_forbids_fallback():
    client, _ = _client(status=404, body={})
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("shopify_admin", "list_payouts", "c"))
    assert ei.value.code == "ACTION_NOT_FOUND"
    assert "不得以自建通道补齐" in ei.value.message


def test_429_is_rate_limited_and_attributes_quota():
    client, _ = _client(status=429, body={})
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("merchant_erp", "list_orders", "c"))
    assert ei.value.code == "RATE_LIMITED"
    assert "配额" in ei.value.message


def test_transport_exception_is_unreachable():
    client, _ = _client(status=ConnectionError("boom"))
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("shopify_admin", "list_orders", "c"))
    assert ei.value.code == "RUNTIME_UNREACHABLE"


def test_unknown_envelope_is_bad_response():
    client, _ = _client(status=200, body={"unexpected": {}})
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("shopify_admin", "list_orders", "c"))
    assert ei.value.code == "BAD_RESPONSE"


def test_error_message_carries_no_credentials():
    """错误是最容易漏凭据的地方：只带 provider/action/状态码。"""
    client, _ = _client(status=500, body={"message": "x" * 10})
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("shopify_admin", "list_orders", "secret-conn"))
    text = ei.value.message + str(ei.value.detail)
    assert "secret-conn" not in text
    assert "authorization" not in text.lower()


def test_probe_returns_action_set_or_empty():
    client, _ = _client(status=200, body={"actions": [{"name": "list_orders"},
                                                     "get_order"]})
    got = client.probe("shopify_admin")
    assert got == {"list_orders", "get_order"}
    dead, _ = _client(status=500, body={})
    assert dead.probe("shopify_admin") == set()


# ---- 归一化 ----


def test_normalize_requires_stable_id():
    with pytest.raises(TemplateError) as ei:
        normalize_record({"amount": "1"}, store="s", domain="d",
                         source_ref="ref")
    assert ei.value.code == "BAD_RESPONSE"


def test_normalize_shape_is_shared_across_platforms():
    rec = normalize_record({"id": "o1", "amount": 10, "currency": "USD",
                            "status": "paid", "occurred_at": "2026-11-06",
                            "link": "o1", "fees": {"commission": "1"}},
                           store="shop", domain="shopify:orders",
                           source_ref="shopify:order")
    assert rec["stable_id"] == stable_id("shop", "shopify:orders", "o1")
    assert rec["amount"] == "10" and rec["currency"] == "USD"
    assert rec["raw"]["fees"] == {"commission": "1"}
    assert rec["source_ref"] == "shopify:order:o1"


def test_normalize_is_idempotent_keyed():
    a = normalize_record({"id": "x"}, store="s", domain="d", source_ref="r")
    b = normalize_record({"id": "x", "amount": "999"}, store="s", domain="d",
                         source_ref="r")
    assert a["stable_id"] == b["stable_id"]


# ---- 静态门：模板层不得自建出网 ----


def test_connector_package_has_no_vendor_urls():
    pkg = Path(__file__).resolve().parent.parent / "fd_erp_vertical_templates"
    offenders: list[str] = []
    for path in pkg.rglob("*.py"):
        if path.name == "runtime_client.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.startswith(("http://", "https://")):
                offenders.append(f"{path.relative_to(pkg)}:{node.value}")
    assert offenders == [], f"模板层出现硬地址（应经运行时动作）：{offenders}"


def test_connector_modules_do_not_import_http_clients():
    pkg = Path(__file__).resolve().parent.parent / "fd_erp_vertical_templates" / "connectors"
    offenders: list[str] = []
    for path in pkg.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                if names & {"httpx", "requests", "aiohttp", "urllib3"}:
                    offenders.append(str(path.name))
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in {"httpx", "requests", "aiohttp", "urllib3"}:
                    offenders.append(str(path.name))
    assert offenders == [], f"连接器自行出网：{sorted(set(offenders))}"


def test_normalize_is_idempotent_for_already_shaped_records():
    """已经是 recon 形状的记录（有 stable_id + source_ref）原样返回，不再加工。

    演示/测试会直接喂 recon 记录；再归一化一次会把缺 `id` 的既有记录判成
    BAD_RESPONSE——那是调用方形状问题，不是数据问题。
    """
    shaped = {"stable_id": "s-1", "amount": "100", "currency": "USD",
              "status": "paid", "occurred_at": "2026-11-06",
              "source_ref": "shopify:demo:order:1", "raw": {"order_id": "1"}}
    out = normalize_record(shaped, store="demo", domain="shopify:orders",
                           source_ref="shopify:demo:order")
    assert out == shaped
    assert out is not shaped          # 返回副本，调用方改它不影响输入

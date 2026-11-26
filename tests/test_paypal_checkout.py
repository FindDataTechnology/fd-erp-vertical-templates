"""PayPal 在线收款车道测试（change establish-ai-native-ecommerce-stack 0.13）。

判据（任务验证原文）：
1. 无真实商户号（运行时未配置）时以 `NOT_WIRED` 如实失败，**不回落任何自建直连**；
2. 接线形态 = 经连接器运行时执行 `paypal.create_order` / `paypal.capture_order`
   （钱进商家授权账户，模板层不持凭据不持 URL）；
3. 静态门认识 PayPal 主机（绕过运行时直连会被 payment_direct_call_gate 判红）。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.connectors import checkout
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.runtime_client import ActionRef, RuntimeClient


def _client_with(records_by_action: dict[str, list[dict]]) -> RuntimeClient:
    """注入 transport 的假运行时：记录每次动作引用与载荷，按动作回放订单。"""
    seen: list[tuple[str, dict]] = []

    def transport(method, url, headers, body):
        path = url.rsplit("/v1/actions/", 1)[-1]
        seen.append((path, body))
        action = path.split(".")[-1]
        return 200, {"data": {"order": records_by_action[action]}}

    client = RuntimeClient(base_url="http://runtime.test", transport=transport)
    client.seen = seen  # type: ignore[attr-defined]
    return client


CREATE_ORDER = {
    "id": "PAYPAL-ORDER-1", "status": "CREATED",
    "links": [{"rel": "self", "href": "https://api-m.paypal.com/v2/checkout/orders/PAYPAL-ORDER-1"},
              {"rel": "approve", "href": "https://www.paypal.com/checkoutnow?token=PAYPAL-ORDER-1"}],
}
CAPTURED_ORDER = {
    "id": "PAYPAL-ORDER-1", "status": "COMPLETED",
    "purchase_units": [{"reference_id": "SO-ABC1234567",
                        "payments": {"captures": [{"id": "CAP-1",
                                                   "amount": {"currency_code": "USD",
                                                              "value": "99.00"}}]}}],
}


def test_create_without_runtime_is_not_wired(monkeypatch):
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    lane = checkout.PayPalCheckout()
    with pytest.raises(TemplateError) as ei:
        lane.create_order(order_ref="SO-ABC1234567", amount="99.00", currency="USD")
    assert ei.value.code == "NOT_WIRED"
    assert "自建直连" in ei.value.message


def test_capture_without_runtime_is_not_wired(monkeypatch):
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    with pytest.raises(TemplateError) as ei:
        checkout.PayPalCheckout().capture_order(paypal_order_id="PAYPAL-ORDER-1")
    assert ei.value.code == "NOT_WIRED"


def test_create_goes_through_runtime_with_catalog_shape():
    """create_order 经运行时执行 paypal.create_order，载荷即目录 inputSchema 形状。"""
    client = _client_with({"create_order": CREATE_ORDER,
                           "capture_order": CAPTURED_ORDER})
    lane = checkout.PayPalCheckout(client=client)
    out = lane.create_order(order_ref="SO-ABC1234567", amount="99.00", currency="USD")
    assert out["paypal_order_id"] == "PAYPAL-ORDER-1"
    assert out["status"] == "CREATED"
    assert out["approve_url"].startswith("https://www.paypal.com/")
    (path, body), = client.seen  # type: ignore[attr-defined]  # 只发生这一次调用
    assert path == "paypal.create_order"
    assert body["connection"] == "paypal-default"
    assert body["intent"] == "CAPTURE"
    assert body["purchaseUnits"] == [{"referenceId": "SO-ABC1234567",
                                      "amount": {"currencyCode": "USD",
                                                 "value": "99.00"}}]


def test_capture_returns_completed_and_capture_id():
    client = _client_with({"create_order": CREATE_ORDER,
                           "capture_order": CAPTURED_ORDER})
    out = checkout.PayPalCheckout(client=client).capture_order(
        paypal_order_id="PAYPAL-ORDER-1")
    assert out["completed"] is True
    assert out["capture_id"] == "CAP-1"
    assert out["amount"] == "99.00" and out["currency"] == "USD"
    (path, body), = client.seen  # type: ignore[attr-defined]
    assert path == "paypal.capture_order"
    assert body["orderId"] == "PAYPAL-ORDER-1"


def test_describe_honestly_declares_no_live():
    surface = checkout.PayPalCheckout().describe()
    assert surface["live_declared"] is False   # 无真实商户号：未实测不宣称
    assert surface["transport"] == "connector-runtime"


def test_payment_gate_knows_paypal_hosts(tmp_path):
    """把 PayPal API 主机直连写进模板层任意文件 → 门判红并点名。"""
    root = tmp_path
    (root / "rogue.py").write_text(
        'import httpx\n'
        'def pay():\n'
        '    return httpx.post("https://api-m.paypal.com/v2/checkout/orders")\n',
        encoding="utf-8")
    import subprocess
    import sys
    from pathlib import Path
    gate = Path(__file__).resolve().parents[1] / "scripts" / "payment_direct_call_gate.py"
    r = subprocess.run([sys.executable, str(gate), "--root", str(root)],
                       capture_output=True, text=True)
    assert r.returncode == 1, r.stdout
    assert "api-m.paypal.com" in r.stdout

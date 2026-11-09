"""运行时封套与 Shopify 结算面（A 任务 2.3）。

实测封套（`open-connector-mt/src/server/api/runtime-api.ts:217`）：

    {"success": true, "message": "OK", "data": <动作输出>, "meta": {...}}

动作输出里的集合键**每个动作自己起名**（`orders` / `payouts` / `inventoryItems`），
所以判据是「能取到集合」而不是「键名叫 records」——硬编码键名会在加动作时
静默返回空列表，那正是「读不到当成不存在」的变体。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.connectors import shopify
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.runtime_client import ActionRef, RuntimeClient


def _client(body):
    return RuntimeClient("http://runtime.test",
                         transport=lambda *a: (200, body))


def test_extracts_orders_from_runtime_envelope():
    client = _client({"success": True, "message": "OK",
                      "data": {"orders": [{"id": "o1", "amount": "10"}],
                               "pageInfo": {"hasNextPage": False}},
                      "meta": {}})
    out = client.call(ActionRef("shopify_admin", "list_orders", "c"))
    assert out["records"] == [{"id": "o1", "amount": "10"}]


def test_extracts_payouts_from_runtime_envelope():
    client = _client({"success": True, "message": "OK",
                      "data": {"payouts": [{"id": "p1", "amount": "98"}],
                               "payoutsAccountAvailable": True},
                      "meta": {}})
    out = client.call(ActionRef("shopify_admin", "list_payouts", "c"))
    assert out["records"] == [{"id": "p1", "amount": "98"}]
    assert out["data"]["payoutsAccountAvailable"] is True


def test_single_record_output_is_wrapped():
    client = _client({"data": {"order": {"id": "o1", "amount": "10"}}})
    out = client.call(ActionRef("shopify_admin", "get_order", "c"))
    assert out["records"] == [{"id": "o1", "amount": "10"}]


def test_envelope_without_collection_is_bad_response():
    client = _client({"data": {"pageInfo": {"hasNextPage": False}}})
    with pytest.raises(TemplateError) as ei:
        client.call(ActionRef("shopify_admin", "list_orders", "c"))
    assert ei.value.code == "BAD_RESPONSE"
    assert "没有记录集合" in ei.value.message


# ---- Shopify 结算面：三种「没有 payout」必须分开 ----


def test_shop_not_on_shopify_payments_is_not_zero_payouts():
    """商户没启用 Shopify Payments ≠ 没有结算流水。"""
    def executor(action, payload):
        return []   # 空记录
    reader = shopify.ShopifyStoreReader("s", executor=executor)
    reader._call = lambda action, payload=None, **kw: {
        "records": [], "data": {"payoutsAccountAvailable": False}}
    with pytest.raises(TemplateError) as ei:
        reader.read_payments()
    assert ei.value.code == "NOT_WIRED"
    assert "不是「没有收款」" in ei.value.message


def test_payouts_mapped_with_fee_dimensions():
    def executor(action, payload):
        return [{"id": "p1", "amount": "98", "currency": "USD",
                 "status": "paid", "occurred_at": "2026-11-06",
                 "adjustmentsFeeAmount": "2", "chargesFeeAmount": "1"}]
    reader = shopify.ShopifyStoreReader("s", executor=executor)
    reader._call = lambda action, payload=None, **kw: {
        "records": executor(action, payload),
        "data": {"payoutsAccountAvailable": True}}
    rows = reader.read_payments()
    assert rows[0]["raw"]["fees"] == {"commission": "2", "payment_fee": "1"}


def test_payout_action_missing_surfaces_as_action_not_found(monkeypatch):
    """目录没有 payout 动作 → 透出 ACTION_NOT_FOUND，不降级成空列表。"""
    def transport(method, url, headers, body):
        return 404, {"message": "action not found"}
    reader = shopify.ShopifyStoreReader(
        "s", client=RuntimeClient("http://runtime.test", transport=transport))
    with pytest.raises(TemplateError) as ei:
        reader.read_payments()
    assert ei.value.code == "ACTION_NOT_FOUND"

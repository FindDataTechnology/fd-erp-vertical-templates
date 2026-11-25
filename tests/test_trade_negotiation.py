"""协商车道（询价 → 报价 → 接受）的纪律测试。

这组测试守的是 spec `trade-negotiation` 的硬要求，不是实现细节：
  1. 询价单**不带价格**（带价格即拒，不是静默忽略）；
  2. 报价**必须有时效**、总量承诺 ≥ 起订量；
  3. 过期 / 低于起订量 / 剩余不足 一律拒，且**不产生订单**；
  4. 接受报价的成交价取自报价，订单带 `quote_ref`；
  5. 协商全程**零资金动作**。
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.negotiation import TradeDesk
from fd_erp_vertical_templates.orchestration import OrderOrchestrator
from fd_erp_vertical_templates.pricing import PricingRuleBook

DAY = date.today().isoformat()
TENANT = "acme"
CHANNEL = "retail-online"


class _NoKernel:
    """预付单车道不碰内核（不占信用、不产生分录），这里只是凑签名。"""

    def call(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("预付单不该调内核")


@pytest.fixture()
def shop(tmp_path):
    data = tmp_path / "data"
    md = MasterData(TENANT, data)
    md.confirm_request(
        md.create_entity("product", {
            "code": "SKU-1", "name": "矿泉水", "category": "饮品",
            "units": [{"unit": "件", "kind": "base", "rate": 1}],
            "base_price": "128.00"})["request_id"], decided_by="owner")
    pricing = PricingRuleBook(TENANT, data, md)
    rid = pricing.add_rule({
        "layer": "retail", "price": "128.00", "currency": "CNY",
        "rate_source": "retail/price-list/acme/v1",
        "valid_from": "2020-01-01", "valid_to": "2099-12-31",
        "scope": {"channel_id": CHANNEL, "product_code": "SKU-1"},
    }, requested_by="owner")["request_id"]
    pricing.confirm_rule(rid, decided_by="owner")
    orders = OrderOrchestrator(TENANT, data, _NoKernel(), md, pricing)
    desk = TradeDesk(TENANT, data)
    return {"data": data, "md": md, "pricing": pricing,
            "orders": orders, "desk": desk}


def future(days=7) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def make_demand(desk, **kw):
    body = dict(lines=[{"productId": "SKU-1", "qty": 10}], needed_by=future(),
                ship_to={"name": "张三", "phone": "13800000000"})
    body.update(kw)
    return desk.create_demand(**body)


def make_quote(desk, demand_ref, **kw):
    body = dict(demand_ref=demand_ref, unit_price="96.00", currency="CNY",
                valid_until=future(), lead_time_days=3, moq=1,
                qty_offered=10, product_code="SKU-1")
    body.update(kw)
    return desk.create_quote(**body)


def accept(shop, quote_ref, qty=10, **kw):
    body = dict(quote_ref=quote_ref, qty=qty, orchestrator=shop["orders"],
                day=DAY, channel=CHANNEL,
                ship_to={"name": "张三", "phone": "13800000000"})
    body.update(kw)
    return shop["desk"].accept_quote(**body)


# ---- 询价 ----

def test_demand_has_no_price(shop):
    out = make_demand(shop["desk"])
    assert out["amount"] is None
    ev = [e for e in shop["desk"]._events() if e.get("record") == "trade_demand"][0]
    assert "unit_price" not in json.dumps(ev["payload"])


def test_demand_with_expected_price_rejected(shop):
    with pytest.raises(TemplateError) as ei:
        make_demand(shop["desk"], lines=[{"productId": "SKU-1", "qty": 1,
                                          "expected_price": "1"}])
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "expected_price" in str(ei.value.detail)


def test_open_demand_cap(shop):
    for _ in range(20):
        make_demand(shop["desk"])
    with pytest.raises(TemplateError) as ei:
        make_demand(shop["desk"])
    assert "上限" in ei.value.message


# ---- 报价 ----

def test_quote_requires_valid_until(shop):
    d = make_demand(shop["desk"])
    with pytest.raises(TemplateError) as ei:
        shop["desk"].create_quote(demand_ref=d["demand_ref"], unit_price="96",
                                  valid_until="", lead_time_days=1, moq=1,
                                  qty_offered=10, product_code="SKU-1")
    assert "valid_until" in str(ei.value.detail)


def test_quote_valid_until_must_be_future(shop):
    d = make_demand(shop["desk"])
    with pytest.raises(TemplateError) as ei:
        make_quote(shop["desk"], d["demand_ref"],
                   valid_until=(date.today() - timedelta(days=1)).isoformat())
    assert "未来" in ei.value.message


def test_quote_offered_below_moq_rejected(shop):
    """总量承诺 < 起订量 = 永远无法成交的报价，应在报价时拒。"""
    d = make_demand(shop["desk"])
    with pytest.raises(TemplateError) as ei:
        make_quote(shop["desk"], d["demand_ref"], moq=20, qty_offered=10)
    assert "起订量" in ei.value.message


# ---- 接受 ----

def test_accept_uses_quoted_price_not_list_price(shop):
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"])
    out = accept(shop, q["quote_ref"], qty=10)
    assert out["quote_ref"] == q["quote_ref"]
    assert Decimal(out["amount_total"]) == Decimal("960.00")   # 96 × 10，不是 128 × 10
    ev = [e for e in shop["desk"]._events() if e.get("record") == "order"
          and e.get("ref") == out["order_ref"]][0]
    assert ev["payload"]["quote_ref"] == q["quote_ref"]


def test_expired_quote_cannot_be_accepted(shop, monkeypatch):
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"], valid_until=future(1))
    # 把「现在」推到有效期之后：过期判定必须用接受时刻（设计 D5 Risks）。
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3 * 86400)
    with pytest.raises(TemplateError) as ei:
        accept(shop, q["quote_ref"], qty=10)
    assert "过期" in ei.value.message
    # 负例自证：拒绝必须不产生订单
    assert not [e for e in shop["desk"]._events() if e.get("record") == "order"]


def test_below_moq_rejected(shop):
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"], moq=10, qty_offered=10)
    with pytest.raises(TemplateError) as ei:
        accept(shop, q["quote_ref"], qty=2)
    assert ei.value.detail["moq"] == "10"
    assert not [e for e in shop["desk"]._events() if e.get("record") == "order"]


def test_quote_total_commitment_exhausted(shop):
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"], moq=1, qty_offered=10)
    accept(shop, q["quote_ref"], qty=4)
    accept(shop, q["quote_ref"], qty=6)
    with pytest.raises(TemplateError) as ei:
        accept(shop, q["quote_ref"], qty=1)
    assert "剩余" in ei.value.message


def test_split_acceptance_accumulates(shop):
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"], moq=1, qty_offered=10)
    accept(shop, q["quote_ref"], qty=5)
    out = accept(shop, q["quote_ref"], qty=5)
    assert out["quote_ref"] == q["quote_ref"]
    assert shop["desk"].quote_remaining(q["quote_ref"]) == Decimal(0)


def test_budget_gate_applies_to_quoted_orders(shop):
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"])
    with pytest.raises(TemplateError) as ei:
        accept(shop, q["quote_ref"], qty=10, grant_jti="j1", grant_budget="50")
    assert ei.value.code == "BUDGET_EXHAUSTED"
    assert not [e for e in shop["desk"]._events() if e.get("record") == "order"]


def test_no_payment_or_fund_events(shop):
    """协商全程零资金动作（spec 硬要求）。"""
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"])
    out = accept(shop, q["quote_ref"], qty=10)
    evs = shop["desk"]._events()
    assert not [e for e in evs if e.get("record") == "payment_received"]
    order = [e for e in evs if e.get("record") == "order" and e.get("ref") == out["order_ref"]][0]
    assert order["payload"]["settlement_mode"] == "prepaid"
    assert out["payment_state"] == "awaiting_payment"


def test_quote_without_product_cannot_settle(shop):
    d = make_demand(shop["desk"])
    q = make_quote(shop["desk"], d["demand_ref"], product_code="")
    with pytest.raises(TemplateError) as ei:
        accept(shop, q["quote_ref"], qty=10)
    assert "商品" in ei.value.message

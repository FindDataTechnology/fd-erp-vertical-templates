"""能力矩阵 → 对账降级（change 任务 3.1-3.3）。

三条硬判据：
1. 不可得腿不产出「零差异」（差异数 0 与「没核对」必须可区分）。
2. 结算/库存不可得时不得用订单金额或交易流水推算。
3. 未注入矩阵时行为与改造前逐字等价（降级不是默认行为，是显式接入）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability import load_matrix
from fd_erp_vertical_templates.recon.crossborder import CrossborderRecon

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
MATRIX_PATH = TEMPLATES / "crossborder" / "capability-matrix.json"


def _rec(rid: str, amount, *, day="2026-11-06", currency="CNY",
         status="posted", raw=None, side="L") -> dict:
    return {"stable_id": rid, "amount": str(amount), "currency": currency,
            "status": status, "occurred_at": f"{day}T10:00:00",
            "source_ref": f"{side}:{rid}", "raw": raw or {}}


ORDERS = [_rec("o1", 250, raw={"order_id": "o1", "link": "o1"}),
          _rec("o2", 80, raw={"order_id": "o2", "link": "o2"})]
PAYMENTS = [_rec("p1", 240, side="R",
                 raw={"order_id": "o1", "link": "o1", "fees": {"commission": "15"}})]
INVENTORY = [_rec("i1", 0, raw={"order_id": "o1", "link": "o1"}, status="posted")]


def _matrix_with(platform: str, flow: str, status: str, *, reason="why") -> dict:
    return load_matrix({
        "schema": "ecommerce-capability-matrix/v1",
        "vertical": "crossborder",
        "platforms": [{"key": platform, "flows": {flow: {
            "status": status, "channel": "x.y", "reason": reason,
            "evidence": [], "measured_at": "2026-11-09",
        }}}],
    }, today=date(2026, 11, 10))


def test_without_matrix_behaviour_is_unchanged():
    """未注入矩阵 = 旧行为（回归基线）。"""
    recon = CrossborderRecon("store-1")
    assert recon.recon_shape() is None
    assert recon.capability_missing() == []
    r = recon.reconcile_orders_payments(ORDERS, PAYMENTS)
    assert r["pair"] == "orders-payments"
    assert r["diffs"], "旧行为应当产出差异"
    assert "capability_missing" not in r


def test_missing_settlements_blocks_pair_without_zero_diffs():
    m = _matrix_with("shopify", "settlements", "unmapped",
                     reason="provider 无 payout 动作")
    recon = CrossborderRecon("store-1", matrix=m, platform="shopify")
    r = recon.reconcile_orders_payments(ORDERS, [])
    assert r["diffs"] == []
    assert r["capability_missing"]["flow"] == "settlements"
    assert r["capability_missing"]["status"] == "unmapped"
    assert "不产出零差异" in r["note"]


def test_missing_inventory_blocks_status_pair():
    m = _matrix_with("shopify", "inventory", "unmapped", reason="未接")
    recon = CrossborderRecon("store-1", matrix=m, platform="shopify")
    r = recon.reconcile_orders_inventory(ORDERS, [])
    assert r["matched"] == 0
    assert r["diffs"] == []
    assert r["capability_missing"]["flow"] == "inventory"
    assert "不产出零差异" in r["note"]


def test_fee_report_refuses_to_invent_settlement_totals():
    """结算不可得时不得产出 totals / explained —— 推算就是编数。"""
    m = _matrix_with("shopify", "settlements", "unavailable",
                     reason="上游无结算面")
    recon = CrossborderRecon("store-1", matrix=m, platform="shopify",
                             platform_fee_schedule={"commission": "15/100/x"})
    rep = recon.fee_report(PAYMENTS)
    assert rep["totals"] is None
    assert rep["rows"] == []
    assert rep["fee_explanations"] == []
    assert rep["capability_missing"]["status"] == "unavailable"
    assert "不产出结算额" in rep["note"]


def test_reconcile_reports_shape_and_missing_legs():
    m = load_matrix(MATRIX_PATH, today=date(2026, 11, 10))
    recon = CrossborderRecon("store-1", matrix=m, platform="shopify")
    out = recon.reconcile(orders=ORDERS, payments=PAYMENTS, inventory=INVENTORY)
    assert out["shape"] == 0
    assert out["diff_count"] == 0          # 0 = 什么都没核对
    assert {d["flow"] for d in out["capability_missing"]} == {
        "orders", "settlements", "inventory"}
    assert all(r.get("capability_missing") for r in out["pairs"].values())
    # 每一对都明确说「为什么没核对」，而不是静默空列表
    for pair in out["pairs"].values():
        assert pair.get("note")


def test_two_way_recon_when_only_inventory_missing():
    """订单+结算可得、库存缺 → 两方照跑，库存腿单列归因。"""
    m = load_matrix({
        "schema": "ecommerce-capability-matrix/v1",
        "vertical": "crossborder",
        "platforms": [{"key": "p1", "flows": {
            "orders": {"status": "available", "channel": "a", "reason": "",
                       "measured_at": "2026-11-09",
                       "evidence": [{"kind": "provider_action", "ref": "a"},
                                    {"kind": "runtime_read", "ref": "b"}]},
            "settlements": {"status": "available", "channel": "a", "reason": "",
                            "measured_at": "2026-11-09",
                            "evidence": [{"kind": "provider_action", "ref": "a"},
                                         {"kind": "runtime_read", "ref": "b"}]},
            "inventory": {"status": "unmapped", "channel": "a",
                          "reason": "未接库存面", "evidence": []},
        }}],
    }, today=date(2026, 11, 10))
    recon = CrossborderRecon("store-1", matrix=m, platform="p1")
    out = recon.reconcile(orders=ORDERS, payments=PAYMENTS, inventory=INVENTORY)
    assert out["shape"] == 2
    assert out["pairs"]["orders-payments"].get("capability_missing") is None
    assert out["pairs"]["orders-inventory"]["capability_missing"]["flow"] == "inventory"
    assert out["legs"]["orders-inventory"] == "unmapped"
    assert out["legs"]["orders-payments"] == "available"

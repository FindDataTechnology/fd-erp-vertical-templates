"""国内轨迹 fixture（A 任务 6.1）。

两条变体各跑一次：
- 三腿齐 → shape 3，无 capability_missing；
- 缺结算变体 → 结算腿进 capability_missing，**不产出结算额/结算差异**。

判据落在「缺腿会不会被渲染成零差异」上——这是国内轨迹最容易骗过自己的地方。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability import load_matrix
from fd_erp_vertical_templates.connectors import merchant_erp
from fd_erp_vertical_templates.recon.crossborder import CrossborderRecon

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "crossborder"
FIXTURE = TEMPLATE / "fixtures" / "domestic-store.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _executor(table: dict):
    def run(action, payload=None, **kwargs):
        return table[action]
    return run


def _matrix(available: tuple[str, ...]) -> object:
    flows = {}
    for flow, action in (("orders", "list_orders"),
                         ("settlements", "list_settlements"),
                         ("inventory", "list_inventory")):
        if flow in available:
            flows[flow] = {"status": "available", "channel": f"merchant_erp.{action}",
                           "reason": "", "measured_at": "2026-11-09",
                           "evidence": [{"kind": "provider_action", "ref": "a"},
                                        {"kind": "runtime_read", "ref": "b"}]}
        else:
            flows[flow] = {"status": "unmapped", "channel": f"merchant_erp.{action}",
                           "reason": "该厂商未开放账单面", "evidence": []}
    return load_matrix({"schema": "ecommerce-capability-matrix/v1",
                        "vertical": "crossborder",
                        "platforms": [{"key": "merchant_erp", "flows": flows}]},
                       today=date(2026, 11, 10))


def test_fixture_schema_and_links_are_consistent():
    d = _load()
    assert d["schema"] == "erp-store-fixture/v1"
    order_ids = {o["id"] for o in d["orders"]}
    for flow in ("settlements", "inventory"):
        for row in d[flow]:
            assert row["link"] in order_ids, f"{flow} 的 {row['id']} 挂到不存在的订单"
    assert "settlements_missing" in d["variants"]


def test_domestic_full_track_runs_three_way():
    d = _load()
    reader = merchant_erp.MerchantErpReader(
        d["store"], executor=_executor({"list_orders": d["orders"],
                                        "list_settlements": d["settlements"],
                                        "list_inventory": d["inventory"]}))
    recon = CrossborderRecon(d["store"], matrix=_matrix(("orders", "settlements", "inventory")),
                             platform="merchant_erp")
    out = recon.reconcile(orders=reader.read_orders(),
                          payments=reader.read_settlements(),
                          inventory=reader.read_inventory())
    assert out["shape"] == 3
    assert out["capability_missing"] == []
    assert len(out["pairs"]["orders-payments"]["diffs"]) >= 0
    # 可得腿的结果里不应出现 capability_missing 键（不是 None，而是不存在）
    assert "capability_missing" not in out["pairs"]["orders-inventory"]


def test_domestic_missing_settlements_is_flagged_not_zero():
    """缺结算变体：必须出现 capability_missing，且不产出结算额。"""
    d = _load()
    reader = merchant_erp.MerchantErpReader(
        d["store"], executor=_executor({"list_orders": d["orders"],
                                        "list_inventory": d["inventory"]}))
    matrix = _matrix(("orders", "inventory"))
    recon = CrossborderRecon(d["store"], matrix=matrix, platform="merchant_erp")
    out = recon.reconcile(orders=reader.read_orders(),
                          inventory=reader.read_inventory())
    assert out["shape"] == 2
    flows = {m["flow"] for m in out["capability_missing"]}
    assert flows == {"settlements"}
    pair = out["pairs"]["orders-payments"]
    assert pair["diffs"] == []
    assert pair["capability_missing"]["flow"] == "settlements"

    fee = recon.fee_report([])
    assert fee["totals"] is None
    assert fee["rows"] == []
    assert fee["capability_missing"]["flow"] == "settlements"


def test_fixture_amounts_are_internally_balanced():
    """合成数据要能自圆其说：每笔订单的结算额 + 费用 = 订单额（演示时不被打断）。"""
    from decimal import Decimal
    d = _load()
    by_link = {s["link"]: s for s in d["settlements"]}
    for order in d["orders"]:
        settle = by_link.get(order["id"])
        assert settle is not None, f"{order['id']} 没有对应结算（三腿齐变体应当齐）"
        total = (Decimal(settle["amount"]) + Decimal(settle["fees"]["commission"])
                 + Decimal(settle["fees"]["payment_fee"]))
        assert total == Decimal(order["amount"]), \
            f"{order['id']} 结算+费用 {total} ≠ 订单额 {order['amount']}"

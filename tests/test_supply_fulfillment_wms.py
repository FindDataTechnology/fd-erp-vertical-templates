"""履约编排 / 账实对账 / 阈值派单测试（add-supply-chain-vertical 任务 5.x/7.2）。

- 履约两路径：越库直发（供应商主档直发标识）与仓内多仓分配 + 货缺如实
- 运单轨迹/电子回单/签收回流（只读物流适配器 + 留痕）
- 日终账实对账（真内核分录聚合 vs WMS 快照 → three_way 四类差异）
- 阈值预警 → 处置任务（确认车道派单）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fd_erp_vertical_templates.fulfillment import (
    DemoLogisticsAdapter, FulfillmentOrchestrator)
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.orchestration import OrderOrchestrator
from fd_erp_vertical_templates.pricing import PricingRuleBook
from fd_erp_vertical_templates.wms_sync import (
    DemoWmsAdapter, book_positions_from_entries, recon_book_vs_wms)
from fd_erp_vertical_templates.reports import ThresholdEvaluator, load_report_preset
from fd_erp_vertical_templates.errors import TemplateError


# ---- 履约两路径（合成 WMS/物流，零 DB）----

@pytest.fixture()
def supply_md(tmp_path):
    m = MasterData("tpl-supply", tmp_path / "data")
    s1 = m.confirm_request(m.create_entity("supplier", {
        "code": "S-DS", "name": "直发供应商", "direct_ship": True,
        "direct_ship_products": ["P-DS"]})["request_id"],
        decided_by="owner")["entity_id"]
    s2 = m.confirm_request(m.create_entity("supplier", {
        "code": "S-NO", "name": "普通供应商", "direct_ship": False}
    )["request_id"], decided_by="owner")["entity_id"]
    for code, sid in (("P-DS", s1), ("P-WH", s2), ("P-NONE", s2)):
        m.confirm_request(m.create_entity("product", {
            "code": code, "name": f"商品{code}",
            "units": [{"unit": "件", "kind": "base", "rate": 1}],
            "base_price": "10.00", "supplier_id": sid})["request_id"],
            decided_by="owner")
    return m


def _handoff(tmp_path, m, lines):
    """走订单编排产生真实移交记录（跨模块衔接）。"""
    kernel = _FakeCredit()
    board = OrderOrchestrator("tpl-supply", tmp_path / "data", kernel, m,
                              PricingRuleBook("tpl-supply", tmp_path / "data",
                                              master=m))
    out = board.accept_order(ids_customer(m), lines, "2026-11-07")
    assert out["accepted"] is True
    confirmed = board.confirm_order(out["request_id"], decided_by="owner")
    return confirmed["handoff_ref"], confirmed["order_ref"]


def ids_customer(m):
    return m.find_by_code("customer", "C-CUST")["entity_id"]


class _FakeCredit:
    state = {"limit": "10000", "ar_balance": "0", "reserved": "0",
             "available": "10000"}

    def query_credit(self, customer_id):
        return dict(self.state)

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None):
        return {"stable_id": stable_id, "status": "reserved"}

    def release_credit(self, customer_id, stable_id):
        return {"stable_id": stable_id, "status": "released"}


def test_fulfillment_dropship_and_warehouse_paths(tmp_path, supply_md):
    m = supply_md
    # 客户建档（供订单编排）
    m.confirm_request(m.create_entity("customer", {"code": "C-CUST",
                                                   "name": "客户丙"}
                                      )["request_id"], decided_by="owner")
    wms = DemoWmsAdapter([
        {"product_code": "P-WH", "warehouse": "WH-A", "qty": 6,
         "status": "available"},
        {"product_code": "P-WH", "warehouse": "WH-B", "qty": 4,
         "status": "available"}])
    logistics = DemoLogisticsAdapter(
        tracks={"WB-001": [{"at": "2026-11-08T08:00:00", "state": "picked"},
                           {"at": "2026-11-08T18:00:00", "state": "in_transit"}]},
        pods={"WB-001": {"signed_at": "2026-11-09T10:00:00",
                         "signer": "仓内收货人", "pod_ref": "POD-001"}})
    ff = FulfillmentOrchestrator("tpl-supply", tmp_path / "data", m, wms, logistics)
    handoff_ref, order_ref = _handoff(
        tmp_path, m, [
            {"product_code": "P-DS", "qty": 5, "unit": "件"},   # 直发行
            {"product_code": "P-WH", "qty": 8, "unit": "件"},   # 仓内行（跨两仓）
            {"product_code": "P-NONE", "qty": 3, "unit": "件"}])  # 货缺行
    plan = ff.plan_fulfillment(handoff_ref, order_ref,
                               ids_customer(m), [
                                   {"product_code": "P-DS", "qty": 5, "unit": "件"},
                                   {"product_code": "P-WH", "qty": 8, "unit": "件"},
                                   {"product_code": "P-NONE", "qty": 3, "unit": "件"}])
    by_code = {ln["product_code"]: ln for ln in plan["lines"]}
    # 直发路径：供应商主档直发标识命中 → 直发指令记录（模板层留痕）
    assert by_code["P-DS"]["path"] == "dropship"
    assert by_code["P-DS"]["instruction"] == "dropship-instruction-recorded"
    # 仓内路径：多仓分配（WH-A 6 + WH-B 2），出库指令逐仓留痕
    assert by_code["P-WH"]["path"] == "warehouse"
    assert by_code["P-WH"]["allocations"] == [
        {"warehouse": "WH-A", "qty": "6"}, {"warehouse": "WH-B", "qty": "2"}]
    # 货缺：如实 shortage，不伪造可履约
    assert by_code["P-NONE"]["shortage"] == "3"
    # 指令留痕可回查
    events = ff._events()
    assert sum(1 for e in events
               if e["record"] == "dropship_instruction") == 1
    assert sum(1 for e in events
               if e["record"] == "outbound_instruction") == 2

    # ---- 运单回流：轨迹 + 电子回单 + 全单物流视图 ----
    wb = ff.record_waybill(plan["ref"], "WB-001", "demo-carrier")
    assert wb["waybill_no"] == "WB-001"
    track = ff.track_waybill("WB-001")
    assert [e["state"] for e in track["events"]] == ["picked", "in_transit"]
    with pytest.raises(TemplateError):
        ff.track_waybill("WB-404")  # 只读适配器如实 NOT_FOUND
    pod = ff.record_pod("WB-001")
    assert pod["pod"]["signer"] == "仓内收货人"
    view = ff.fulfillment_view(order_ref)
    p = view["plans"][0]
    assert len(p["dropship_instructions"]) == 1
    assert len(p["outbound_instructions"]) == 2
    assert p["waybills"][0]["track_events"] is not None
    assert p["waybills"][0]["pod"]["pod_ref"] == "POD-001"


def test_fulfillment_requires_real_handoff(tmp_path, supply_md):
    m = supply_md
    wms = DemoWmsAdapter([])
    ff = FulfillmentOrchestrator("tpl-supply", tmp_path / "data", m, wms,
                                 DemoLogisticsAdapter())
    with pytest.raises(TemplateError) as ei:
        ff.plan_fulfillment("FH-NOT-EXIST", "SO-X", "C", [])
    assert "履约移交记录不存在" in ei.value.message


# ---- 账实对账（真内核分录 vs WMS 快照）----

def _post_inventory(kernel, stable_id, code, wh, qty, *, direction="debit"):
    other = "2000" if direction == "debit" else "5000"
    lines = [
        {"account_code": "1200", "direction": direction, "amount": str(qty),
         "currency": "CNY"},
        {"account_code": other, "direction":
            "credit" if direction == "debit" else "debit",
         "amount": str(qty), "currency": "CNY"},
    ]
    kernel.submit_proposal(stable_id=stable_id, lines=lines,
                           source_ref=f"inv:{code}:{wh}")
    kernel.confirm_proposal(stable_id=stable_id)


def test_book_vs_wms_recon_four_diff_kinds(kernel, reset_db):
    # 账面（真内核分录，inv:键）：P001 +10 / P002 +5 / P003 +7 / P004 +4
    _post_inventory(kernel, "inv-t1", "P001", "WH1", 10)
    _post_inventory(kernel, "inv-t2", "P002", "WH1", 5)
    _post_inventory(kernel, "inv-t3", "P003", "WH2", 7)
    _post_inventory(kernel, "inv-t4", "P004", "WH1", 4)
    # 无维度键的手工调整分录（如实进 unparsed）
    kernel.submit_proposal(stable_id="inv-m1", lines=[
        {"account_code": "1200", "direction": "debit", "amount": "2",
         "currency": "CNY"},
        {"account_code": "2000", "direction": "credit", "amount": "2",
         "currency": "CNY"}], source_ref="manual-adjust")
    kernel.confirm_proposal(stable_id="inv-m1")

    wms = DemoWmsAdapter([
        {"product_code": "P001", "warehouse": "WH1", "qty": 10,
         "status": "available"},    # 匹配
        {"product_code": "P002", "warehouse": "WH1", "qty": 3,
         "status": "available"},    # 数量不符（账 5 / 实 3）
        {"product_code": "P004", "warehouse": "WH1", "qty": 4,
         "status": "frozen"},       # 状态不符（账面统一 available 口径）
        {"product_code": "P009", "warehouse": "WH1", "qty": 6,
         "status": "available"}])   # 实有账无
    rep = recon_book_vs_wms(kernel, wms, tenant_id="tpl-e2e",
                            account_code="1200")
    kinds = rep["totals"]["by_kind"]
    assert kinds["missing_in_right"] == 1      # P003 账有实无
    assert kinds["extra_in_right"] == 1        # P009 实有账无
    assert kinds["amount_mismatch"] == 1       # P002 数量不符
    assert kinds["status_mismatch"] == 1       # P004 状态不符
    assert rep["matched_count"] == 1           # P001
    mism = next(d for d in rep["diffs"] if d["kind"] == "amount_mismatch")
    assert mism["left_amount"] == "5" and mism["right_amount"] == "3"
    assert mism["left_raw"]["entry_refs"]      # 双侧引用：账面 txn#line
    assert mism["right_ref"].startswith("wms:")
    status = next(d for d in rep["diffs"] if d["kind"] == "status_mismatch")
    assert "frozen" in status["explanation_hint"]
    # 无维度键分录如实列示（不静默吞掉）
    assert any(u["source_ref"] == "manual-adjust"
               for u in rep["unparsed_entries"])
    assert rep["caliber_note"]


def test_book_positions_from_entries_unit():
    entries = [
        {"txn_id": "t1", "line_no": 1, "source_ref": "inv:P1:W1",
         "direction": "debit", "amount": "10", "posted_at": "2026-11-07T10:00:00"},
        {"txn_id": "t2", "line_no": 1, "source_ref": "inv:P1:W1",
         "direction": "credit", "amount": "3", "posted_at": "2026-11-07T11:00:00"},
        {"txn_id": "t3", "line_no": 1, "source_ref": "other",
         "direction": "debit", "amount": "5", "posted_at": "2026-11-07T11:00:00"},
    ]
    positions, unparsed = book_positions_from_entries(entries)
    assert len(positions) == 1
    assert positions[0]["amount"] == "7"          # 净数量：10 − 3
    assert positions[0]["raw"]["link"] == "P1|W1"
    assert len(unparsed) == 1 and unparsed[0]["txn_id"] == "t3"


# ---- 阈值预警 → 处置任务（确认车道派单）----

REPO = Path(__file__).resolve().parent.parent
PRESET_PATH = REPO / "templates" / "supply-chain" / "report-preset" / "preset.json"


def test_threshold_alerts_and_disposal_lane(tmp_path):
    preset = load_report_preset(PRESET_PATH)
    ev = ThresholdEvaluator("tpl-supply", tmp_path / "disposal")
    # 毛利异常（< 5%）+ 逾期应收（> 0）触发；库存积压未提供值如实不评估
    alerts = ev.evaluate_thresholds(preset, {
        "gross_margin": "3.2",
        "overdue_receivable": "5800.00",
        "inventory_stagnation": None,
    })
    triggered = {a["metric"]: a for a in alerts if a.get("triggered")}
    assert set(triggered) == {"gross_margin", "overdue_receivable"}
    assert triggered["gross_margin"]["severity"] == "high"
    stalled = next(a for a in alerts if a["metric"] == "inventory_stagnation")
    assert stalled.get("not_evaluated") is True
    # 处置任务经确认车道派单：pending → confirm 生效 / reject 留痕
    task = ev.raise_disposal_task(
        triggered["gross_margin"], title="毛利异常复核（锚点实例）",
        assignee="reconciler")
    assert task["status"] == "pending"
    with pytest.raises(TemplateError):
        ev.raise_disposal_task(stalled, title="未触发不可派单")  # 未触发拒绝派单
    ev.confirm_disposal_task(task["request_id"], decided_by="owner")
    task2 = ev.raise_disposal_task(triggered["overdue_receivable"],
                                   title="逾期应收催收（锚点实例）")
    ev.reject_disposal_task(task2["request_id"], "证据不足", decided_by="owner")
    assert ev.pending_tasks() == []  # 一个确认派发、一个拒绝留痕


def test_handoff_lines_drive_planning_without_key_mismatch(tmp_path, supply_md):
    """确认车道交出的 handoff 行必须能直接喂给履约编排（键名不得错配）。

    2026-11-08 产上首现：`confirm_order` 写进 handoff 的 lines 是**取价后**的
    形状（商品键叫 `product`），而 `plan_fulfillment` 只认 `product_code` ——
    标准「确认 → 履约」流会拿空编码查主档，报「商品未建档：」。这里直接消费
    handoff 载荷里的行，锁死两条车道对同一行的理解一致。
    """
    m = supply_md
    m.confirm_request(m.create_entity("customer", {"code": "C-CUST",
                                                   "name": "客户丙"}
                                      )["request_id"], decided_by="owner")
    ff = FulfillmentOrchestrator("tpl-supply", tmp_path / "data", m,
                                 DemoWmsAdapter([]), DemoLogisticsAdapter())
    handoff_ref, order_ref = _handoff(
        tmp_path, m, [{"product_code": "P-DS", "qty": 1, "unit": "件"}])
    handoff = next(e for e in ff._handoff_log.events()
                   if e.get("ref") == handoff_ref)
    plan = ff.plan_fulfillment(handoff_ref, order_ref, ids_customer(m),
                               handoff["payload"]["lines"])  # 原样消费，不改形状
    assert plan["lines"][0]["product_code"] == "P-DS"
    assert plan["lines"][0]["path"] == "dropship"

    with pytest.raises(TemplateError) as ei:
        ff.plan_fulfillment(handoff_ref, order_ref, ids_customer(m),
                            [{"qty": "1"}])
    assert ei.value.code == "INVALID_ARGUMENT"

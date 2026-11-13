"""生产工单（change: add-manufacturing-orders 任务 2.1-2.5）。

判据（对应 spec `erp-manufacturing`）：

1. BOM 人工、单层、自引用拒、同组件重复拒、修订追加式；
2. 工单按 BOM 展开、ref 幂等、无 BOM 拒；
3. 领料单位成本必须由人给（缺失即拒）、非 BOM 组件拒、超领点名；
4. 完工：单位成本 = 归集 ÷ 计划数量，累计超计划拒，未归集拒；
5. status 的在制品与账上 1401（按 production_order 维度）对账，不一致如实报差。

零 DB：替身内核只记提案；`issue/complete` 落账后由测试替身回填分录。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.manufacturing import ManufacturingLane

TENANT = "tpl-supply"


class _StubKernel:
    def __init__(self, entries: list[dict] | None = None):
        self.entries = list(entries or [])
        self.submitted: list[dict] = []

    def query_entries(self, offset=0, limit=100, **_kw):
        return {"entries": self.entries[offset:offset + limit],
                "rows_returned": len(self.entries)}

    def list_proposals(self, status=None, **_kw):
        return {"proposals": [], "rows_returned": 0}

    def submit_proposal(self, stable_id, lines, **kw):
        prop = {"proposal_id": len(self.submitted) + 1, "stable_id": stable_id,
                "status": "pending", "lines": lines, **kw}
        self.submitted.append(prop)
        return prop


def _lane(tmp_path, kernel=None) -> ManufacturingLane:
    return ManufacturingLane(TENANT, tmp_path, kernel or _StubKernel(),
                             today="2026-11-01")


def _bom(lane, product="FG-1", qty="1", components=None):
    comps = components or [{"sku": "RM-1", "qty": "2", "unit": "个"},
                           {"sku": "RM-2", "qty": "1", "unit": "个"}]
    return lane.register_bom(product, qty, comps, registered_by="owner")


# ---- 1) BOM ----

def test_bom_is_registered_by_a_human_only(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.register_bom("FG-1", "1", [{"sku": "RM-1", "qty": "2"}],
                          registered_by="system")
    assert "人工登记" in ei.value.message
    assert lane.bom("FG-1") is None


def test_bom_refuses_self_reference_and_duplicate_rows(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.register_bom("FG-1", "1", [{"sku": "FG-1", "qty": "1"}])
    assert "自引用" in ei.value.message
    with pytest.raises(TemplateError) as ei:
        lane.register_bom("FG-1", "1", [{"sku": "RM-1", "qty": "1"},
                                        {"sku": "RM-1", "qty": "2"}])
    assert "两次" in ei.value.message


def test_bom_revision_is_appended(tmp_path):
    lane = _lane(tmp_path)
    first = _bom(lane)
    second = lane.register_bom("FG-1", "1", [{"sku": "RM-1", "qty": "3"}],
                               registered_by="owner")
    assert first["version"] == 1 and second["version"] == 2
    assert lane.bom("FG-1")["components"][0]["qty"] == "3.000000"
    assert len(lane._events("bom")) == 2


def test_multilevel_bom_is_refused_in_both_registration_orders(tmp_path):
    """spec 写的是「多层 BOM MUST 拒绝」——不是「不展开就算数」。

    只堵一个方向，换一种登记顺序就能绕过去，所以两个方向都验。
    """
    lane = _lane(tmp_path)
    lane.register_bom("SUB-1", "1", [{"sku": "RM-1", "qty": "2"}],
                      registered_by="owner")
    with pytest.raises(TemplateError) as ei:            # ① 组件自身有 BOM
        lane.register_bom("FG-1", "1",
                          [{"sku": "SUB-1", "qty": "1"}], registered_by="owner")
    assert "多层 BOM" in ei.value.message
    assert ei.value.detail["components_with_bom"] == ["SUB-1"]

    other = _lane(tmp_path / "b")
    other.register_bom("FG-1", "1", [{"sku": "SUB-1", "qty": "1"}],
                       registered_by="owner")
    with pytest.raises(TemplateError) as ei:            # ② 产品已被当组件用
        other.register_bom("SUB-1", "1",
                           [{"sku": "RM-1", "qty": "2"}], registered_by="owner")
    assert "多层" in ei.value.message
    assert other.bom("SUB-1") is None
    # 单层形状不受影响：上层仍可正常下工单
    assert other.order("FG-1", "1", ref="WO-1")["requirements"][0]["sku"] == "SUB-1"


# ---- 2) 工单 ----

def test_order_expands_the_bom_and_is_idempotent(tmp_path):
    lane = _lane(tmp_path)
    _bom(lane)
    out = lane.order("FG-1", "5", ref="WO-1")
    assert {r["sku"]: r["qty"] for r in out["requirements"]} == {
        "RM-1": "10.000000", "RM-2": "5.000000"}
    again = lane.order("FG-1", "5", ref="WO-1")
    assert again["duplicate"] is True
    assert len(lane._events("order")) == 1


def test_order_without_bom_is_refused(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _lane(tmp_path).order("FG-X", "1", ref="WO-X")
    assert "BOM" in ei.value.message


# ---- 3) 领料 ----

def test_issue_requires_a_human_given_unit_cost(tmp_path):
    lane = _lane(tmp_path)
    _bom(lane)
    lane.order("FG-1", "1", ref="WO-1")
    with pytest.raises(TemplateError) as ei:
        lane.issue("WO-1", [{"sku": "RM-1", "qty": "2"}])
    assert "成本" in ei.value.message
    with pytest.raises(TemplateError):
        lane.issue("WO-1", [{"sku": "RM-1", "qty": "2", "unit_cost": "0"}])


def test_issue_posts_wip_against_inventory(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel)
    _bom(lane)
    lane.order("FG-1", "1", ref="WO-1")
    out = lane.issue("WO-1", [{"sku": "RM-1", "qty": "2", "unit_cost": "30"},
                              {"sku": "RM-2", "qty": "1", "unit_cost": "40"}])
    assert out["total"] == "100.00"
    lines = {(l["account_code"], l["direction"]): l["amount"]
             for l in kernel.submitted[0]["lines"]}
    assert lines == {("1401", "debit"): "100.00", ("1200", "credit"): "100.00"}
    assert kernel.submitted[0]["dimensions"] == {"production_order": "WO-1"}
    assert kernel.submitted[0]["source_ref"] == "mfg-issue:WO-1"


def test_issue_refuses_components_outside_the_bom(tmp_path):
    lane = _lane(tmp_path)
    _bom(lane)
    lane.order("FG-1", "1", ref="WO-1")
    with pytest.raises(TemplateError) as ei:
        lane.issue("WO-1", [{"sku": "RM-9", "qty": "1", "unit_cost": "10"}])
    assert "不在工单" in ei.value.message


def test_over_issue_is_recorded_and_named(tmp_path):
    lane = _lane(tmp_path)
    _bom(lane)
    lane.order("FG-1", "1", ref="WO-1")
    out = lane.issue("WO-1", [{"sku": "RM-1", "qty": "3", "unit_cost": "10"}])
    assert out["overIssued"] == [{"sku": "RM-1", "required": "2.000000",
                                  "issued": "3.000000", "over": "1.000000"}]
    assert "超领" in out["note"]


# ---- 4) 完工 ----

def test_complete_transfers_wip_to_finished_goods(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel)
    _bom(lane)
    lane.order("FG-1", "2", ref="WO-1")
    lane.issue("WO-1", [{"sku": "RM-1", "qty": "4", "unit_cost": "30"}])   # 120
    out = lane.complete("WO-1", "1")
    assert out["unitCost"] == "60.00"           # 120 ÷ 计划 2
    assert out["total"] == "60.00"
    lines = {(l["account_code"], l["direction"]): l["amount"]
             for l in kernel.submitted[-1]["lines"]}
    assert lines == {("1200", "debit"): "60.00", ("1401", "credit"): "60.00"}


def test_complete_refuses_over_completion_and_missing_cost(tmp_path):
    lane = _lane(tmp_path)
    _bom(lane)
    lane.order("FG-1", "2", ref="WO-1")
    with pytest.raises(TemplateError) as ei:
        lane.complete("WO-1", "1")              # 还没领料
    assert "归集成本" in ei.value.message
    lane.issue("WO-1", [{"sku": "RM-1", "qty": "4", "unit_cost": "10"}])
    lane.complete("WO-1", "1")
    with pytest.raises(TemplateError) as ei:
        lane.complete("WO-1", "2")              # 只剩 1 台
    assert ei.value.detail["remaining"] == "1.000000"


# ---- 5) 在制品对账 ----

def test_status_reconciles_wip_with_the_ledger(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel)
    _bom(lane)
    lane.order("FG-1", "2", ref="WO-1")
    lane.issue("WO-1", [{"sku": "RM-1", "qty": "4", "unit_cost": "30"}])   # 归集 120
    lane.complete("WO-1", "1")                                            # 转出 60

    status = lane.status("WO-1")
    assert status["issuedCost"] == "120.00"
    assert status["completedCost"] == "60.00"
    assert status["wipBalance"] == "60.00"
    # 账上还没有分录（提案未过门）→ 台账 60 vs 账上 0，如实报差
    assert status["wipOnLedger"] == "0.00"
    assert status["difference"] == "60.00" and status["consistent"] is False

    # 模拟内核落账：把两张提案的分录回填
    base = len(kernel.entries)
    for i, prop in enumerate(kernel.submitted):
        for j, line in enumerate(prop["lines"]):
            kernel.entries.append({
                "account_code": line["account_code"], "direction": line["direction"],
                "amount": line["amount"], "amount_base": line["amount"],
                "currency": "CNY", "booked_on": "2026-11-01",
                "posted_at": "2026-11-01T09:00:00+08:00",
                "txn_id": f"t-{i}", "line_no": j + 1,
                "dimensions": prop["dimensions"]})
    after = lane.status("WO-1")
    assert after["wipOnLedger"] == "60.00"
    assert after["consistent"] is True and after["difference"] == "0.00"


def test_status_names_a_ledger_gap(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel)
    _bom(lane)
    lane.order("FG-1", "2", ref="WO-1")
    lane.issue("WO-1", [{"sku": "RM-1", "qty": "4", "unit_cost": "30"}])
    # 账上多出一笔同维度手工分录
    kernel.entries.append({
        "account_code": "1401", "direction": "debit", "amount": "50",
        "amount_base": "50", "currency": "CNY", "booked_on": "2026-11-02",
        "posted_at": "2026-11-02T09:00:00+08:00", "txn_id": "t-x", "line_no": 1,
        "dimensions": {"production_order": "WO-1"}})
    status = lane.status("WO-1")
    assert status["wipBalance"] == "120.00"
    assert status["wipOnLedger"] == "50.00"
    assert status["consistent"] is False
    assert status["difference"] == "70.00"

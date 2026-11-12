"""承运商运费单车道测试（change: add-carrier-freight-dimension tasks 3.1-3.3）。

判据（对应 spec `erp-carrier-freight-lane`）：

1. 计费按运单幂等，`refs` 带 `{waybill, carrier, period}`；
2. 归集只认带承运商的费用行；无承运商维度的费用不进单（宁可空也不编）；
3. 明细按运单聚合、带费用溯源；
4. 确认走内核提案车道（自平记录）；驳回必附 note；已拍板再拍如实冲突。

零 DB：替身内核记录 bill_event / list_fees / submit_proposal 的调用。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.freight import FreightStatementLane

TENANT = "tpl-cp"
CARRIER = "承运甲"
DAY = "2026-11-08"


class _StubKernel:
    """记录调用的替身：bill_event 幂等、list_fees 回费用行、submit_proposal 幂等。

    `list_fees` 默认按**真内核的形态**返回（`paginate` → `data/total/truncated`）；
    `shape="fees"` 复现模板层 in-process 替身的旧形态（`fees/rows_returned`），
    两条都验，防止「只对得上单侧替身、真链读空」。
    """

    def __init__(self, *, shape: str = "paginate", page_cap: int | None = None):
        self.fees: list[dict] = []
        self.billed: dict[str, dict] = {}
        self.proposals: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self._shape = shape
        self._page_cap = page_cap

    def bill_event(self, event):
        self.calls.append(("bill_event", event))
        sid = event["stable_id"]
        if sid in self.billed:
            return {"duplicate": True, "fees": self.billed[sid]["fees"]}
        fee_id = len(self.fees) + 1
        row = {"fee_id": fee_id, "fee_item": "运费", "amount": "40.000000",
               "currency": "CNY", "rule_id": 7, "rule_version": 2,
               "status": "posted", "refs": dict(event.get("refs") or {})}
        self.fees.append(row)
        out = {"duplicate": False, "fees": [row]}
        self.billed[sid] = out
        return out

    def list_fees(self, offset=0, limit=100):
        cap = self._page_cap or limit
        window = self.fees[offset:offset + min(limit, cap)]
        if self._shape == "fees":  # 旧形态：不带完整性声明
            return {"fees": window, "rows_returned": len(self.fees)}
        return {"data": window, "total": len(self.fees), "offset": offset,
                "limit": cap, "truncated": offset + len(window) < len(self.fees)}

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        reversal_of=None, lane_fleet=None, lane_post=None,
                        lane_title=None, lane_target=None, tenant_id=None,
                        submitted_by="agent"):
        self.calls.append(("submit_proposal", {"stable_id": stable_id,
                                               "lines": lines,
                                               "lane_target": lane_target}))
        if stable_id in self.proposals:
            return {**self.proposals[stable_id], "duplicate": True}
        out = {"id": len(self.proposals) + 1,
               "proposal_id": len(self.proposals) + 1, "stable_id": stable_id,
               "source_ref": source_ref, "lane_target": lane_target,
               "status": "pending", "duplicate": False}
        self.proposals[stable_id] = out
        return out

    def list_proposals(self, status=None, tenant_id=None, offset=0, limit=100):
        rows = [r for r in self.proposals.values()
                if status is None or r["status"] == status]
        return {"data": rows[offset:offset + limit], "total": len(rows)}


@pytest.fixture()
def lane():
    return FreightStatementLane(TENANT, _StubKernel())


def test_both_response_shapes_are_read():
    """真内核返回 `data`、模板替身返回 `fees` —— 两种形态都必须读到行。

    只认一种就会在另一侧静默读空（跨入口形状漂移）：单侧测试全绿、真链永远空单。
    """
    for shape in ("paginate", "fees"):
        lane = FreightStatementLane(TENANT, _StubKernel(shape=shape))
        lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                           occurred_at=f"{DAY}T09:00:00", measures={})
        out = lane.statements()
        assert out["total"] == 1, shape
        assert out["rows"][0]["total"] == "40.000000", shape


def test_fee_scan_pages_past_the_page_cap():
    """对端每页只给 2 行：也要翻到底，不能只读第一页（假空单）。"""
    kernel = _StubKernel(page_cap=2)
    lane = FreightStatementLane(TENANT, kernel)
    for i in range(5):
        lane.bill_delivery(waybill=f"WB-{i}", carrier=CARRIER,
                           occurred_at=f"{DAY}T09:00:00", measures={})
    out = lane.statements()
    assert out["rows"][0]["waybillCount"] == 5
    assert out["truncated"] is False


def test_scan_truncation_is_reported_not_hidden():
    """页数上限用尽时如实标 truncated，不把读到的当成全部。"""
    lane = FreightStatementLane(TENANT, _StubKernel(page_cap=1))
    kernel = lane._kernel
    for i in range(5):
        lane.bill_delivery(waybill=f"WB-{i}", carrier=CARRIER,
                           occurred_at=f"{DAY}T09:00:00", measures={})
    # max_pages=1：只允许读一页
    fees, truncated = lane._fee_rows(max_pages=1)
    assert len(fees) == 1 and truncated is True
    assert kernel.fees  # 替身里确实有更多行
    out = lane.statements()  # 默认上限足够，读完 5 行
    assert out["truncated"] is False


def test_bill_delivery_is_per_waybill_and_carries_refs(lane):
    out = lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                             occurred_at=f"{DAY}T09:00:00",
                             measures={"quantity": 2, "amount": "40"})
    assert out["duplicate"] is False
    call = lane._kernel.calls[0][1]
    assert call["stable_id"] == "WB-1:freight"
    assert call["refs"] == {"waybill": "WB-1", "carrier": CARRIER, "period": DAY}
    # 同运单重推：幂等，不再增费用行
    again = lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                               occurred_at=f"{DAY}T09:00:00",
                               measures={"quantity": 2, "amount": "40"})
    assert again["duplicate"] is True
    assert len(lane._kernel.fees) == 1


def test_bill_delivery_requires_carrier(lane):
    with pytest.raises(TemplateError) as ei:
        lane.bill_delivery(waybill="WB-2", carrier="  ",
                           occurred_at=f"{DAY}T09:00:00", measures={})
    assert "缺承运商" in ei.value.message


def test_statements_group_by_carrier_and_period(lane):
    lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                       occurred_at=f"{DAY}T09:00:00", measures={})
    lane.bill_delivery(waybill="WB-2", carrier=CARRIER,
                       occurred_at=f"{DAY}T15:00:00", measures={})
    lane.bill_delivery(waybill="WB-3", carrier="承运乙",
                       occurred_at=f"{DAY}T15:00:00", measures={})
    out = lane.statements()
    assert out["total"] == 2  # 承运甲一张（两张运单并入）+ 承运乙一张
    by_id = {r["statementId"]: r for r in out["rows"]}
    assert by_id[f"FS-{CARRIER}-{DAY}"]["waybillCount"] == 2
    assert by_id[f"FS-{CARRIER}-{DAY}"]["total"] == "80.000000"
    assert by_id[f"FS-承运乙-{DAY}"]["waybillCount"] == 1


def test_fees_without_carrier_never_enter_statements(lane):
    """无承运商维度的费用不进运费单——宁可空也不编归集。"""
    lane._kernel.fees.append({"fee_id": 99, "fee_item": "仓储费", "amount": "10.000000",
                              "currency": "CNY", "rule_id": 1, "rule_version": 1,
                              "status": "posted", "refs": {"document": "SO-1"}})
    out = lane.statements()
    assert out["rows"] == []
    assert "没有带承运商" in out["note"]


def test_statement_detail_aggregates_by_waybill_with_provenance(lane):
    lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                       occurred_at=f"{DAY}T09:00:00", measures={})
    detail = lane.statement(f"FS-{CARRIER}-{DAY}")
    assert detail["waybillCount"] == 1
    line = detail["lines"][0]
    assert line["waybill"] == "WB-1" and line["total"] == "40.000000"
    fee = line["fees"][0]
    assert fee["ruleVersion"] == 2 and fee["refs"]["carrier"] == CARRIER


def test_statement_not_found_and_bad_id(lane):
    with pytest.raises(TemplateError) as ei:
        lane.statement("FS-不存在-2026-11-08")
    assert ei.value.code == "NOT_FOUND"
    with pytest.raises(TemplateError) as bad:
        lane.statement("XX-1")
    assert bad.value.code == "NOT_FOUND"


def test_confirm_records_self_balancing_proposal(lane):
    lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                       occurred_at=f"{DAY}T09:00:00", measures={})
    out = lane.confirm(f"FS-{CARRIER}-{DAY}", "accepted", note="对账一致")
    assert out["status"] == "pending"
    proposal_call = [c for c in lane._kernel.calls if c[0] == "submit_proposal"][0][1]
    debit = sum(float(l["amount"]) for l in proposal_call["lines"]
                if l["direction"] == "debit")
    credit = sum(float(l["amount"]) for l in proposal_call["lines"]
                 if l["direction"] == "credit")
    assert debit == credit == 40.0  # 自平：净额为零，不动余额
    assert proposal_call["lane_target"] == "diff:freight_statement_confirmed"


def test_confirm_reject_requires_note(lane):
    lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                       occurred_at=f"{DAY}T09:00:00", measures={})
    with pytest.raises(TemplateError) as ei:
        lane.confirm(f"FS-{CARRIER}-{DAY}", "rejected")
    assert "note" in ei.value.message


def test_confirm_twice_after_settled_raises_workflow_state(lane):
    lane.bill_delivery(waybill="WB-1", carrier=CARRIER,
                       occurred_at=f"{DAY}T09:00:00", measures={})
    sid = f"FS-{CARRIER}-{DAY}"
    first = lane.confirm(sid, "accepted")
    assert first["status"] == "pending"
    # 确认门落定（内核把这条提案拍板了）
    stored = [v for v in lane._kernel.proposals.values()
              if v.get("source_ref", "").find(f":statement:{sid}->") >= 0]
    assert len(stored) == 1
    stored[0]["status"] = "confirmed"
    # 改判驳回：结论类型不同 ⇒ stable_id 不同，幂等撞不上；必须靠落定扫描拦住
    with pytest.raises(TemplateError) as ei:
        lane.confirm(sid, "rejected", note="改主意")
    assert ei.value.code == "WORKFLOW_STATE"
    assert "已拍板" in ei.value.message
    # 同结论再拍也拦（不因「同 kind 幂等」而静默返回旧提案）
    with pytest.raises(TemplateError) as again:
        lane.confirm(sid, "accepted")
    assert again.value.code == "WORKFLOW_STATE"
    assert len(lane._kernel.proposals) == 1  # 没有因再拍多出提案

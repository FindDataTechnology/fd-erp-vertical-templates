"""到货预告 → 事件层 → 数量面 → 过账 的接线测试（establish-erp-event-kernel 7.1）。

模板层只经 KernelClient 协议说话，所以这里用一个记录调用的假内核，逐条断言
「它到底对内核做了什么」——不碰 DB，也不假装验过内核语义（那是内核仓的测试）。

三条必须立的判据：
  1. 一行一事件、键绑到货预告 ref（重放同一 ref 才幂等，不靠行内容去重）；
  2. 缺库位/批次/单位时写 `__unknown__`，绝不写空串（空串在内核会被读成「没有」）；
  3. 内核说没规则过账 → 如实 `posted=False` + 原因，不伪造已过账、也不吞成 200 成功。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.orchestration import (UNKNOWN,
                                                     ProcurementOrchestrator)


class RecordingKernel:
    """只记录协议调用；可注入单步失败，用来验证差错回传。"""

    def __init__(self, *, fail_post: str | None = None,
                 fail_ingest_on: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._fail_post = fail_post
        self._fail_ingest_on = fail_ingest_on
        self._next_id = 100

    def ingest_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None, provenance=None):
        self.calls.append(("ingest_event", {
            "event_type": event_type, "stable_id": stable_id,
            "payload": dict(payload), "source_ref": source_ref,
            "provenance": dict(provenance or {})}))
        if self._fail_ingest_on and self._fail_ingest_on == payload.get("sku"):
            raise _KernelErr("EVENT_UNKNOWN_TYPE", "事件类型未登记")
        self._next_id += 1
        return {"id": self._next_id, "status": "accepted", "duplicate": False}

    def project_event(self, event_id, object_type, object_key, quantity_delta,
                      unit, dimension=None):
        self.calls.append(("project_event", {
            "event_id": event_id, "object_type": object_type,
            "object_key": object_key, "quantity_delta": quantity_delta,
            "unit": unit, "dimension": dict(dimension or {})}))
        return {"quantity": str(quantity_delta), "unit": unit, "drift": False}

    def post_event(self, event_id):
        self.calls.append(("post_event", {"event_id": event_id}))
        if self._fail_post:
            raise _KernelErr(self._fail_post, "缺过账规则：未落账（不猜科目）")
        return {"posted": True, "txn_id": f"txn-{event_id}", "lines": 2}

    def query_object_balance(self, object_type, object_key, dimension=None):
        self.calls.append(("query_object_balance", {
            "object_type": object_type, "object_key": object_key,
            "dimension": dict(dimension or {})}))
        return {"quantity": "5", "unit": "箱", "dimension": dict(dimension or {})}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def call(self, name: str, index: int = 0) -> dict:
        hits = [args for n, args in self.calls if n == name]
        return hits[index]

    # 模板层还有别的协议方法（proposal/credit/billing），本用例不触发；
    # 缺方法时由 Python 自然抛 AttributeError，正是「多调了不该调的面」的证据。


class _KernelErr(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@pytest.fixture()
def po_ref(tmp_path):
    """建一条已推送的采购单，返回其 ref（到货预告的前置状态）。"""
    md = MasterData("tpl-arrival", tmp_path / "data")
    sid = md.confirm_request(
        md.create_entity("supplier", {"code": "S001", "name": "供应商乙"})
        ["request_id"], decided_by="owner")["entity_id"]
    for code in ("P001", "P002"):
        md.confirm_request(
            md.create_entity("product", {
                "code": code, "name": f"商品{code}",
                "units": [{"unit": "件", "kind": "base", "rate": 1},
                          {"unit": "箱", "kind": "purchase", "rate": 24}],
                "base_price": "100.00", "supplier_id": sid})["request_id"],
            decided_by="owner")
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md)
    demand = proc.submit_demand("manual", [
        {"product_code": "P001", "qty": "5", "unit": "件"},
        {"product_code": "P002", "qty": "2", "unit": "件"}])
    agg = proc.aggregate_demand([demand["ref"]])
    po = proc.propose_purchase_order(agg["ref"], sid)
    proc.confirm_purchase_order(po["request_id"])
    proc.record_push(po["ref"], channel="supplier-portal")
    return tmp_path, md, po["ref"]


def test_arrival_notice_lands_one_event_per_line(po_ref):
    """两行到货 → 两次 ingest + 两次投影 + 两次过账；键是 ref:行号，载荷带 po_ref。"""
    tmp_path, md, ref = po_ref
    kernel = RecordingKernel()
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md, kernel=kernel)

    out = proc.record_arrival_notice(
        ref, [{"product_code": "P001", "qty": "5", "unit": "箱",
               "warehouse": "WH-EAST", "lot": "L2301"},
              {"product_code": "P002", "qty": "2", "unit": "件"}])

    assert out["ref"].startswith("AN")
    assert kernel.names() == ["ingest_event", "project_event", "post_event",
                              "ingest_event", "project_event", "post_event"]
    first = kernel.call("ingest_event", 0)
    assert first["event_type"] == "goods_received"
    assert first["stable_id"] == f"{out['ref']}:1"
    assert first["source_ref"] == out["ref"]
    assert first["provenance"]["source"] == "supplier-arrival-notice"
    assert first["payload"] == {"sku": "P001", "quantity": "5", "unit": "箱",
                               "warehouse": "WH-EAST", "lot": "L2301",
                               "po_ref": ref}
    # 投影维度跟着载荷走：库位/批次都在，数量用同一笔
    proj = kernel.call("project_event", 0)
    assert proj["object_type"] == "sku" and proj["object_key"] == "P001"
    assert proj["quantity_delta"] == "5" and proj["unit"] == "箱"
    assert proj["dimension"] == {"warehouse": "WH-EAST", "lot": "L2301"}
    # 响应把每行的事实回给调用方（含余额与分录句柄），不是只说"有 2 行"
    line = out["kernel"]["lines"][0]
    assert line["balance"] == "5" and line["posted"] is True
    assert line["txn_id"].startswith("txn-")
    assert kernel.call("ingest_event", 1)["stable_id"] == f"{out['ref']}:2"


def test_missing_dimensions_are_explicit_unknown_not_empty_string(po_ref):
    """到货预告不带库位/批次/单位 → 写 __unknown__；写空串会被内核读成「没有」。"""
    tmp_path, md, ref = po_ref
    kernel = RecordingKernel()
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md, kernel=kernel)

    out = proc.record_arrival_notice(ref, [{"product": "P001", "qty": 5}])

    payload = kernel.call("ingest_event", 0)["payload"]
    assert payload["unit"] == UNKNOWN
    assert payload["warehouse"] == UNKNOWN
    assert payload["lot"] == UNKNOWN
    assert "" not in (payload["unit"], payload["warehouse"], payload["lot"])
    assert kernel.call("project_event", 0)["dimension"] == {
        "warehouse": UNKNOWN, "lot": UNKNOWN}
    assert out["kernel"]["lines"][0]["dimension"]["warehouse"] == UNKNOWN


def test_no_posting_rule_is_reported_not_faked(po_ref):
    """内核无过账规则 → posted=False + 原因，事件与库存照旧成立（三处只有账本缺）。"""
    tmp_path, md, ref = po_ref
    kernel = RecordingKernel(fail_post="POSTING_RULE_MISSING")
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md, kernel=kernel)

    out = proc.record_arrival_notice(ref, [{"product_code": "P001", "qty": "5",
                                            "unit": "箱"}])

    line = out["kernel"]["lines"][0]
    assert line["posted"] is False
    assert line["posting_reason"] == "rule_missing_or_incomplete"
    assert line["posting_code"] == "POSTING_RULE_MISSING"
    assert "未落账" in line["posting_error"]
    # 事件与数量面没被这笔账务缺口拖下水
    assert line["event_id"] and line["balance"] == "5"
    assert out["kernel"]["ok"] is True  # 过账缺口不是本轮接线的失败


def test_ingest_failure_is_surfaced_per_line(po_ref):
    """某行事件落不进内核 → 该行进 errors 并点名步骤，另一行不受影响。"""
    tmp_path, md, ref = po_ref
    kernel = RecordingKernel(fail_ingest_on="P002")
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md, kernel=kernel)

    out = proc.record_arrival_notice(
        ref, [{"product_code": "P001", "qty": "1", "unit": "箱"},
              {"product_code": "P002", "qty": "1", "unit": "箱"}])

    assert out["kernel"]["ok"] is False
    err = out["kernel"]["errors"][0]
    assert err["line"] == 2 and err["step"] == "ingest_event"
    assert err["code"] == "EVENT_UNKNOWN_TYPE"
    assert [ln["line"] for ln in out["kernel"]["lines"]] == [1]
    # 编排记录仍在（工作流真相），且不缺行
    assert len(proc._get(out["ref"])["payload"]["lines"]) == 2


def test_no_kernel_degrades_honestly(po_ref):
    """没注入内核 → 明确说「事实未落事件层」，不假装成功。"""
    tmp_path, md, ref = po_ref
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md)
    out = proc.record_arrival_notice(ref, [{"product_code": "P001", "qty": "1"}])
    assert out["kernel"] == {"ok": False, "skipped": "no_kernel", "lines": [],
                             "reason": out["kernel"]["reason"]}
    assert "未落事件层" in out["kernel"]["reason"]


def test_delivery_feedback_passes_kernel_result_through(po_ref):
    """发货反馈自动生成到货预告，事实层的结论也要回给调用方（否则前端看不见）。"""
    tmp_path, md, ref = po_ref
    kernel = RecordingKernel()
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md, kernel=kernel)
    out = proc.record_delivery_feedback(
        ref, [{"product_code": "P001", "qty": "3", "unit": "箱"}],
        responded_by="sp:S001")
    assert out["kernel"]["lines"][0]["event_id"]
    assert out["kernel"]["lines"][0]["posted"] is True


def test_bad_line_is_reported_without_kernel_call(po_ref):
    """缺商品编码的行不入事件层：记账事实不许无主键。"""
    tmp_path, md, ref = po_ref
    kernel = RecordingKernel()
    proc = ProcurementOrchestrator("tpl-arrival", tmp_path / "data", md, kernel=kernel)
    out = proc.record_arrival_notice(ref, [{"qty": "1"}])
    assert kernel.calls == []
    assert out["kernel"]["errors"][0]["code"] == "INVALID_ARGUMENT"

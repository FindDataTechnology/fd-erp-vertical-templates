"""开票回执车道（change: add-invoice-receipt-lane 任务 1.1-1.7）。

判据（对应 spec `erp-invoice-issuance`）：

1. 只能给 `accepted` 的申请录回执（pending / rejected 都拒，顺序不许倒）；
2. 号码全租户唯一，撞号点名归属；同（申请,号码,金额）幂等；
3. 金额三态：matched / mismatched+差额 / unverified（没推定值就不算相符）；
4. 未开票清单只含 accepted 且无有效回执；
5. 作废留痕、清单恢复、号码可重新登记。

零 DB：替身内核按 event_type 返回申请事件。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.invoice_receipt import InvoiceReceiptLane

TENANT = "tpl-supply"
CUST = "C-001"


class _StubKernel:
    """事件替身：`invoice_requested` 的申请投影（status 由用例指定）。"""

    def __init__(self, requests: list[dict] | None = None):
        self._rows = list(requests or [])
        self.calls: list[dict] = []

    def query_events(self, event_type=None, lane=None, status=None, limit=100,
                     after_event_id=0, **_kw):
        self.calls.append({"event_type": event_type, "after": after_event_id})
        rows = [r for r in self._rows
                if (event_type is None or r["event_type"] == event_type)
                and r["id"] > after_event_id]
        return {"items": rows[:limit],
                "completeness": {"declared_total": len(rows)}}


def _req(ref, status="accepted", customer=CUST, order="SO-1"):
    return {"id": int(ref.split("-")[-1]) if ref.split("-")[-1].isdigit() else 1,
            "event_type": "invoice_requested", "stable_id": ref,
            "status": status, "lane": "draft",
            "occurred_at": "2026-11-01T08:00:00+00:00",
            "payload": {"customer_id": customer, "title": "示范客户一号",
                        "tax_id": "91310000MA1K3XYZ12", "order_id": order}}


def _lane(tmp_path, requests=None):
    return InvoiceReceiptLane(TENANT, tmp_path / "data",
                              _StubKernel(requests or [_req("IR-C-001-000001")]))


# ---- 1) 前置条件 ----

def test_only_accepted_requests_can_receive_a_receipt(tmp_path):
    lane = _lane(tmp_path, [_req("IR-C-001-000001", status="pending")])
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000001", "12345678", "2026-11-10", "1000")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "pending" in ei.value.message
    assert "先过确认门再开票" in ei.value.message


def test_rejected_request_refused(tmp_path):
    lane = _lane(tmp_path, [_req("IR-C-001-000001", status="rejected")])
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000001", "12345678", "2026-11-10", "1000")
    assert "rejected" in ei.value.message


def test_unknown_request_refused(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000099", "12345678", "2026-11-10", "1000")
    assert ei.value.code == "NOT_FOUND"


# ---- 2) 号码唯一与幂等 ----

def test_duplicate_invoice_number_names_the_owner(tmp_path):
    lane = _lane(tmp_path, [_req("IR-C-001-000001"), _req("IR-C-001-000002")])
    lane.record("IR-C-001-000001", "NO-8888", "2026-11-10", "1000")
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000002", "NO-8888", "2026-11-10", "1000")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert ei.value.detail["ownerRef"] == "IR-C-001-000001"
    assert "同号两张票" in ei.value.message


def test_same_number_case_insensitive(tmp_path):
    lane = _lane(tmp_path, [_req("IR-C-001-000001"), _req("IR-C-001-000002")])
    lane.record("IR-C-001-000001", "ab-1234", "2026-11-10", "1000")
    with pytest.raises(TemplateError):
        lane.record("IR-C-001-000002", "AB-1234", "2026-11-10", "1000")


def test_same_receipt_twice_is_idempotent(tmp_path):
    lane = _lane(tmp_path)
    first = lane.record("IR-C-001-000001", "NO-8888", "2026-11-10", "1000")
    again = lane.record("IR-C-001-000001", "NO-8888", "2026-11-10", "1000")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert len(lane.receipts_for("IR-C-001-000001")) == 1


def test_same_number_different_amount_on_same_request_refused(tmp_path):
    lane = _lane(tmp_path)
    lane.record("IR-C-001-000001", "NO-8888", "2026-11-10", "1000")
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000001", "NO-8888", "2026-11-10", "900")
    assert "先作废再重录" in ei.value.message


def test_bad_invoice_number_shape_refused(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000001", "x", "2026-11-10", "1000")
    assert "形态不符" in ei.value.message


# ---- 3) 金额三态 ----

def test_amount_matched(tmp_path):
    lane = _lane(tmp_path)
    out = lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000",
                      tax_amount="115.04", expected_amount="1000")
    assert out["match"] == "matched"
    assert out["difference"] == "0.00"
    assert out["amountWithoutTax"] == "884.96"


def test_amount_mismatched_reports_difference_and_still_records(tmp_path):
    lane = _lane(tmp_path)
    out = lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "990",
                      expected_amount="1000")
    assert out["match"] == "mismatched"
    assert out["difference"] == "-10.00"
    assert "票已开出" in out["warning"]
    assert len(lane.receipts_for("IR-C-001-000001")) == 1   # 事实不能丢


def test_no_expected_amount_is_unverified_not_matched(tmp_path):
    lane = _lane(tmp_path)
    out = lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000")
    assert out["match"] == "unverified"
    assert "difference" not in out
    assert "不报「相符」" in out["note"]


def test_tax_cannot_exceed_total(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "100",
                    tax_amount="150")
    assert ei.value.detail["taxAmount"] == "150.00"


def test_amount_must_be_positive(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "0")
    assert "必须大于 0" in ei.value.message


def test_channel_must_be_in_the_book(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "100",
                    channel="carrier-pigeon")
    assert ei.value.detail["allowed"] == ["offline", "etax", "kingdee"]


# ---- 4) 未开票清单 ----

def test_unissued_lists_accepted_without_receipt(tmp_path):
    kernel = _StubKernel([_req("IR-C-001-000001"), _req("IR-C-001-000002"),
                          _req("IR-C-001-000003", status="pending"),
                          _req("IR-C-001-000004", status="rejected")])
    lane = InvoiceReceiptLane(TENANT, tmp_path / "data", kernel)
    lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000")
    out = lane.unissued([CUST])
    assert [r["requestRef"] for r in out["rows"]] == ["IR-C-001-000002"]
    assert out["total"] == 1
    assert "待审与已驳回不计入缺口" in out["caliber_note"]


def test_unissued_only_for_the_given_customers(tmp_path):
    kernel = _StubKernel([_req("IR-C-001-000001", customer="C-001"),
                          _req("IR-C-002-000001", customer="C-002")])
    lane = InvoiceReceiptLane(TENANT, tmp_path / "data", kernel)
    assert [r["customerId"] for r in lane.unissued(["C-002"])["rows"]] == ["C-002"]


def test_unissued_requires_customer_set(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.unissued([])
    assert "须给客户集合" in ei.value.message


# ---- 5) 作废 ----

def test_void_restores_the_gap_and_frees_the_number(tmp_path):
    lane = _lane(tmp_path, [_req("IR-C-001-000001"), _req("IR-C-001-000002")])
    lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000")
    assert lane.unissued([CUST])["total"] == 1
    out = lane.void("NO-1", reason="票号录错")
    assert out["duplicate"] is False
    assert lane.unissued([CUST])["total"] == 2      # 缺口恢复
    # 号码可重新登记到正确的申请
    again = lane.record("IR-C-001-000002", "NO-1", "2026-11-11", "1000")
    assert again["duplicate"] is False
    assert [r["requestRef"] for r in lane.receipts_for("IR-C-001-000002")] == \
        ["IR-C-001-000002"]


def test_void_requires_reason(tmp_path):
    lane = _lane(tmp_path)
    lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000")
    with pytest.raises(TemplateError) as ei:
        lane.void("NO-1", reason="  ")
    assert "须说明原因" in ei.value.message


def test_void_twice_is_idempotent_and_unknown_number_not_found(tmp_path):
    lane = _lane(tmp_path)
    lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000")
    lane.void("NO-1", reason="录错")
    assert lane.void("NO-1", reason="再作废")["duplicate"] is True
    with pytest.raises(TemplateError) as ei:
        lane.void("NO-NOPE", reason="不存在")
    assert ei.value.code == "NOT_FOUND"


# ---- 6) 核对视图 ----

def test_reconciliation_separates_unissued_mismatched_and_unverified(tmp_path):
    kernel = _StubKernel([_req("IR-C-001-000001"), _req("IR-C-001-000002"),
                          _req("IR-C-001-000003")])
    lane = InvoiceReceiptLane(TENANT, tmp_path / "data", kernel)
    lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000",
                expected_amount="1000")           # matched
    lane.record("IR-C-001-000002", "NO-2", "2026-11-10", "990",
                expected_amount="1000")           # mismatched
    # 第三条不录 → unissued
    out = lane.reconciliation([CUST])
    assert out["counts"] == {"issued": 2, "unissued": 1,
                             "mismatched": 1, "unverified": 0}
    verdicts = {r["requestRef"]: r["verdict"] for r in out["rows"]}
    assert verdicts == {"IR-C-001-000001": "matched",
                        "IR-C-001-000002": "mismatched",
                        "IR-C-001-000003": "unissued"}
    assert "unverified 单列计数" in out["caliber_note"]


def test_reconciliation_marks_unverified_when_no_expected_amount(tmp_path):
    lane = _lane(tmp_path)
    lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "1000")
    out = lane.reconciliation([CUST])
    assert out["counts"]["unverified"] == 1
    assert out["rows"][0]["verdict"] == "unverified"


def test_partial_invoicing_shows_as_issued_with_difference_visible(tmp_path):
    """一申请多票：按「有有效回执」判已开票，差额在明细里看得见（不静默）。"""
    lane = _lane(tmp_path)
    lane.record("IR-C-001-000001", "NO-1", "2026-11-10", "400",
                expected_amount="1000")
    out = lane.reconciliation([CUST])
    assert out["rows"][0]["verdict"] == "mismatched"
    assert out["rows"][0]["difference"] == "-600.00"
    assert len(lane.receipts_for("IR-C-001-000001")) == 1

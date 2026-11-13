"""发票台账与进项抵扣资格（change: add-cn-invoice-register 任务 1.5 / 2.3 / 3.4）。

判据（对应 spec `erp-cn-localization` 新增的五条）：

1. 登记幂等（方向+票种+号码）、票面税额与算出来的不符即拒、非法票种拒；
2. 抵扣资格 = 可抵票种 **且** 已认证——普票认证也不可抵，专票未认证进「待认证」；
3. 采购过账按台账判定：不可抵的税进成本（不出进项行），同一张票不能抵两次；
4. 销项按「税率 × 票种」分列；进项三档不漏；
5. 台账与账上 2221.01.02 勾稽，手工记账那类差额要点名。

零 DB：替身内核只记提案。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.cn_invoice import (TIER_DEDUCTIBLE,
                                                  TIER_NON_DEDUCTIBLE,
                                                  TIER_PENDING, CnInvoiceBook)
from fd_erp_vertical_templates.cn_tax import CnVatLane
from fd_erp_vertical_templates.errors import TemplateError

TENANT = "tpl-inv"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, entries: list[dict] | None = None):
        self.entries = list(entries or [])
        self.submitted: list[dict] = []

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.entries[offset:offset + limit],
                "rows_returned": len(self.entries)}

    def submit_proposal(self, stable_id, lines, **kw):
        for old in self.submitted:
            if old["stable_id"] == stable_id:
                return {**old, "duplicate": True}
        prop = {"proposal_id": len(self.submitted) + 1, "stable_id": stable_id,
                "status": "pending", "lines": lines, **kw}
        self.submitted.append(prop)
        return prop


def _book(tmp_path, kernel=None) -> CnInvoiceBook:
    k = kernel or _StubKernel()
    return CnInvoiceBook(TENANT, tmp_path, k, lane=CnVatLane(TENANT, k))


def _register(book, *, direction="purchase", kind="vat-special", number="N-1",
              net="100", rate="13"):
    return book.register(direction, kind, number, net, rate=rate,
                         issued_on="2026-11-10")


def _lines(prop) -> dict:
    return {(l["account_code"], l["direction"]): l["amount"] for l in prop["lines"]}


# ---- 1) 登记 ----

def test_registering_the_same_invoice_twice_is_idempotent(tmp_path):
    book = _book(tmp_path)
    first = _register(book)
    again = _register(book)
    assert first["duplicate"] is False and again["duplicate"] is True
    assert len(book.invoices()) == 1
    assert first["tax"] == "13.00" and first["gross"] == "113.00"


def test_invoice_tax_must_match_amount_times_rate(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _book(tmp_path).register("purchase", "vat-special", "N-9", "100",
                                 rate="13", tax="12")
    assert ei.value.detail["expected"] == "13.00"
    assert "票面税额" in ei.value.message


def test_unknown_invoice_kind_is_refused(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _register(_book(tmp_path), kind="收据")
    assert ei.value.detail["allowed"] == [
        "vat-special", "e-special", "customs-payment", "vat-general", "e-general"]


def test_direction_must_be_sale_or_purchase(tmp_path):
    with pytest.raises(TemplateError):
        _register(_book(tmp_path), direction="销项")


# ---- 2) 抵扣资格 ----

def test_a_general_invoice_cannot_be_deducted_even_if_certified(tmp_path):
    book = _book(tmp_path)
    _register(book, kind="vat-general", number="P-1")
    book.certify("P-1", certified_on="2026-11-20", by="cfo")
    tiers = book.deduction(PERIOD)["tiers"]
    assert tiers[TIER_NON_DEDUCTIBLE]["tax"] == "13.00"
    assert tiers[TIER_DEDUCTIBLE]["tax"] == "0.00"
    assert tiers[TIER_NON_DEDUCTIBLE]["invoices"][0]["certified"] is True


def test_a_special_invoice_is_pending_until_certified(tmp_path):
    book = _book(tmp_path)
    _register(book, kind="vat-special", number="S-1")
    assert book.deduction(PERIOD)["tiers"][TIER_PENDING]["tax"] == "13.00"
    book.certify("S-1", certified_on="2026-11-20", by="cfo")
    tiers = book.deduction(PERIOD)["tiers"]
    assert tiers[TIER_DEDUCTIBLE]["tax"] == "13.00"
    assert tiers[TIER_PENDING]["tax"] == "0.00"


def test_certification_requires_a_human_and_keeps_the_record(tmp_path):
    book = _book(tmp_path)
    _register(book, number="S-2")
    with pytest.raises(TemplateError):
        book.certify("S-2", by="system")
    out = book.certify("S-2", certified_on="2026-11-21", by="cfo")
    assert out["certified"] is True and out["certifiedBy"] == "cfo"
    assert out["certifiedOn"] == "2026-11-21"
    assert book.certify("S-2", by="cfo")["duplicate"] is True


def test_certifying_an_unknown_invoice_is_not_found(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _book(tmp_path).certify("不存在", by="cfo")
    assert ei.value.code == "NOT_FOUND"


# ---- 3) 过账按台账判定 ----

def test_general_invoice_purchase_puts_the_tax_into_cost(tmp_path):
    kernel = _StubKernel()
    book = _book(tmp_path, kernel)
    _register(book, kind="vat-general", number="P-1")
    out = book.purchase("B-1", "P-1", "113", booked_on="2026-11-10")
    assert out["deductible"] is False
    assert _lines(kernel.submitted[0]) == {("5000", "debit"): "113.00",
                                           ("2000", "credit"): "113.00"}


def test_uncertified_special_invoice_also_goes_to_cost(tmp_path):
    kernel = _StubKernel()
    book = _book(tmp_path, kernel)
    _register(book, kind="vat-special", number="S-1")
    out = book.purchase("B-2", "S-1", "113", booked_on="2026-11-10")
    assert out["tier"] == TIER_PENDING and out["deductible"] is False
    assert _lines(kernel.submitted[0]) == {("5000", "debit"): "113.00",
                                           ("2000", "credit"): "113.00"}


def test_certified_special_invoice_deducts_normally(tmp_path):
    kernel = _StubKernel()
    book = _book(tmp_path, kernel)
    _register(book, kind="vat-special", number="S-3")
    book.certify("S-3", certified_on="2026-11-09", by="cfo")
    out = book.purchase("B-3", "S-3", "113", booked_on="2026-11-10")
    assert out["deductible"] is True and out["tier"] == TIER_DEDUCTIBLE
    assert _lines(kernel.submitted[0]) == {
        ("5000", "debit"): "100.00", ("2221.01.02", "debit"): "13.00",
        ("2000", "credit"): "113.00"}


def test_the_same_invoice_cannot_be_deducted_twice(tmp_path):
    book = _book(tmp_path)
    _register(book, number="S-4")
    book.certify("S-4", certified_on="2026-11-09", by="cfo")
    book.purchase("B-4", "S-4", "113", booked_on="2026-11-10")
    with pytest.raises(TemplateError) as ei:
        book.purchase("B-5", "S-4", "113", booked_on="2026-11-11")
    assert "不能抵两次" in ei.value.message


def test_an_unregistered_invoice_cannot_be_posted(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _book(tmp_path).purchase("B-6", "NOPE", "113")
    assert "未登记" in ei.value.message


def test_posting_a_different_amount_than_the_invoice_is_refused(tmp_path):
    book = _book(tmp_path)
    _register(book, number="S-5", net="100")
    with pytest.raises(TemplateError) as ei:
        book.purchase("B-7", "S-5", "226", booked_on="2026-11-10")
    assert "票面" in ei.value.message


# ---- 4) 申报口径 ----

def test_sales_are_split_by_rate_and_invoice_kind(tmp_path):
    book = _book(tmp_path)
    _register(book, direction="sale", kind="vat-special", number="A-1", net="100")
    _register(book, direction="sale", kind="vat-general", number="A-2", net="200")
    out = book.filing_summary(PERIOD)
    rows = {(r["rate"], r["invoiceKind"]): (r["sales"], r["tax"])
            for r in out["salesByRateAndKind"]}
    assert rows[("13", "vat-special")] == ("100.00", "13.00")
    assert rows[("13", "vat-general")] == ("200.00", "26.00")
    assert Decimal(sum(Decimal(r["tax"]) for r in out["salesByRateAndKind"])) == 39


def test_the_three_input_tiers_add_up_to_every_registered_invoice(tmp_path):
    book = _book(tmp_path)
    _register(book, number="I-1")                       # 专票未认证 → 待认证
    _register(book, number="I-2")
    book.certify("I-2", certified_on="2026-11-09", by="cfo")   # 专票已认证 → 可抵
    _register(book, kind="vat-general", number="I-3")   # 普票 → 不可抵
    tiers = book.deduction(PERIOD)["tiers"]
    total = sum(Decimal(tiers[t]["tax"]) for t in
                (TIER_DEDUCTIBLE, TIER_PENDING, TIER_NON_DEDUCTIBLE))
    assert total == Decimal(book.deduction(PERIOD)["totalTax"])
    assert Decimal(tiers[TIER_DEDUCTIBLE]["tax"]) == 13
    assert Decimal(tiers[TIER_PENDING]["tax"]) == 13
    assert Decimal(tiers[TIER_NON_DEDUCTIBLE]["tax"]) == 13


# ---- 5) 台账与账面勾稽 ----

def _ledger_input(amount: str) -> dict:
    return {"account_code": "2221.01.02", "direction": "debit", "amount": amount,
            "amount_base": amount, "booked_on": "2026-11-10",
            "txn_id": "t-manual", "line_no": 1}


def test_reconcile_is_clean_when_everything_went_through_the_book(tmp_path):
    kernel = _StubKernel([_ledger_input("13")])
    book = _book(tmp_path, kernel)
    _register(book, number="S-9")
    book.certify("S-9", certified_on="2026-11-09", by="cfo")
    out = book.reconcile(PERIOD)
    assert out["consistent"] is True and out["difference"] == "0.00"
    assert out["ledgerInputVat"] == "13.00" and out["invoiceInputVat"] == "13.00"


def test_reconcile_names_a_manual_input_vat_posting(tmp_path):
    kernel = _StubKernel([_ledger_input("13"), _ledger_input("13")])
    book = _book(tmp_path, kernel)
    _register(book, number="S-10")
    book.certify("S-10", certified_on="2026-11-09", by="cfo")
    out = book.reconcile(PERIOD)
    assert out["consistent"] is False
    assert out["difference"] == "13.00"
    assert "未经台账" in out["warning"]


# ---- 6) vat_return 接台账（三档与勾稽同源） ----

def test_vat_return_carries_the_three_input_tiers(tmp_path):
    from fd_erp_vertical_templates.cn_statements import CnStatements
    kernel = _StubKernel()
    book = _book(tmp_path, kernel)
    _register(book, number="I-1")                              # 待认证
    _register(book, number="I-2")
    book.certify("I-2", certified_on="2026-11-09", by="cfo")   # 可抵
    _register(book, kind="vat-general", number="I-3")          # 不可抵
    out = CnStatements(TENANT, kernel).vat_return(PERIOD, invoices=book)
    assert out["inputTiers"] == {"deductible": "13.00",
                                 "pendingCertification": "13.00",
                                 "nonDeductible": "13.00"}
    assert out["invoiceReconcile"]["consistent"] is False      # 账上还没有进项分录
    assert out["invoiceReconcile"]["difference"] == "-13.00"

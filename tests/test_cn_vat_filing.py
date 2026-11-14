"""增值税申报数登记与「账面 vs 申报」拼表（change: add-cn-vat-filing-registration）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. **登记后逐项可对**：账面与申报一致时六项差额全 `0.00`、`consistent=true`；
2. **不一致时点名差额**：申报报少了，那项的 `difference` 必须是那个数，不是「差不多」；
3. **未登记 ≠ 登记为零**：前者 `provided=false` 且口径说明写「不能当作已核对」，
   后者 `provided=true` 且各为 `0.00`——混成一态会让「没人报过」显示成「报了零」；
4. **申报数必须带出处**：无 `source` 拒绝（外部事实，无出处无法复核），负数也拒；
5. **同期间后一次为准**：追加式事件档，重报/更正覆盖前一次；
6. **进项三档进表**：台账有可抵/待认证/不可抵三类时，`vat_reconciliation` 必须给出三档，
   而账上只有可抵那一档。

零 DB：替身内核返回内核真实形状。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.cn_filing import CnFilingLane
from fd_erp_vertical_templates.cn_invoice import CnInvoiceBook
from fd_erp_vertical_templates.cn_statements import CnStatements
from fd_erp_vertical_templates.cn_tax import CnVatLane
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.localization import CHART_SMALL, CnPack

TENANT = "tpl-cn-vat-filing"
PERIOD = "2026-11"
RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


class _StubKernel:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = list(rows or [])
        self.proposals: list[dict] = []

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}

    def query_balance(self, account_code, **_kw):
        total = 0
        for row in self.rows:
            if str(row.get("account_code")) != str(account_code):
                continue
            amount = float(row.get("amount_base") or row.get("amount") or 0)
            total += amount if row.get("direction") == "debit" else -amount
        return {"balance": str(-total)}

    def submit_proposal(self, stable_id, lines, **kw):
        rec = {"stable_id": stable_id, "lines": lines, **kw}
        self.proposals.append(rec)
        return {"stable_id": stable_id, "status": "pending", **kw}


def _e(code, direction, amount, txn, when):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "posted_at": f"{when}T09:00:00+08:00", "txn_id": txn, "line_no": 1,
            "dimensions": {}}


def _ledger() -> list[dict]:
    """含税现销 1130（收入 1000 + 销项 130）、含税现购 565（成本 500 + 进项 65）。"""
    return [
        _e("1000", "debit", "1130", "s1", "2026-11-10"),
        _e("4000", "credit", "1000", "s1", "2026-11-10"),
        _e("2221.01.01", "credit", "130", "s1", "2026-11-10"),
        _e("5000", "debit", "500", "p1", "2026-11-12"),
        _e("2221.01.02", "debit", "65", "p1", "2026-11-12"),
        _e("1000", "credit", "565", "p1", "2026-11-12"),
    ]


def _lane(tmp_path, *, invoices=None, rows=None) -> CnFilingLane:
    kernel = _StubKernel(_ledger() if rows is None else rows)
    statements = CnStatements(TENANT, kernel, pack=CnPack(chart=CHART_SMALL))
    return CnFilingLane(TENANT, tmp_path, kernel, statements=statements,
                        invoices=invoices)


def _declared_matching() -> dict[str, str]:
    return {"sales": "1000", "outputVat": "130", "inputVat": "65",
            "inputTransferOut": "0", "openingCredit": "0"}


# ---- 1) 登记后逐项可对 ----

def test_a_registered_return_reconciles_item_by_item(tmp_path):
    lane = _lane(tmp_path)
    lane.register_vat_return(PERIOD, source="电子税局 2026-11 申报表",
                             **_declared_matching())
    out = lane.vat_reconciliation(PERIOD)
    assert out["provided"] is True
    assert out["consistent"] is True, out["reconciliation"]
    assert [r["difference"] for r in out["reconciliation"]] == ["0.00"] * 6
    assert out["declared"]["payable"] == "65.00"
    assert out["declaredSource"] == "电子税局 2026-11 申报表"


# ---- 2) 不一致时点名差额 ----

def test_a_mismatch_names_the_exact_difference(tmp_path):
    lane = _lane(tmp_path)
    # 申报把进项报成 70（多抵 5），应纳因此少 5
    lane.register_vat_return(PERIOD, source="申报表",
                             **{**_declared_matching(), "inputVat": "70"})
    out = lane.vat_reconciliation(PERIOD)
    assert out["consistent"] is False
    payable = next(r for r in out["reconciliation"] if r["item"] == "payable")
    input_vat = next(r for r in out["reconciliation"] if r["item"] == "inputVat")
    assert input_vat["difference"] == "-5.00"
    assert payable["difference"] == "5.00"


# ---- 3) 未登记 ≠ 登记为零 ----

def test_never_registered_and_registered_zero_are_two_different_facts(tmp_path):
    lane = _lane(tmp_path)
    never = lane.vat_reconciliation(PERIOD)
    assert never["provided"] is False
    assert "不能当作已核对" in never["caliberNote"]
    assert lane.vat_declared(PERIOD) is None

    lane.register_vat_return(PERIOD, source="零申报")
    zero = lane.vat_reconciliation(PERIOD)
    assert zero["provided"] is True
    assert zero["declared"]["outputVat"] == "0.00"
    assert "不能当作已核对" not in zero["caliberNote"]
    assert zero["consistent"] is False          # 账面有 130，报了零 → 差额如实列出
    assert next(r for r in zero["reconciliation"]
                if r["item"] == "outputVat")["difference"] == "130.00"


# ---- 4) 出处必填、负数拒 ----

def test_a_return_without_a_source_is_refused(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.register_vat_return(PERIOD, source="  ", **_declared_matching())
    assert "出处" in ei.value.message


def test_a_negative_declared_item_is_refused(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.register_vat_return(PERIOD, source="x",
                                 **{**_declared_matching(), "inputVat": "-5"})
    assert "不能为负" in ei.value.message


# ---- 5) 同期间后一次为准 ----

def test_a_correction_supersedes_the_earlier_registration(tmp_path):
    lane = _lane(tmp_path)
    lane.register_vat_return(PERIOD, source="初报", **_declared_matching())
    lane.register_vat_return(PERIOD, source="更正申报",
                             **{**_declared_matching(), "outputVat": "131"})
    got = lane.vat_declared(PERIOD)
    assert got["outputVat"] == "131"
    out = lane.vat_reconciliation(PERIOD)
    assert out["declaredSource"] == "更正申报"
    assert out["consistent"] is False


# ---- 6) 进项三档进表 ----

def test_the_input_tiers_reach_the_reconciliation_from_the_invoice_book(tmp_path):
    kernel = _StubKernel(_ledger())
    book = CnInvoiceBook(TENANT, tmp_path, kernel,
                         lane=CnVatLane(TENANT, kernel))
    book.register("purchase", "vat-special", "P-DED", net="1000", tax="130",
                  issued_on="2026-11-05")
    book.certify("P-DED", certified_on="2026-11-06")
    book.register("purchase", "vat-special", "P-PEND", net="2000", tax="260",
                  issued_on="2026-11-06")                      # 可抵票种未认证
    book.register("purchase", "vat-general", "P-NODED", net="500", tax="65",
                  issued_on="2026-11-07")                      # 普票不可抵
    lane = _lane(tmp_path, invoices=book)
    out = lane.vat_reconciliation(PERIOD)
    tiers = out["inputTiers"]
    assert Decimal(tiers["deductible"]) == Decimal("130.00")
    assert Decimal(tiers["pendingCertification"]) == Decimal("260.00")
    assert Decimal(tiers["nonDeductible"]) == Decimal("65.00")


def test_without_an_invoice_book_the_tiers_simply_are_absent(tmp_path):
    out = _lane(tmp_path).vat_reconciliation(PERIOD)
    assert "inputTiers" not in out      # 没有台账就不编，而不是填三个 0

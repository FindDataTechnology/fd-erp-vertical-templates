"""账簿：科目余额表（三栏）与明细账（change: add-ledger-books 任务 2.1-2.4）。

判据（对应 spec `erp-ledger-books`）：

1. 三栏：期初 + 本期借 − 本期贷 == 期末，逐科目成立；全表借贷合计相等；
2. 互证：科目余额表期末列 == 试算平衡表余额（逐科目，两条独立取数路径）；
3. 明细账：逐行余额闭合，期末 == 科目余额表该科目期末 == 试算表余额；
4. 符号统一「借 − 贷」，方向只放 direction；表外科目出行但 line 为空；
5. 空期间零值不报错；重复查询一致；新增分录立刻反映（不缓存）。

零 DB：替身内核返回内核真实形状（`entries`）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.books import LedgerBooks
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.statements import StatementBook

TENANT = "tpl-supply"


class _StubKernel:
    def __init__(self, entries: list[dict]):
        self._entries = list(entries)

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100, **_kw):
        rows = [e for e in self._entries
                if account_code is None or e["account_code"] == account_code]
        return {"entries": rows[offset:offset + limit],
                "rows_returned": len(rows)}


def _e(code, direction, amount, booked_on="2026-11-10", line_no=1, txn=None,
       source_ref=None):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": booked_on,
            "posted_at": f"{booked_on}T09:00:00+08:00",
            "txn_id": txn or f"t-{booked_on}-{code}-{direction}",
            "line_no": line_no, "source_ref": source_ref, "dimensions": {}}


def _two_months() -> list[dict]:
    """10 月：应收 1000（收入）；11 月：收现 600、赊销 2000、费用 300 付现。"""
    return [
        _e("1100", "debit", "1000", "2026-10-05", 1, "t-oct-1"),
        _e("4000", "credit", "1000", "2026-10-05", 2, "t-oct-1"),
        _e("1000", "debit", "600", "2026-11-03", 1, "t-nov-1"),
        _e("1100", "credit", "600", "2026-11-03", 2, "t-nov-1"),
        _e("1100", "debit", "2000", "2026-11-08", 1, "t-nov-2"),
        _e("4000", "credit", "2000", "2026-11-08", 2, "t-nov-2"),
        _e("5200", "debit", "300", "2026-11-09", 1, "t-nov-3"),
        _e("1000", "credit", "300", "2026-11-09", 2, "t-nov-3"),
    ]


def _books(entries=None) -> LedgerBooks:
    return LedgerBooks(TENANT, _StubKernel(_two_months() if entries is None else entries))


# ---- 1) 三栏科目余额表 ----

def test_account_balances_three_columns():
    out = _books().account_balances("2026-11")
    rows = {r["accountCode"]: r for r in out["rows"]}
    assert rows["1100"]["opening"] == "1000.00"      # 10 月赊销
    assert rows["1100"]["debit"] == "2000.00"
    assert rows["1100"]["credit"] == "600.00"
    assert rows["1100"]["closing"] == "2400.00"     # 1000 + 2000 − 600
    assert rows["1000"]["opening"] == "0.00"
    assert rows["1000"]["closing"] == "300.00"      # 收现 600 − 付现 300
    assert rows["4000"]["opening"] == "-1000.00"     # 10 月赊销的余额进了期初列
    assert rows["4000"]["closing"] == "-3000.00"     # 期末是累计余额：−1000 − 2000
    assert rows["4000"]["direction"] == "credit"
    assert out["totals"]["debit"] == out["totals"]["credit"] == "2900.00"
    assert out["balanced"] is True
    assert all(r["balanced"] for r in out["rows"])


def test_every_row_proves_opening_plus_movement_equals_closing():
    out = _books().account_balances("2026-11")
    for row in out["rows"]:
        assert Decimal(row["opening"]) + Decimal(row["debit"]) \
            - Decimal(row["credit"]) == Decimal(row["closing"])


# ---- 2) 与试算平衡表互证（两条独立取数路径）----

def test_closing_column_matches_trial_balance_account_by_account():
    entries = _two_months()
    books = _books(entries)
    tb = StatementBook(TENANT, _StubKernel(entries)).trial_balance("2026-11")
    balances = {r["accountCode"]: r["balance"] for r in tb["rows"]}
    rows = {r["accountCode"]: r["closing"] for r in books.account_balances("2026-11")["rows"]}
    assert rows == balances


def test_off_chart_account_gets_a_row_without_a_line():
    entries = _two_months() + [_e("9999", "debit", "50", "2026-11-10", 1, "t-x"),
                               _e("1000", "credit", "50", "2026-11-10", 2, "t-x")]
    out = _books(entries).account_balances("2026-11")
    row = next(r for r in out["rows"] if r["accountCode"] == "9999")
    assert row["line"] is None and row["closing"] == "50.00"
    assert out["unclassified"] == ["9999"]
    assert out["balanced"] is True                      # 表外科目不影响借贷平衡


# ---- 3) 明细账 ----

def test_subsidiary_ledger_runs_a_balance_line_by_line():
    out = _books().subsidiary_ledger("1100", "2026-11")
    assert out["opening"] == "1000.00"
    assert [r["debit"] for r in out["rows"]] == ["0.00", "2000.00"]   # 先收现后赊销
    assert [r["balance"] for r in out["rows"]] == ["400.00", "2400.00"]
    assert out["totals"] == {"debit": "2000.00", "credit": "600.00",
                             "closing": "2400.00"}
    assert out["closingBalance"] == "2400.00"
    assert out["balanced"] is True
    assert out["rows"][0]["txnId"] == "t-nov-1"


def test_subsidiary_closing_equals_account_balances_closing():
    books = _books()
    sub = books.subsidiary_ledger("1100", "2026-11")
    row = next(r for r in books.account_balances("2026-11")["rows"]
               if r["accountCode"] == "1100")
    assert sub["closingBalance"] == row["closing"]


def test_subsidiary_without_period_starts_from_zero():
    out = _books().subsidiary_ledger("1100")
    assert out["opening"] == "0.00"
    assert out["closingBalance"] == "2400.00"        # 1000 + 2000 − 600
    assert len(out["rows"]) == 3


def test_subsidiary_of_an_account_without_movement_is_empty_but_says_so():
    out = _books().subsidiary_ledger("1601", "2026-11")
    assert out["rows"] == [] and out["totals"]["closing"] == "0.00"
    assert "无发生额" in out["note"]


def test_subsidiary_requires_an_account_code():
    with pytest.raises(TemplateError) as ei:
        _books().subsidiary_ledger("", "2026-11")
    assert ei.value.code == "INVALID_ARGUMENT"


def test_subsidiary_limit_truncates_display_but_not_the_totals():
    out = _books().subsidiary_ledger("1100", "2026-11", limit=1)
    assert len(out["rows"]) == 1 and out["truncated"] is True
    assert out["rowCount"] == 2
    assert out["closingBalance"] == "2400.00"        # 合计仍按全量算
    assert out["rows"][-1]["balance"] == "2400.00"


# ---- 4) 空、只读、无缓存 ----

def test_empty_period_is_zero_not_an_error():
    out = _books().account_balances("2026-09")
    assert out["rows"] == [] and out["balanced"] is True
    assert out["totals"] == {"opening": "0.00", "debit": "0.00",
                             "credit": "0.00", "closing": "0.00"}


def test_reports_are_identical_across_calls():
    books = _books()
    assert books.account_balances("2026-11") == books.account_balances("2026-11")
    assert books.subsidiary_ledger("1100", "2026-11") \
        == books.subsidiary_ledger("1100", "2026-11")


def test_new_entries_show_up_without_any_rebuild():
    kernel = _StubKernel(_two_months())
    books = LedgerBooks(TENANT, kernel)
    before = books.account_balances("2026-11")
    kernel._entries.append(_e("1000", "debit", "70", "2026-11-12", 1, "t-new"))
    kernel._entries.append(_e("4000", "credit", "70", "2026-11-12", 2, "t-new"))
    after = books.account_balances("2026-11")
    assert Decimal(after["totals"]["debit"]) == Decimal(before["totals"]["debit"]) + 70
    row = next(r for r in after["rows"] if r["accountCode"] == "1000")
    assert row["closing"] == "370.00"


def test_period_follows_booked_on_not_posting_time():
    entries = [{"account_code": "1100", "direction": "debit", "amount": "100",
                "amount_base": "100", "booked_on": "2026-10-31",
                "posted_at": "2026-11-02T09:00:00+08:00", "txn_id": "t1", "line_no": 1},
               {"account_code": "4000", "direction": "credit", "amount": "100",
                "amount_base": "100", "booked_on": "2026-10-31",
                "posted_at": "2026-11-02T09:00:00+08:00", "txn_id": "t1", "line_no": 2}]
    books = _books(entries)
    assert books.account_balances("2026-10")["rows"] != []
    nov = books.account_balances("2026-11")
    # 11 月没有发生额：不带余额的科目不出现，带余额的科目出行但本期两列为零
    assert {r["accountCode"]: (r["debit"], r["credit"], r["closing"])
            for r in nov["rows"]} == {"1100": ("0.00", "0.00", "100.00"),
                                      "4000": ("0.00", "0.00", "-100.00")}
    assert nov["totals"]["debit"] == "0.00"
    sub = books.subsidiary_ledger("1100", "2026-11")
    assert sub["opening"] == "100.00"                  # 期初把它算进来


# ---- 6) 序时账与凭证（change: add-fi-voucher-and-journal）----

def _two_vouchers():
    """两张业务凭证（各借贷相等）+ 一张结转凭证。"""
    return [
        _e("1100", "debit", "1130", txn="T1", line_no=1, source_ref="vat-sale:S1"),
        _e("4000", "credit", "1000", txn="T1", line_no=2, source_ref="vat-sale:S1"),
        _e("2221", "credit", "130", txn="T1", line_no=3, source_ref="vat-sale:S1"),
        _e("5000", "debit", "300", txn="T2", line_no=1, source_ref="cogs:2026-11"),
        _e("1200", "credit", "300", txn="T2", line_no=2, source_ref="cogs:2026-11"),
        _e("4000", "debit", "1000", txn="T9", line_no=1, source_ref="close:2026-11"),
        _e("3103", "credit", "1000", txn="T9", line_no=2, source_ref="close:2026-11"),
    ]


def test_journal_summarises_per_voucher():
    out = _books(_two_vouchers()).journal("2026-11")
    assert out["vouchers"] == 3 and out["lines"] == 7
    assert out["balanced"] is True, out.get("error")
    rows = {r["txnId"]: r for r in out["rows"]}
    assert rows["T1"]["debit"] == "1130.00" and rows["T1"]["credit"] == "1130.00"
    assert rows["T1"]["balanced"] is True and rows["T1"]["lines"] == 3
    assert rows["T2"]["debit"] == "300.00"
    # 结转凭证必须标出来——不标会被当成业务发生额
    assert rows["T9"]["isClosing"] is True
    assert rows["T1"]["isClosing"] is False
    assert out["totals"]["debit"] == out["totals"]["credit"] == "2430.00"


def test_journal_limit_truncates_only_the_displayed_vouchers():
    out = _books(_two_vouchers()).journal("2026-11", limit=1)
    assert out["truncated"] is True and out["rowCount"] == 3
    assert len(out["rows"]) == 1
    # 合计按全量三张算
    assert out["totals"]["debit"] == "2430.00" and out["vouchers"] == 3


def test_journal_on_an_empty_period():
    out = _books([]).journal("2026-11")
    assert out["rows"] == [] and out["vouchers"] == 0
    assert out["balanced"] is True
    assert "没有分录" in out["note"]


def test_voucher_returns_every_line_and_balances():
    out = _books(_two_vouchers()).voucher("T1")
    assert out["txnId"] == "T1"
    assert len(out["lines"]) == 3
    codes = [r["accountCode"] for r in out["lines"]]
    assert codes == ["1100", "4000", "2221"]        # 按行号
    assert out["lines"][0]["debit"] == "1130.00"
    assert out["lines"][0]["account"]                  # 带科目名
    assert out["totals"] == {"debit": "1130.00", "credit": "1130.00"}
    assert out["balanced"] is True
    assert out["isClosing"] is False
    assert out["sourceRef"] == "vat-sale:S1"


def test_voucher_marks_a_closing_voucher():
    out = _books(_two_vouchers()).voucher("T9")
    assert out["isClosing"] is True
    assert out["balanced"] is True
    assert [r["accountCode"] for r in out["lines"]] == ["4000", "3103"]


def test_voucher_of_an_unknown_txn_is_not_found():
    with pytest.raises(TemplateError) as ei:
        _books(_two_vouchers()).voucher("NOPE")
    assert ei.value.code == "NOT_FOUND"
    assert ei.value.detail["txnId"] == "NOPE"


def test_voucher_requires_a_txn_id():
    with pytest.raises(TemplateError) as ei:
        _books([]).voucher("  ")
    assert ei.value.code == "INVALID_ARGUMENT"

"""报表期间对比（change: add-report-comparison 任务 1.1-1.5）。

判据（对应 spec `erp-report-comparison`）：

1. 基准封闭（previous / last-year），跨年按日历回退，不猜「最近有数的期间」；
2. 零基准不给率（`ratio=None`），差额照常；
3. 无基准期间 → `baselineAvailable=false`，基准值与变动率置空（不拿 0 当基准）；
4. 差额统一「本期 − 基准」，变动率分母取绝对值（亏损扩大不翻转符号）；
5. 本期列与报表同源：同一期对比表的本期值 == 直接查该期报表。

零 DB：替身内核 + 真实 StatementBook。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.comparison import ReportComparison
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.statements import StatementBook

TENANT = "tpl-supply"


class _StubKernel:
    def __init__(self, entries: list[dict]):
        self._entries = list(entries)

    def query_entries(self, offset=0, limit=100, **_kw):
        return {"entries": self._entries[offset:offset + limit],
                "rows_returned": len(self._entries)}


def _e(code, direction, amount, period, line_no=1, txn=None):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": f"{period}-10",
            "posted_at": f"{period}-10T09:00:00+08:00",
            "txn_id": txn or f"t-{period}-{code}", "line_no": line_no,
            "dimensions": {}}


def _two_months() -> list[dict]:
    """10 月：现销 1000、费用 400；11 月：现销 3000、费用 500。"""
    return [
        _e("1000", "debit", "1000", "2026-10", 1, "t-oct-sale"),
        _e("4000", "credit", "1000", "2026-10", 2, "t-oct-sale"),
        _e("5200", "debit", "400", "2026-10", 1, "t-oct-exp"),
        _e("1000", "credit", "400", "2026-10", 2, "t-oct-exp"),
        _e("1000", "debit", "3000", "2026-11", 1, "t-nov-sale"),
        _e("4000", "credit", "3000", "2026-11", 2, "t-nov-sale"),
        _e("5200", "debit", "500", "2026-11", 1, "t-nov-exp"),
        _e("1000", "credit", "500", "2026-11", 2, "t-nov-exp"),
    ]


def _book(entries=None) -> ReportComparison:
    kernel = _StubKernel(_two_months() if entries is None else entries)
    return ReportComparison(TENANT, kernel,
                            statements=StatementBook(TENANT, kernel))


# ---- 1) 基准 ----

def test_baseline_is_previous_month_and_wraps_the_year():
    assert ReportComparison.baseline_period("2026-11") == "2026-10"
    assert ReportComparison.baseline_period("2026-01") == "2025-12"
    assert ReportComparison.baseline_period("2026-11", "last-year") == "2025-11"


def test_unknown_basis_is_refused_with_allowed_values():
    with pytest.raises(TemplateError) as ei:
        ReportComparison.baseline_period("2026-11", "ytd")
    assert ei.value.detail["allowed"] == ["previous", "last-year"]
    assert "本年累计" in ei.value.detail["note"]


# ---- 2) 逐行对比 ----

def test_income_rows_show_current_baseline_change_and_ratio():
    out = _book().compare("2026-11")
    inc = out["statements"]["incomeStatement"]
    assert inc["baselinePeriod"] == "2026-10" and inc["baselineAvailable"] is True
    rows = {r["key"]: r for r in inc["rows"]}
    assert rows["营业收入"]["current"] == "3000.00"
    assert rows["营业收入"]["baseline"] == "1000.00"
    assert rows["营业收入"]["change"] == "2000.00"
    assert rows["营业收入"]["ratio"] == "200.00%"
    assert rows["其他经营费用"]["ratio"] == "25.00%"        # 500 vs 400


def test_balance_and_cash_rows_are_aligned():
    out = _book().compare("2026-11")
    bal = out["statements"]["balanceSheet"]
    keys = {r["key"] for r in bal["rows"]}
    assert {"1000", "__assets_total__", "__unclosed__"} <= keys
    cash = out["statements"]["cashFlow"]
    assert {r["key"] for r in cash["rows"]} == {
        "operating", "investing", "financing", "netIncrease"}
    op = next(r for r in cash["rows"] if r["key"] == "operating")
    assert op["current"] == "2500.00" and op["baseline"] == "600.00"


# ---- 3) 零基准不给率 ----

def test_zero_baseline_gives_no_ratio_but_keeps_the_change():
    entries = [_e("1000", "debit", "800", "2026-11", 1, "t-nov"),
               _e("4000", "credit", "800", "2026-11", 2, "t-nov"),
               _e("5200", "debit", "100", "2026-10", 1, "t-oct"),   # 上期无收入
               _e("1000", "credit", "100", "2026-10", 2, "t-oct")]
    out = _book(entries).compare("2026-11")
    row = next(r for r in out["statements"]["incomeStatement"]["rows"]
               if r["key"] == "营业收入")
    assert row["current"] == "800.00" and row["baseline"] == "0.00"
    assert row["change"] == "800.00"
    assert row["ratio"] is None
    assert "基准为零" in out["statements"]["incomeStatement"]["note"]


# ---- 4) 无基准期间 ----

def test_missing_baseline_period_is_not_zero():
    entries = [_e("1000", "debit", "500", "2026-11", 1, "t-nov"),
               _e("4000", "credit", "500", "2026-11", 2, "t-nov")]
    out = _book(entries).compare("2026-11")
    inc = out["statements"]["incomeStatement"]
    assert inc["baselineAvailable"] is False
    assert all(r["baseline"] is None and r["ratio"] is None and r["change"] is None
               for r in inc["rows"])
    assert inc["rows"][0]["current"] == "500.00"          # 本期照常给
    assert "不拿 0 当基准" in inc["note"]
    # 存量表同月无可比累计也一样判不可用（账本从 11 月才开始）
    assert out["statements"]["balanceSheet"]["baselineAvailable"] is False


def test_stock_baseline_is_available_when_only_cumulative_history_exists():
    """基准期间没有发生额，但累计余额有内容 → 资产负债表仍可比；流量表不可比。"""
    entries = [_e("1000", "debit", "1000", "2026-10", 1, "t-oct"),
               _e("3000", "credit", "1000", "2026-10", 2, "t-oct"),
               _e("1000", "debit", "500", "2026-12", 1, "t-dec"),
               _e("4000", "credit", "500", "2026-12", 2, "t-dec")]
    out = _book(entries).compare("2026-12")       # 基准 2026-11：无发生但有累计
    assert out["statements"]["balanceSheet"]["baselineAvailable"] is True
    assert out["statements"]["incomeStatement"]["baselineAvailable"] is False


# ---- 5) 差额符号 ----

def test_loss_widening_keeps_the_negative_sign():
    entries = [
        _e("5200", "debit", "300", "2026-11", 1, "t-nov"),
        _e("1000", "credit", "300", "2026-11", 2, "t-nov"),
        _e("5200", "debit", "100", "2026-10", 1, "t-oct"),
        _e("1000", "credit", "100", "2026-10", 2, "t-oct"),
    ]
    out = _book(entries).compare("2026-11")
    row = next(r for r in out["statements"]["incomeStatement"]["rows"]
               if r["key"] == "营业利润")
    assert row["current"] == "-300.00" and row["baseline"] == "-100.00"
    assert row["change"] == "-200.00"
    assert row["ratio"] == "-200.00%"      # 分母取绝对值，不翻转成「增长 200%」


# ---- 6) 本期列与报表同源 ----

def test_current_column_equals_the_statements_themselves():
    kernel = _StubKernel(_two_months())
    book = StatementBook(TENANT, kernel)
    cmp_book = ReportComparison(TENANT, kernel, statements=book)
    out = cmp_book.compare("2026-11")

    inc = book.income_statement("2026-11")
    inc_rows = {r["key"]: r["current"] for r in out["statements"]["incomeStatement"]["rows"]}
    for line in inc["lines"]:
        assert inc_rows[line["line"]] == line["amount"]

    bs = book.balance_sheet("2026-11")
    bal_rows = {r["key"]: r["current"] for r in out["statements"]["balanceSheet"]["rows"]}
    assert bal_rows["__assets_total__"] == bs["assets"]["total"]
    assert bal_rows["__unclosed__"] == bs["unclosedProfit"]

    cf = book.cash_flow("2026-11")
    cash_rows = {r["key"]: r["current"] for r in out["statements"]["cashFlow"]["rows"]}
    assert cash_rows["operating"] == cf["categories"]["operating"]["net"]
    assert cash_rows["netIncrease"] == cf["netIncrease"]


def test_compare_rejects_bad_period():
    with pytest.raises(TemplateError):
        _book().compare("2026/11")

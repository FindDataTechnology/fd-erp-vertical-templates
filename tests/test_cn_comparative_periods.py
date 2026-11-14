"""法定报表的对照列（change: add-cn-comparative-periods）。

判据（对应 spec `erp-cn-localization` 新增要求「三张法定报表给出对照列并互相勾稽」）：

1. **上年同期取同月去年**：`2026-11 → 2025-11`，不是上一期 `2026-10`——后者是另一个数，
   且错得毫无痕迹（报表照样平、利润照样像样，只是对不上税务局那一份）；
2. **本年累计只从 1 月 1 日起算**：不是「账本第一天起算」，那是资产负债表的口径；
3. **资产负债表年初数 = 上一年 12 月的期末数**：年初与期末走同一段代码，只换窗口；
4. **现金流量表累计列与资产负债表勾稽**：累计期末现金 = 货币资金期末数、
   累计期初现金 = 货币资金年初数、`期初 + 净增加 = 期末`；
5. **一年前没有账本时如实报 0**，不拿本期数顶替、不按比例估。

零 DB：替身内核返回内核真实形状。
"""
from __future__ import annotations

from decimal import Decimal

from fd_erp_vertical_templates.cn_statements import CnStatements
from fd_erp_vertical_templates.localization import CHART_LARGE, CHART_SMALL, CnPack

TENANT = "tpl-cn-comparative"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}


def _e(code, direction, amount, txn, when):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "posted_at": f"{when}T09:00:00+08:00", "txn_id": txn, "line_no": 1,
            "dimensions": {}}


def _two_years() -> list[dict]:
    """跨两个年度的一本账（现销 + 成本结转，借贷必平）：

    2025-11 收现 1000；2025-12 收现 500；2026-01 收现 300；2026-10 收现 700；
    2026-11 收现 2000、付现成本 800。
    于是：货币资金 2025-12-31 余 1500；2026-11 期末 3700（1500 + 300 + 700 + 2000 − 800）。
    """
    rows = [
        _e("1000", "debit", "1000", "a1", "2025-11-10"),
        _e("4000", "credit", "1000", "a1", "2025-11-10"),
        _e("1000", "debit", "500", "a2", "2025-12-10"),
        _e("4000", "credit", "500", "a2", "2025-12-10"),
        _e("1000", "debit", "300", "b1", "2026-01-10"),
        _e("4000", "credit", "300", "b1", "2026-01-10"),
        _e("1000", "debit", "700", "b2", "2026-10-10"),
        _e("4000", "credit", "700", "b2", "2026-10-10"),
        _e("1000", "debit", "2000", "b3", "2026-11-10"),
        _e("4000", "credit", "2000", "b3", "2026-11-10"),
        _e("5000", "debit", "800", "b4", "2026-11-10"),
        _e("1000", "credit", "800", "b4", "2026-11-10"),
    ]
    return rows


def _book(chart: str = CHART_SMALL, rows: list[dict] | None = None) -> CnStatements:
    return CnStatements(TENANT, _StubKernel(_two_years() if rows is None else rows),
                        pack=CnPack(chart=chart))


def _line(rows, key):
    return next(r for r in rows if r["key"] == key)


# ---- 1) 上年同期是同月去年，不是上一期 ----

def test_the_prior_year_column_is_the_same_month_last_year():
    inc = _book().income_statement(PERIOD)
    assert inc["priorYear"]["period"] == "2025-11"
    assert inc["priorYear"]["totals"]["netProfit"] == "1000.00"
    # 上一期（2026-10）是另一个数：取错期间不会有任何报错，只会对不上税务局那一份
    october = _book().income_statement("2026-10")
    assert october["totals"]["netProfit"] == "700.00"
    assert inc["priorYear"]["totals"]["netProfit"] != october["totals"]["netProfit"]


def test_the_prior_year_column_equals_a_direct_query_of_that_period():
    inc = _book().income_statement(PERIOD)
    direct = _book().income_statement("2025-11")
    assert (inc["priorYear"]["totals"] == direct["totals"])
    assert ([l["amount"] for l in inc["priorYear"]["lines"]]
            == [l["amount"] for l in direct["lines"]])


# ---- 2) 本年累计只从 1 月 1 日起算 ----

def test_the_cumulative_column_starts_at_january_first_not_at_the_ledger_start():
    inc = _book().income_statement(PERIOD)
    revenue = _line(inc["cumulative"]["lines"], "revenue")["amount"]
    # 300（1 月）+ 700（10 月）+ 2000（11 月）；不含 2025-12 的 500
    assert revenue == "3000.00"
    assert inc["cumulative"]["totals"]["netProfit"] == "2200.00"
    assert _line(inc["lines"], "revenue")["amount"] == "2000.00"   # 本期只有 11 月


# ---- 3) 资产负债表年初数 = 上一年 12 月的期末数 ----

def test_the_opening_column_equals_last_december_closing():
    bs = _book().balance_sheet(PERIOD)
    last = _book().balance_sheet("2025-12")
    assert bs["opening"]["asOf"] == "2025-12-31"
    assert bs["opening"]["totals"] == last["totals"]
    for group in ("assets", "liabilities", "equity"):
        assert ([l["amount"] for l in bs["opening"][group]]
                == [l["amount"] for l in last[group]]), group
        assert ([l["key"] for l in bs["opening"][group]]
                == [l["key"] for l in last[group]]), group
    assert bs["opening"]["balanced"] is True


def test_the_opening_of_next_year_is_this_years_closing():
    jan = _book().balance_sheet("2027-01")
    dec = _book().balance_sheet("2026-12")
    assert jan["opening"]["totals"] == dec["totals"]
    assert _line(jan["opening"]["assets"], "cash")["amount"] == "3700.00"


def test_each_line_moves_by_exactly_the_years_own_entries():
    bs = _book().balance_sheet(PERIOD)
    year_only = _book(rows=[r for r in _two_years()
                            if str(r["booked_on"]).startswith("2026")]).balance_sheet(PERIOD)
    for line in bs["assets"]:
        opening = Decimal(_line(bs["opening"]["assets"], line["key"])["amount"])
        moved = Decimal(_line(year_only["assets"], line["key"])["amount"])
        assert opening + moved == Decimal(line["amount"]), line["key"]


# ---- 4) 现金流量表的对照列与资产负债表勾稽 ----

def test_the_cumulative_cash_column_reconciles_with_the_balance_sheet():
    st = _book()
    flow = st.cash_flow(PERIOD)
    bs = st.balance_sheet(PERIOD)
    cash_now = Decimal(_line(bs["assets"], "cash")["amount"])
    cash_open = Decimal(_line(bs["opening"]["assets"], "cash")["amount"])
    cum = flow["cumulative"]
    assert Decimal(cum["closingCash"]) == cash_now == Decimal("3700.00")
    assert Decimal(cum["openingCash"]) == cash_open == Decimal("1500.00")
    assert (Decimal(cum["openingCash"]) + Decimal(cum["netIncrease"])
            == Decimal(cum["closingCash"]))
    assert Decimal(cum["categorizedNet"]) == Decimal(cum["netIncrease"])
    # 本期列只算 11 月：期初是 11 月 1 日之前（2500），收 2000 付 800
    assert flow["netIncrease"] == "1200.00"
    assert Decimal(flow["openingCash"]) == Decimal("2500.00")
    assert Decimal(flow["closingCash"]) == Decimal("3700.00")
    # 两列的期末是同一个数（同一个账本、同一时点），只是起点不同
    assert flow["closingCash"] == cum["closingCash"]


def test_the_prior_year_cash_column_equals_a_direct_query():
    flow = _book().cash_flow(PERIOD)
    direct = _book().cash_flow("2025-11")
    assert flow["priorYear"]["period"] == "2025-11"
    for key in ("openingCash", "closingCash", "netIncrease"):
        assert flow["priorYear"][key] == direct[key], key
    assert flow["priorYear"]["consistent"] is True


def test_both_charts_agree_on_the_comparative_columns():
    small = _book(CHART_SMALL)
    large = _book(CHART_LARGE)
    a, b = small.balance_sheet(PERIOD), large.balance_sheet(PERIOD)
    assert a["opening"]["totals"] == b["opening"]["totals"]
    for column in ("cumulative", "priorYear"):
        ai, bi = small.income_statement(PERIOD), large.income_statement(PERIOD)
        assert ai[column]["totals"] == bi[column]["totals"], column
    for column in ("cumulative", "priorYear"):
        ac, bc = small.cash_flow(PERIOD), large.cash_flow(PERIOD)
        for key in ("openingCash", "closingCash", "netIncrease", "categorizedNet"):
            assert ac[column][key] == bc[column][key], (column, key)


# ---- 5) 一年前没有账本时如实报零 ----

def test_a_year_without_a_ledger_reports_zero_instead_of_borrowing_this_year():
    only_2026 = [r for r in _two_years() if str(r["booked_on"]).startswith("2026")]
    st = _book(rows=only_2026)
    inc = st.income_statement(PERIOD)
    assert inc["priorYear"]["period"] == "2025-11"
    assert all(l["amount"] == "0.00" for l in inc["priorYear"]["lines"])
    assert inc["priorYear"]["totals"]["netProfit"] == "0.00"
    flow = st.cash_flow(PERIOD)
    assert flow["priorYear"]["openingCash"] == "0.00"
    assert flow["priorYear"]["netIncrease"] == "0.00"
    assert flow["priorYear"]["categorizedNet"] == "0.00"
    # 年初也没有账本，所以年初数全为零——空是事实，不是要补一个像样的数
    bs = st.balance_sheet(PERIOD)
    assert bs["opening"]["totals"]["assets"] == "0.00"

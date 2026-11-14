"""国别报表的损益口径必须排除结转分录（change: fix-cn-statements-closing-entries）。

判据（对应 spec `erp-cn-localization` 新增要求「国别报表的损益口径排除结转分录，
且所得税预缴与利润表同源」）：

1. **结账月的利润表不因结转分录归零**：收入 1000、成本 400 的月份过完结转，仍然是
   收入 1000、净利 600——不是 0/0/0（0 不是「这期没赚钱」，是「账已经做完了」）；
2. **资产负债表不受结转影响**：结转是真实的权益搬移，排除它会让「未结转损益」永远不减；
3. **所得税预缴基数与利润表累计列逐值相等**：同一口径两份实现在结账月必然分叉，
   而两侧都不报错；
4. **增值税申报口径的销售额不归零**；
5. 反证：把过滤去掉，这几条必须红（判据真的盯着结转分录，而不是恰好都为零）。

零 DB：替身内核返回内核真实形状。
"""
from __future__ import annotations

from decimal import Decimal

from fd_erp_vertical_templates.cn_filing import CnFilingLane
from fd_erp_vertical_templates.cn_statements import CnStatements
from fd_erp_vertical_templates.localization import CHART_SMALL, CnPack

TENANT = "tpl-cn-closing"
PERIOD = "2026-11"
RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


class _StubKernel:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)
        self.proposals: list[dict] = []

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}

    def submit_proposal(self, stable_id, lines, **kw):
        """预缴只出待确认提案；这里记录形状即可（本用例只看金额与基数）。"""
        rec = {"stable_id": stable_id, "lines": lines, **kw}
        self.proposals.append(rec)
        return {"stable_id": stable_id, "status": "pending", **kw}


def _e(code, direction, amount, txn, when, *, source_ref=None):
    row = {"account_code": code, "direction": direction, "amount": amount,
           "amount_base": amount, "booked_on": when,
           "posted_at": f"{when}T09:00:00+08:00", "txn_id": txn, "line_no": 1,
           "dimensions": {}}
    if source_ref:
        row["source_ref"] = source_ref
    return row


def _month(*, with_closing: bool) -> list[dict]:
    """含税现销 1130（收入 1000 + 销项 130）、成本结转 400：

    过账后再补一笔 `close:2026-11` 把损益搬进留存收益（借收入/贷费用/差额进 3100）。
    """
    rows = [
        _e("1000", "debit", "1130", "t1", "2026-11-10"),
        _e("4000", "credit", "1000", "t1", "2026-11-10"),
        _e("2221.01.01", "credit", "130", "t1", "2026-11-10"),
        _e("5000", "debit", "400", "t2", "2026-11-10"),
        _e("1200", "credit", "400", "t2", "2026-11-10"),
    ]
    if with_closing:
        # 结转：收入 1000 反方向结平、成本 400 反方向，差额 600 进留存收益
        rows += [
            _e("4000", "debit", "1000", "c1", "2026-11-30",
               source_ref="close:2026-11"),
            _e("5000", "credit", "400", "c1", "2026-11-30",
               source_ref="close:2026-11"),
            _e("3100", "credit", "600", "c1", "2026-11-30",
               source_ref="close:2026-11"),
        ]
    return rows


def _book(rows):
    return CnStatements(TENANT, _StubKernel(rows), pack=CnPack(chart=CHART_SMALL))


def _line(rows, key):
    return next(r for r in rows if r["key"] == key)


# ---- 1) 结账月的利润表不归零 ----

def test_a_closed_month_still_reports_its_revenue_and_profit():
    inc = _book(_month(with_closing=True)).income_statement(PERIOD)
    assert _line(inc["lines"], "revenue")["amount"] == "1000.00"
    assert inc["totals"]["totalProfit"] == "600.00"
    assert inc["totals"]["netProfit"] == "600.00"


def test_the_closed_and_open_month_reports_are_identical():
    closed = _book(_month(with_closing=True)).income_statement(PERIOD)
    open_ = _book(_month(with_closing=False)).income_statement(PERIOD)
    assert closed["totals"] == open_["totals"]
    assert ([l["amount"] for l in closed["lines"]]
            == [l["amount"] for l in open_["lines"]])
    assert closed["cumulative"]["totals"] == open_["cumulative"]["totals"]
    assert closed["priorYear"]["totals"] == open_["priorYear"]["totals"]


# ---- 2) 资产负债表不受结转影响 ----

def test_closing_entries_do_not_distort_the_balance_sheet():
    closed = _book(_month(with_closing=True)).balance_sheet(PERIOD)
    open_ = _book(_month(with_closing=False)).balance_sheet(PERIOD)
    assert closed["totals"] == open_["totals"]
    assert closed["balanced"] is True and open_["balanced"] is True
    # 有结转 → 未结转损益为 0；无结转 → 等于当期损益净额 600
    assert Decimal(_line(closed["equity"], "__unclosed__")["amount"]) == 0
    assert _line(open_["equity"], "__unclosed__")["amount"] == "600.00"


# ---- 3) 预缴基数与利润表同源 ----

def test_the_income_tax_base_equals_the_income_statement_cumulative_profit():
    kernel = _StubKernel(_month(with_closing=True))
    statements = CnStatements(TENANT, kernel, pack=CnPack(chart=CHART_SMALL))
    lane = CnFilingLane(TENANT, "/tmp", kernel, statements=statements)
    lane.register_rates(RATES, effective_from="2026-01", source="测试：财税公告")
    prepay = lane.income_tax_prepay(PERIOD, submitted_by="test")
    cumulative = statements.income_statement(PERIOD)["cumulative"]["totals"]
    assert prepay["cumulativeProfit"] == cumulative["totalProfit"] == "600.00"
    assert prepay["due"] == "150.00"             # 600 × 25%
    assert prepay["noop"] is False


# ---- 4) 申报口径的销售额不归零 ----

def test_the_vat_return_sales_do_not_collapse_after_closing():
    closed = _book(_month(with_closing=True)).vat_return(PERIOD)
    open_ = _book(_month(with_closing=False)).vat_return(PERIOD)
    assert Decimal(closed["ledger"]["sales"]) == Decimal("1000.00")
    assert closed["ledger"]["sales"] == open_["ledger"]["sales"]


# ---- 5) 反证：去掉过滤这几条会红 ----

def test_the_filter_is_what_keeps_the_numbers_right(monkeypatch):
    """把 is_closing_entry 换成永不命中，结账月的报表必须塌成 0 ——
    证明上面的用例真的盯着结转分录，而不是恰好都为零。"""
    from fd_erp_vertical_templates import cn_statements as module

    monkeypatch.setattr(module, "is_closing_entry", lambda _row: False)
    inc = _book(_month(with_closing=True)).income_statement(PERIOD)
    assert _line(inc["lines"], "revenue")["amount"] == "0.00"
    assert inc["totals"]["totalProfit"] == "0.00"
    vat = _book(_month(with_closing=True)).vat_return(PERIOD)
    assert Decimal(vat["ledger"]["sales"]) == 0

"""中国法定报表（change: add-cn-localization-pack 任务 4.4）。

判据（对应 spec `erp-cn-localization` 第四、五条）：

1. 资产负债表按小企业准则项目出，且自平（资产总计 = 负债和所有者权益总计）；
2. 已发生额但落不进任何项目的科目被 `unmapped` 点名、`complete=false`——
   漏项的报表照样平，所以「平」与「完整」是两个判断；
3. 含税销售拆进「营业收入 100 / 应交税费 13」，不是 113 全进收入；
4. 利润表按本期发生额（不是累计），营业利润/利润总额/净利润逐级算对；
5. 申报口径与账面逐项勾稽：未提供独立申报数时自报「未核对」，
   提供后不一致要报差额（手工在销项科目记账那类差异必须现形）。

零 DB：替身内核按内核真实形状返回 entries。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.cn_statements import CnStatements
from fd_erp_vertical_templates.errors import TemplateError

TENANT = "tpl-cn"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}


def _e(code, direction, amount, when="2026-11-10", txn=None):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "posted_at": f"{when}T09:00:00+08:00",
            "txn_id": txn or f"t-{code}-{direction}-{amount}", "line_no": 1,
            "dimensions": {}}


def _taxed_month() -> list[dict]:
    """含税销售 113（13%）+ 含税采购 113（13%）+ 实收资本 1000。"""
    return [
        _e("1100", "debit", "113", txn="t-sale"),
        _e("4000", "credit", "100", txn="t-sale"),
        _e("2221.01.01", "credit", "13", txn="t-sale"),
        _e("5000", "debit", "100", txn="t-buy"),
        _e("2221.01.02", "debit", "13", txn="t-buy"),
        _e("2000", "credit", "113", txn="t-buy"),
        _e("1000", "debit", "1000", txn="t-cap"),
        _e("3000", "credit", "1000", txn="t-cap"),
    ]


def _book(rows=None) -> CnStatements:
    return CnStatements(TENANT, _StubKernel(_taxed_month() if rows is None else rows))


def _line(rows, key):
    return next(r for r in rows if r["key"] == key)


# ---- 1) 自平与项目金额 ----

def test_balance_sheet_ties_and_reports_the_tax_line_separately():
    bs = _book().balance_sheet(PERIOD)
    assert bs["balanced"] is True and bs["difference"] == "0.00"
    assert bs["totals"]["assets"] == "1113.00"          # 现金 1000 + 应收 113
    assert bs["totals"]["liabilities"] == "113.00"      # 应付
    assert bs["totals"]["equity"] == "1000.00"          # 实收 + 未结转损益（本期为 0）
    assert _line(bs["liabilities"], "taxes_payable")["amount"] == "0.00"   # 销项 13 − 进项 13
    assert bs["complete"] is True and bs["unmapped"] == []


def test_revenue_and_cost_are_not_balance_sheet_items():
    """收入/费用不上资产负债表：它们由「未结转损益」承载，不能被算成漏项。"""
    bs = _book().balance_sheet(PERIOD)
    keys = {r["key"] for r in bs["assets"] + bs["liabilities"] + bs["equity"]}
    assert "__unclosed__" in keys
    assert bs["unmapped"] == []


def test_unclosed_profit_flow_shows_up_in_equity():
    rows = _taxed_month() + [_e("1100", "debit", "50", txn="t-extra"),
                             _e("4000", "credit", "50", txn="t-extra")]
    bs = _book(rows).balance_sheet(PERIOD)
    assert _line(bs["equity"], "__unclosed__")["amount"] == "50.00"
    assert bs["balanced"] is True
    assert Decimal(bs["totals"]["equity"]) == Decimal("1050")


def test_prior_months_are_cumulative_on_the_balance_sheet():
    rows = _taxed_month() + [_e("1000", "debit", "200", "2026-10-05", txn="t-oct"),
                             _e("3000", "credit", "200", "2026-10-05", txn="t-oct")]
    bs = _book(rows).balance_sheet(PERIOD)
    assert bs["totals"]["assets"] == "1313.00"          # 1113 + 200
    assert bs["balanced"] is True


# ---- 2) 漏项点名 ----

def test_an_account_outside_every_line_is_named(tmp_path):
    """1604 在建工程在映射里没有（内核没有这个码），改成把内核码指到未覆盖的准则码来验。"""
    book = _book()
    book._map["1000"] = "1901"          # 1901 待处理财产损溢：在「其他流动资产」里，仍被覆盖
    assert book.balance_sheet(PERIOD)["complete"] is True
    book._map["1000"] = "1101"          # 短期投资也在表上
    assert book.balance_sheet(PERIOD)["complete"] is True
    book._map["1000"] = "1231"          # 准则 1231 其他流动资产 → 在表上
    assert book.balance_sheet(PERIOD)["complete"] is True
    book._map.pop("1000")               # 没有映射 → 必须点名
    out = book.balance_sheet(PERIOD)
    assert out["complete"] is False
    assert out["unmapped"] == [{"accountCode": "1000", "amount": "1000.00",
                                "reason": "内核科目没有映射到准则科目"}]
    # 漏项会让表不平——这正是「平」与「完整」必须分开报的原因
    assert out["balanced"] is False
    assert "unmapped" in out["error"]


# ---- 3) 利润表 ----

def test_income_statement_splits_tax_out_of_revenue():
    inc = _book().income_statement(PERIOD)
    assert _line(inc["lines"], "revenue")["amount"] == "100.00"       # 不是 113
    assert _line(inc["lines"], "cost_of_sales")["amount"] == "100.00"
    assert inc["totals"]["netProfit"] == "0.00"
    assert inc["complete"] is True


def test_income_statement_is_period_scoped_not_cumulative():
    rows = _taxed_month() + [_e("1100", "debit", "226", "2026-10-05", txn="t-oct"),
                             _e("4000", "credit", "200", "2026-10-05", txn="t-oct"),
                             _e("2221.01.01", "credit", "26", "2026-10-05", txn="t-oct")]
    inc = _book(rows).income_statement(PERIOD)
    assert _line(inc["lines"], "revenue")["amount"] == "100.00"        # 只算 11 月
    assert _book(rows).income_statement("2026-10")["totals"]["netProfit"] == "200.00"


def test_operating_profit_walks_the_ladder():
    rows = [
        _e("1100", "debit", "1130", txn="s"), _e("4000", "credit", "1000", txn="s"),
        _e("2221.01.01", "credit", "130", txn="s"),
        _e("5000", "debit", "600", txn="c"), _e("2000", "credit", "600", txn="c"),
        _e("5100", "debit", "100", txn="e1"), _e("2000", "credit", "100", txn="e1"),
        _e("5200", "debit", "50", txn="e2"), _e("2000", "credit", "50", txn="e2"),
        _e("6603", "debit", "20", txn="fx"), _e("2000", "credit", "20", txn="fx"),
        _e("6602", "debit", "30", txn="imp"), _e("1231", "credit", "30", txn="imp"),
        _e("1000", "debit", "5", txn="no"), _e("5301", "credit", "5", txn="no"),
    ]
    inc = _book(rows).income_statement(PERIOD)
    assert _line(inc["lines"], "finance_expenses")["amount"] == "20.00"
    assert _line(inc["lines"], "nonoperating_expense")["amount"] == "30.00"
    assert _line(inc["lines"], "nonoperating_income")["amount"] == "5.00"
    assert inc["totals"]["operatingProfit"] == "230.00"   # 1000 − 600 − 100 − 50 − 20
    assert inc["totals"]["totalProfit"] == "205.00"       # 230 + 5 − 30
    assert inc["totals"]["netProfit"] == "205.00"


# ---- 4) 增值税申报口径 ----

def test_vat_return_without_a_declaration_says_it_did_not_reconcile():
    out = _book().vat_return(PERIOD)
    assert out["provided"] is False
    assert "未提供独立申报数" in out["caliberNote"]
    assert out["ledger"] == {"sales": "100.00", "outputVat": "13.00",
                             "inputVat": "13.00", "inputTransferOut": "0.00",
                             "payable": "0.00"}
    assert out["consistent"] is True


def test_vat_return_flags_a_manual_output_vat_posting():
    rows = _taxed_month() + [_e("1100", "debit", "11.3", txn="t-manual"),
                             _e("2221.01.01", "credit", "11.3", txn="t-manual")]
    out = _book(rows).vat_return(PERIOD, declared={"sales": "100", "outputVat": "13",
                                                   "inputVat": "13"})
    assert out["provided"] is True
    assert out["consistent"] is False
    diff = {r["item"]: r["difference"] for r in out["reconciliation"]}
    assert diff["outputVat"] == "11.30"       # 手工那笔
    assert diff["payable"] == "11.30"
    assert diff["sales"] == "0.00"


def test_vat_return_accepts_a_matching_declaration():
    out = _book().vat_return(PERIOD, declared={"sales": "100", "outputVat": "13",
                                               "inputVat": "13"})
    assert out["consistent"] is True
    assert {r["difference"] for r in out["reconciliation"]} == {"0.00"}


def test_bad_period_is_refused():
    with pytest.raises(TemplateError):
        _book().balance_sheet("2026/11")

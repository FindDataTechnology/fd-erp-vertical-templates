"""财务报表引擎（change: add-financial-statements 任务 1.1-1.6）。

判据（对应 spec `erp-financial-statements`）：

1. 试算平衡：每科目借/贷/余额，借贷合计必须相等；
2. 利润表：4000 − 5000 − 5200 = 营业利润，不混资本类科目；
3. 资产负债表：资产 = 负债 + 权益 + 未结转损益；表外科目进 `unclassified` 并判不平；
4. 按维度费用表：两个部门 + 未标注三行，合计等于费用总额；
5. 报表是投影：同批分录重复查询逐字段相同，新增分录后立刻跟上。

零 DB：替身内核返回内核真实形状（`entries`）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

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
        return {"entries": rows[offset:offset + limit], "rows_returned": len(rows)}


def _e(code, direction, amount, posted_at="2026-11-10T09:00:00+08:00",
       dimensions=None):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "posted_at": posted_at,
            "dimensions": dimensions or {}}


def _book(entries):
    return StatementBook(TENANT, _StubKernel(entries))


def _month_entries() -> list[dict]:
    """一个月的完整一套（借贷必平）：到货 1000、出库结转 400、费用 1000、折旧 1000。"""
    return [
        _e("1200", "debit", "1000"), _e("2000", "credit", "1000"),     # 到货
        _e("5000", "debit", "400"), _e("1200", "credit", "400"),       # 成本结转
        _e("5200", "debit", "1000", dimensions={"cost_center": "CC-1"}),
        _e("1000", "credit", "1000"),                                  # 费用
        _e("5200", "debit", "1000"), _e("1602", "credit", "1000"),     # 折旧
        _e("1000", "debit", "5000"), _e("3000", "credit", "5000"),     # 投入资本
    ]


# ---- 1) 试算平衡 ----

def test_trial_balance_totals_match():
    tb = _book(_month_entries()).trial_balance("2026-11")
    assert tb["balanced"] is True
    assert tb["totals"]["debit"] == tb["totals"]["credit"]
    assert Decimal(tb["totals"]["difference"]) == 0
    assert tb["totals"]["debit"] == "8400.00"


def test_trial_balance_reads_direction_per_account():
    tb = _book(_month_entries()).trial_balance("2026-11")
    rows = {r["accountCode"]: r for r in tb["rows"]}
    assert rows["1200"]["balance"] == "600.00"        # 1000 − 400
    assert rows["1200"]["direction"] == "debit"
    assert rows["1602"]["balance"] == "-1000.00"      # 累计折旧贷方
    assert rows["1602"]["direction"] == "credit"


def test_trial_balance_as_of_excludes_later_periods():
    entries = _month_entries() + [_e("5200", "debit", "300",
                                    posted_at="2026-12-05T09:00:00+08:00"),
                                  _e("1000", "credit", "300",
                                     posted_at="2026-12-05T09:00:00+08:00")]
    book = _book(entries)
    nov = {r["accountCode"]: r for r in book.trial_balance("2026-11")["rows"]}
    dec = {r["accountCode"]: r for r in book.trial_balance("2026-12")["rows"]}
    assert nov["5200"]["balance"] == "2000.00"
    assert dec["5200"]["balance"] == "2300.00"


def test_trial_balance_flags_off_chart_accounts_without_faking_balance():
    tb = _book([_e("9999", "debit", "10"), _e("4000", "credit", "10")]).trial_balance()
    assert tb["unclassified"] == ["9999"]
    assert tb["balanced"] is True          # 借贷仍然平；只是没归类
    assert "9999" in tb["note"]


# ---- 2) 利润表 ----

def test_income_statement_is_revenue_minus_cost_and_expense():
    inc = _book(_month_entries()).income_statement("2026-11")
    assert inc["revenue"] == "0.00"
    assert inc["costOfSales"] == "400.00"
    assert inc["otherOpex"] == "2000.00"
    assert inc["operatingProfit"] == "-2400.00"


def test_income_statement_includes_revenue():
    entries = _month_entries() + [_e("1100", "debit", "3000"),
                                  _e("4000", "credit", "3000")]
    inc = _book(entries).income_statement("2026-11")
    assert inc["revenue"] == "3000.00"
    assert inc["operatingProfit"] == "600.00"     # 3000 − 400 − 2000


def test_income_statement_refuses_bad_period():
    with pytest.raises(TemplateError) as ei:
        _book([]).income_statement("2026/11")
    assert ei.value.code == "INVALID_ARGUMENT"


# ---- 3) 资产负债表 ----

def test_balance_sheet_balances():
    bs = _book(_month_entries()).balance_sheet("2026-11")
    assert bs["balanced"] is True
    # 资产：1200 600 + 1000 4000（10000 投入 − 1000 费用 − ... 见下）+ 1602 −1000
    assert bs["assets"]["total"] == "3600.00"
    assert bs["equity"]["total"] == "5000.00"
    assert bs["liabilities"]["total"] == "1000.00"
    assert bs["unclosedProfit"] == "-2400.00"
    assert bs["liabilitiesPlusEquity"] == "3600.00"


def test_balance_sheet_marks_contra_asset():
    bs = _book(_month_entries()).balance_sheet("2026-11")
    line = next(x for x in bs["assets"]["lines"] if x["accountCode"] == "1602")
    assert line["contra"] is True and line["amount"] == "-1000.00"


def test_balance_sheet_flags_unclassified_account():
    entries = _month_entries() + [_e("8888", "debit", "50"),
                                  _e("1000", "credit", "50")]
    bs = _book(entries).balance_sheet("2026-11")
    assert bs["unclassified"] == ["8888"]
    assert bs["balanced"] is False
    assert "不猜类型" in bs["error"]


def test_balance_sheet_reports_difference_when_unbalanced():
    """借贷不平（人为构造）时报表如实报差额，不四舍五入凑平。"""
    entries = [_e("1200", "debit", "100")]
    bs = _book(entries).balance_sheet("2026-11")
    assert bs["balanced"] is False
    assert "不平" in bs["error"]


# ---- 4) 按维度费用表 ----

def test_expenses_by_dimension_with_unattributed():
    entries = [
        _e("5200", "debit", "100", dimensions={"department": "D-1"}),
        _e("5200", "debit", "200", dimensions={"department": "D-2"}),
        _e("5200", "debit", "300"),
        _e("5000", "debit", "400", dimensions={"department": "D-1"}),
    ]
    out = _book(entries).expenses_by_dimension("2026-11", "department")
    rows = {r["dimensionValue"]: r for r in out["rows"]}
    assert rows["D-1"]["amount"] == "500.00"
    assert rows["D-2"]["amount"] == "200.00"
    assert rows["unattributed"]["amount"] == "300.00"
    assert rows["unattributed"]["attributed"] is False
    assert out["total"] == "1000.00"
    assert sum(Decimal(r["amount"]) for r in out["rows"]) == Decimal("1000")


def test_expenses_by_dimension_rejects_unknown_dimension():
    with pytest.raises(TemplateError) as ei:
        _book([]).expenses_by_dimension("2026-11", "dept")
    assert ei.value.detail["allowed"]


# ---- 5) 报表是投影 ----

def test_same_entries_give_identical_reports():
    book = _book(_month_entries())
    assert book.income_statement("2026-11") == book.income_statement("2026-11")
    assert book.balance_sheet("2026-11") == book.balance_sheet("2026-11")


def test_new_entries_show_up_without_any_rebuild():
    kernel = _StubKernel(_month_entries())
    book = StatementBook(TENANT, kernel)
    before = book.income_statement("2026-11")["operatingProfit"]
    kernel._entries.append(_e("5200", "debit", "500"))
    kernel._entries.append(_e("1000", "credit", "500"))
    after = book.income_statement("2026-11")["operatingProfit"]
    assert before == "-2400.00" and after == "-2900.00"


# ---- 6) 期间按业务日期归属（change: add-period-close）----

def test_period_follows_booked_on_not_posting_time():
    """10 月的单据 11 月才落账：利润表必须把它算进 10 月。

    这正是「报表事后会变」的根因——若按 posted_at 归属，同一张 10 月利润表会随着
    谁什么时候确认而变，而且两次都「自洽」。
    """
    entries = [
        {**_e("4000", "credit", "1000"), "booked_on": "2026-10-31"},
        {**_e("4000", "credit", "2000"), "booked_on": "2026-11-02"},
    ]
    book = StatementBook(TENANT, _StubKernel(entries))
    oct_inc = book.income_statement("2026-10")
    nov_inc = book.income_statement("2026-11")
    assert oct_inc["revenue"] == "1000.00"
    assert nov_inc["revenue"] == "2000.00"


# ---- 7) 坏账准备按净额呈现（change: add-bad-debt-provision）----

def test_allowance_is_a_contra_asset_and_impairment_is_an_expense():
    """计提后：资产 = 应收 − 准备，未结转损益同步减少，资产负债表仍平。"""
    entries = [
        _e("1100", "debit", "12000"), _e("4000", "credit", "12000"),
        _e("6602", "debit", "650"), _e("1231", "credit", "650"),
    ]
    book = StatementBook(TENANT, _StubKernel(entries))
    bs = book.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    assert bs["assets"]["total"] == "11350.00"
    contra = [l for l in bs["assets"]["lines"] if l["accountCode"] == "1231"]
    assert contra and contra[0]["contra"] is True
    assert bs["unclosedProfit"] == "11350.00"

    inc = book.income_statement("2026-11")
    assert inc["impairmentLoss"] == "650.00"
    assert inc["operatingProfit"] == "11350.00"


# ---- 8) 现金流量表（change: add-cash-flow-statement）----

def _t(txn: str, booked_on: str, legs: list[tuple[str, str, str]]) -> list[dict]:
    """一笔交易：`txn_id` 相同的多行、同一业务日期。"""
    return [{**_e(code, direction, amount,
                  posted_at=f"{booked_on}T09:00:00+08:00"),
             "booked_on": booked_on, "txn_id": txn}
            for code, direction, amount in legs]


def test_cash_flow_classifies_sale_as_operating():
    entries = _t("t-sale", "2026-11-05",
                 [("1000", "debit", "1000"), ("4000", "credit", "1000")])
    cf = _book(entries).cash_flow("2026-11")
    op = cf["categories"]["operating"]
    assert op["net"] == "1000.00"
    assert op["inflow"] == "1000.00" and op["outflow"] == "0.00"
    line = next(x for x in op["lines"] if x["accountCode"] == "4000")
    assert line["line"] == "销售商品、提供劳务收到的现金"
    assert cf["openingCash"] == "0.00"
    assert cf["closingCash"] == "1000.00"
    assert cf["netIncrease"] == "1000.00"
    assert cf["consistent"] is True
    assert cf["categories"]["investing"]["lines"] == []
    assert cf["categories"]["financing"]["lines"] == []


def test_cash_flow_three_categories_and_self_proof():
    """投资 5000 → 收现 3000 → 购建固定资产 2000：三类净额之和 == 期末 − 期初。"""
    entries = (
        _t("t-capital", "2026-11-01",
           [("1000", "debit", "5000"), ("3000", "credit", "5000")])
        + _t("t-sale", "2026-11-05",
             [("1000", "debit", "3000"), ("4000", "credit", "3000")])
        + _t("t-fa", "2026-11-08",
             [("1601", "debit", "2000"), ("1000", "credit", "2000")])
    )
    cf = _book(entries).cash_flow("2026-11")
    assert cf["categories"]["financing"]["net"] == "5000.00"
    assert cf["categories"]["operating"]["net"] == "3000.00"
    assert cf["categories"]["investing"]["net"] == "-2000.00"
    assert cf["categorizedNet"] == "6000.00"
    assert cf["closingCash"] == "6000.00"
    assert cf["difference"] == "0.00"
    assert cf["consistent"] is True, cf.get("error")
    investing_line = cf["categories"]["investing"]["lines"][0]
    assert investing_line["line"] == "购建固定资产支付的现金"
    assert investing_line["outflow"] == "2000.00"


def test_cash_flow_allocates_multi_counter_by_amount():
    """一次付款 1000，其中费用 800、预付货款 200：逐笔可核，合计仍为 1000。"""
    entries = _t("t-pay", "2026-11-06",
                 [("5200", "debit", "800"), ("1200", "debit", "200"),
                  ("1000", "credit", "1000")])
    cf = _book(entries).cash_flow("2026-11")
    op = cf["categories"]["operating"]
    nets = {l["accountCode"]: l["net"] for l in op["lines"]}
    assert nets == {"5200": "-800.00", "1200": "-200.00"}
    assert {l["line"] for l in op["lines"]} == {
        "支付其他与经营活动有关的现金", "购买商品、接受劳务支付的现金"}
    assert op["outflow"] == "1000.00" and op["net"] == "-1000.00"

    rows = [a for a in cf["attributions"] if a["txnId"] == "t-pay"]
    assert {r["accountCode"]: r["amount"] for r in rows} == {
        "5200": "-800.00", "1200": "-200.00"}
    assert cf["netIncrease"] == "-1000.00" and cf["consistent"] is True


def test_internal_transfer_is_listed_but_excluded_from_categories():
    entries = _t("t-move", "2026-11-07",
                 [("1000", "debit", "900"), ("1000", "credit", "900")])
    cf = _book(entries).cash_flow("2026-11")
    assert cf["internal"] == [{"txnId": "t-move", "inflow": "900.00",
                               "outflow": "900.00", "net": "0.00"}]
    for category in ("operating", "investing", "financing"):
        assert cf["categories"][category]["lines"] == []
        assert cf["categories"][category]["net"] == "0.00"
    assert cf["netIncrease"] == "0.00"
    assert cf["consistent"] is True


def test_off_map_counter_is_not_guessed_and_breaks_self_proof():
    """信用减值损失（6602）不与现金同笔；真出现就不猜——进 unclassified 并判不自证。"""
    entries = _t("t-odd", "2026-11-09",
                 [("6602", "debit", "650"), ("1000", "credit", "650")])
    cf = _book(entries).cash_flow("2026-11")
    assert cf["unclassified"] == ["6602"]
    assert cf["categories"]["operating"]["lines"] == []
    assert cf["consistent"] is False
    assert cf["netIncrease"] == "-650.00"
    assert cf["difference"] == "-650.00"
    assert "error" in cf and "6602" in cf["note"]


def test_cash_leg_without_counterpart_is_a_fragment_not_an_internal_transfer():
    """只有现金行、没有对手行＝交易不完整。绝不能当成「内部调拨」混过去。"""
    cf = _book([_e("1000", "credit", "100")]).cash_flow("2026-11")
    assert cf["internal"] == []
    assert cf["fragments"][0]["net"] == "-100.00"
    assert cf["consistent"] is False
    assert cf["difference"] == "-100.00"


def test_cash_flow_period_follows_booked_on_not_posting_time():
    """10 月 31 日的收现 11 月 2 日才落账：算 10 月；11 月的期初现金是 500。"""
    posted = "2026-11-02T09:00:00+08:00"
    entries = [
        {"account_code": "1000", "direction": "debit", "amount": "500",
         "amount_base": "500", "posted_at": posted, "booked_on": "2026-10-31",
         "txn_id": "t-oct"},
        {"account_code": "4000", "direction": "credit", "amount": "500",
         "amount_base": "500", "posted_at": posted, "booked_on": "2026-10-31",
         "txn_id": "t-oct"},
    ]
    book = _book(entries)
    oct_ = book.cash_flow("2026-10")
    nov = book.cash_flow("2026-11")
    assert oct_["netIncrease"] == "500.00" and oct_["consistent"] is True
    assert nov["openingCash"] == "500.00"
    assert nov["netIncrease"] == "0.00" and nov["consistent"] is True

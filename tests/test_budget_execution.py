"""预算执行整表（change: add-budget-execution 任务 1.1-1.6）。

判据（对应 spec `erp-budget-control` 的「执行报告」要求）：

1. 一张整表：覆盖「有预算 / 有承诺 / 有实际」的并集，不是逐维度值查再拼；
2. 双执行率：承诺率与**实际**率并列；未登记预算时两者为 null（不写 0%、不除零）；
3. 花钱没预算的科目必须点名（`unbudgeted`），不能按 0% 执行率糊过去；
4. `remaining = 预算 − max(承诺, 实际)`，取大者作保守估计（承诺与实际会重叠）；
5. 预警：超支（over）与达到阈值（near）按行点名，标触发口径；
6. 累计口径：预算/承诺按区间求和、实际按区间汇总；月从 1 月、季从首季；
7. 未标维度的实际单列，不摊给任何维度；结转分录不进实际。

零 DB：替身内核只提供 query_entries（同 test_budget_lane 的形状）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.budget import BudgetBook
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.policy import PolicyBook

TENANT = "tpl-supply"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, entries: list[dict] | None = None):
        self._entries = list(entries or [])

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100, **_kw):
        rows = [e for e in self._entries
                if account_code is None or e["account_code"] == account_code]
        if dimensions:
            rows = [e for e in rows
                    if all((e.get("dimensions") or {}).get(k) == v
                           for k, v in dimensions.items())]
        return {"entries": rows[offset:offset + limit]}


def _book(tmp_path, *, dimension="department", period="monthly",
          control="warn-only", entries=None) -> BudgetBook:
    policy = PolicyBook(TENANT, tmp_path / "data")
    want = {"budget_dimension": dimension, "budget_period": period,
            "budget_control": control}
    changes = {k: v for k, v in want.items() if policy.current()["values"][k] != v}
    if changes:
        policy.confirm(policy.propose(changes)["requestId"])
    return BudgetBook(TENANT, tmp_path / "data", _StubKernel(entries), policy)


def _entry(account, direction, amount, when, department=None):
    dims = {"department": department} if department else {}
    return {"account_code": account, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "posted_at": f"{when}T09:00:00+08:00",
            "txn_id": f"t-{account}-{when}-{direction}", "dimensions": dims}


# ---- 1) 整表：三个来源的并集 ----

def test_execution_merges_budget_commitment_and_actual_into_one_table(tmp_path):
    entries = [_entry("5200", "debit", "2500", "2026-11-06", "D-01"),
               _entry("5200", "debit", "1200", "2026-11-07", "D-03")]
    book = _book(tmp_path, entries=entries)
    book.set_budget("D-01", "5200", PERIOD, 10000)          # 有预算无承诺
    book.commit("D-02", "5200", 3000, ref="PO-2",           # 有承诺无预算
                occurred_on="2026-11-05")

    out = book.execution(PERIOD)
    keys = {(r["dimensionValue"], r["accountCode"]) for r in out["rows"]}
    assert keys == {("D-01", "5200"), ("D-02", "5200"), ("D-03", "5200")}
    rows = {(r["dimensionValue"], r["accountCode"]): r for r in out["rows"]}
    assert rows[("D-01", "5200")]["budgetRegistered"] is True
    assert rows[("D-02", "5200")]["budgetRegistered"] is False
    assert rows[("D-03", "5200")]["budgetRegistered"] is False
    assert rows[("D-03", "5200")]["actual"] == "1200.000000"


# ---- 2) 双执行率 ----

def test_dual_rates_and_remaining_use_the_larger_of_commitment_and_actual(tmp_path):
    entries = [_entry("5200", "debit", "2500", "2026-11-06", "D-01")]
    book = _book(tmp_path, entries=entries)
    book.set_budget("D-01", "5200", PERIOD, 10000)
    book.commit("D-01", "5200", 3000, ref="PO-1", occurred_on="2026-11-05")

    row = book.execution(PERIOD)["rows"][0]
    assert row["committedRate"] == "0.3000"
    assert row["actualRate"] == "0.2500"
    assert row["remaining"] == "7000.000000"        # 10000 − max(3000, 2500)
    assert row["overBudget"] is False and row["level"] == "ok"


def test_no_budget_gives_null_rates_not_zero_percent(tmp_path):
    entries = [_entry("5200", "debit", "800", "2026-11-06", "D-09")]
    out = _book(tmp_path, entries=entries).execution(PERIOD)
    row = out["rows"][0]
    assert row["budgetRegistered"] is False
    assert row["committedRate"] is None and row["actualRate"] is None
    assert row["remaining"] is None
    assert row["level"] == "unbudgeted"


# ---- 3) 没预算却花钱要点名 ----

def test_unbudgeted_list_names_the_spend(tmp_path):
    entries = [_entry("5200", "debit", "1200", "2026-11-07", "D-09")]
    book = _book(tmp_path, entries=entries)
    book.commit("D-09", "5200", 200, ref="PO-9", occurred_on="2026-11-05")

    out = book.execution(PERIOD)
    assert out["unbudgeted"] == [{"dimensionValue": "D-09", "accountCode": "5200",
                                  "committed": "200.000000", "actual": "1200.000000"}]
    assert [a["level"] for a in out["alerts"]] == ["unbudgeted"]


# ---- 4) 超支与临近阈值 ----

def test_actual_overrun_is_flagged_with_its_caliber(tmp_path):
    entries = [_entry("5200", "debit", "12000", "2026-11-06", "D-01")]
    book = _book(tmp_path, entries=entries)
    book.set_budget("D-01", "5200", PERIOD, 10000)
    book.commit("D-01", "5200", 2000, ref="PO-1", occurred_on="2026-11-05")

    out = book.execution(PERIOD)
    row = out["rows"][0]
    assert row["remaining"] == "-2000.000000"       # 10000 − max(2000, 12000)
    assert row["overBudget"] is True and row["overBy"] == "actual"
    assert out["alerts"][0]["level"] == "over"


def test_near_threshold_is_reported_but_not_over(tmp_path):
    entries = [_entry("5200", "debit", "9500", "2026-11-06", "D-01")]
    book = _book(tmp_path, entries=entries)
    book.set_budget("D-01", "5200", PERIOD, 10000)

    out = book.execution(PERIOD)
    assert out["rows"][0]["level"] == "near"
    assert out["rows"][0]["overBudget"] is False
    assert out["alerts"][0]["actualRate"] == "0.9500"
    # 阈值可调：把它抬到 0.99 就不该报
    assert book.execution(PERIOD, threshold="0.99")["alerts"] == []


def test_threshold_out_of_range_is_refused(tmp_path):
    with pytest.raises(TemplateError):
        _book(tmp_path).execution(PERIOD, threshold="1.5")


# ---- 5) 累计口径 ----

def test_cumulative_sums_budgets_and_actuals_over_the_year(tmp_path):
    entries = [_entry("5200", "debit", "1000", "2026-01-10", "D-01"),
               _entry("5200", "debit", "2000", "2026-02-10", "D-01"),
               _entry("5200", "debit", "500", "2026-03-10", "D-01"),
               _entry("5200", "debit", "9000", "2026-04-10", "D-01")]  # 区间外
    book = _book(tmp_path, entries=entries)
    for month, amount in (("2026-01", 2000), ("2026-02", 2000), ("2026-03", 2000),
                          ("2026-04", 2000)):
        book.set_budget("D-01", "5200", month, amount)

    out = book.execution("2026-03", cumulative=True)
    assert out["periods"] == ["2026-01", "2026-02", "2026-03"]
    row = out["rows"][0]
    assert row["budget"] == "6000.000000"           # 只算 1..3 月
    assert row["actual"] == "3500.000000"           # 4 月不计
    assert row["actualRate"] == "0.5833"


def test_quarterly_grain_cumulates_from_the_first_quarter(tmp_path):
    entries = [_entry("5200", "debit", "1000", "2026-02-10", "D-01"),
               _entry("5200", "debit", "2000", "2026-08-10", "D-01"),
               _entry("5200", "debit", "4000", "2026-11-10", "D-01")]  # Q4 不计
    book = _book(tmp_path, period="quarterly", entries=entries)
    book.set_budget("D-01", "5200", "2026-Q1", 5000)
    book.set_budget("D-01", "5200", "2026-Q3", 5000)

    out = book.execution("2026-Q3", cumulative=True)
    assert out["periods"] == ["2026-Q1", "2026-Q2", "2026-Q3"]
    row = out["rows"][0]
    assert row["budget"] == "10000.000000"
    assert row["actual"] == "3000.000000"


# ---- 6) 口径纪律 ----

def test_undimensioned_actual_is_listed_separately(tmp_path):
    entries = [_entry("5200", "debit", "700", "2026-11-06"),              # 无维度
               _entry("5200", "debit", "300", "2026-11-07", "D-01")]
    out = _book(tmp_path, entries=entries).execution(PERIOD)
    assert out["unattributed"] == [{"accountCode": "5200", "actual": "700.000000"}]
    assert out["rows"][0]["actual"] == "300.000000"      # 维度行只含标了维度的部分


def test_closing_entries_do_not_zero_the_actuals(tmp_path):
    """结转会把费用科目贷方抹平；算进来「已关账期间的预算实际」就变成 0 了。"""
    entries = [_entry("5200", "debit", "4000", "2026-11-06", "D-01"),
               {"account_code": "5200", "direction": "credit", "amount": "4000",
                "amount_base": "4000", "booked_on": "2026-11-30",
                "posted_at": "2026-11-30T09:00:00+08:00", "txn_id": "t-close",
                "source_ref": "close:2026-11", "dimensions": {"department": "D-01"}}]
    book = _book(tmp_path, entries=entries)
    book.set_budget("D-01", "5200", PERIOD, 10000)
    assert book.execution(PERIOD)["rows"][0]["actual"] == "4000.000000"

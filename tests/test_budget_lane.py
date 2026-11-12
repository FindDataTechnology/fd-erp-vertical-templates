"""预算控制车道（change: add-budget-control-lane 任务 1.1-1.5）。

判据（对应 spec `erp-budget-control`）：

1. 维度/期间跟随口径：维度键与粒度不符即拒；
2. 预算登记须人工拍板，修订追加式（旧记录留档）；
3. 三种控制方式：hard-block 拒且不写承诺 / warn-only 放行标记 / post-audit 不标；
4. 承诺按 ref 幂等，释放须带原因且恢复额度；
5. 查询分开列示承诺与**科目层**实际，实际来自内核分行且明说不区分维度。

零 DB：替身内核只提供 query_entries。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.budget import (BudgetBook, kernel_dimension_key,
                                              period_key, period_range)
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.policy import PolicyBook

TENANT = "tpl-supply"
DEPT = "D-01"
ACC = "5200"


class _StubKernel:
    """分录替身：按 account_code 过滤，offset/limit 分页。"""

    def __init__(self, entries: list[dict] | None = None):
        self._entries = list(entries or [])

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100, **_kw):
        rows = [e for e in self._entries
                if account_code is None or e["account_code"] == account_code]
        if dimensions:
            # 与内核 jsonb 包含语义同口径：给几个键就要求全部命中
            rows = [e for e in rows
                    if all((e.get("dimensions") or {}).get(k) == v
                           for k, v in dimensions.items())]
        return {"entries": rows[offset:offset + limit]}


def _book(tmp_path, *, dimension="department", period="monthly",
          control="warn-only", entries=None):
    policy = PolicyBook(TENANT, tmp_path / "data")
    want = {"budget_dimension": dimension, "budget_period": period,
            "budget_control": control}
    changes = {k: v for k, v in want.items()
               if policy.current()["values"][k] != v}
    if changes:
        policy.confirm(policy.propose(changes)["requestId"])
    return BudgetBook(TENANT, tmp_path / "data", _StubKernel(entries), policy)


def _entry(account, direction, amount, posted_at, dimensions=None):
    return {"account_code": account, "direction": direction,
            "amount": amount, "amount_base": amount, "posted_at": posted_at,
            "dimensions": dimensions or {}}


# ---- 1) 期间键与粒度 ----

def test_period_key_follows_grain():
    from datetime import date
    assert period_key(date(2026, 11, 13), "monthly") == "2026-11"
    assert period_key(date(2026, 11, 13), "quarterly") == "2026-Q4"
    assert period_key(date(2026, 2, 1), "quarterly") == "2026-Q1"
    assert period_range("2026-11", "monthly")[0].isoformat() == "2026-11-01"
    assert period_range("2026-Q4", "quarterly")[1].isoformat() == "2027-01-01"


def test_quarter_grain_refuses_a_month_key(tmp_path):
    book = _book(tmp_path, period="quarterly")
    with pytest.raises(TemplateError) as ei:
        book.set_budget(DEPT, ACC, "2026-11", 10000)
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "YYYY-Qn" in ei.value.message
    assert book.set_budget(DEPT, ACC, "2026-Q4", 10000)["budget"]["period"] == "2026-Q4"


def test_month_grain_refuses_a_quarter_key(tmp_path):
    book = _book(tmp_path)
    with pytest.raises(TemplateError) as ei:
        book.set_budget(DEPT, ACC, "2026-Q4", 10000)
    assert "YYYY-MM" in ei.value.message


# ---- 2) 预算登记须人工、修订追加式 ----

def test_program_must_not_set_budget(tmp_path):
    book = _book(tmp_path)
    with pytest.raises(TemplateError) as ei:
        book.set_budget(DEPT, ACC, "2026-11", 10000, approved_by="system")
    assert "人工拍板" in ei.value.message
    with pytest.raises(TemplateError):
        book.set_budget(DEPT, ACC, "2026-11", 10000, approved_by="budget-engine")
    assert book.budgets() == []


def test_revision_appends_and_keeps_history(tmp_path):
    book = _book(tmp_path)
    first = book.set_budget(DEPT, ACC, "2026-11", 8000)
    assert first["revised"] is False and first["previousAmount"] is None
    again = book.set_budget(DEPT, ACC, "2026-11", 12000)
    assert again["revised"] is True and again["previousAmount"] == "8000.000000"
    rows = book.budgets("2026-11")
    assert len(rows) == 1 and rows[0]["amount"] == "12000.000000"   # 生效后者
    # 历史留档：档内两条都还在
    assert len([r for r in book._records("budget")]) == 2


def test_negative_budget_refused(tmp_path):
    book = _book(tmp_path)
    with pytest.raises(TemplateError) as ei:
        book.set_budget(DEPT, ACC, "2026-11", -1)
    assert "不能为负" in ei.value.message


def test_dimension_value_required(tmp_path):
    book = _book(tmp_path)
    with pytest.raises(TemplateError) as ei:
        book.set_budget("   ", ACC, "2026-11", 1000)
    assert "维度值不能为空" in ei.value.message


# ---- 3) 三种控制方式 ----

def test_hard_block_refuses_and_writes_nothing(tmp_path):
    book = _book(tmp_path, control="hard-block")
    book.set_budget(DEPT, ACC, "2026-11", 10000)
    book.commit(DEPT, ACC, 9000, ref="PO-1", occurred_on="2026-11-05")
    with pytest.raises(TemplateError) as ei:
        book.commit(DEPT, ACC, 2000, ref="PO-2", occurred_on="2026-11-06")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "超出预算" in ei.value.message
    assert ei.value.detail["remaining"] == "1000.000000"
    assert ei.value.detail["requested"] == "2000.000000"
    # 拒绝不得留下承诺
    assert list(book._commit_state()["active"]) == ["PO-1"]


def test_warn_only_lets_it_through_with_a_flag(tmp_path):
    book = _book(tmp_path, control="warn-only")
    book.set_budget(DEPT, ACC, "2026-11", 10000)
    book.commit(DEPT, ACC, 9000, ref="PO-1", occurred_on="2026-11-05")
    out = book.commit(DEPT, ACC, 2000, ref="PO-2", occurred_on="2026-11-06")
    assert out["duplicate"] is False
    assert out["overBudget"] is True
    assert out["overAmount"] == "1000.000000"
    assert "warn-only" in out["warning"]
    assert out["remaining"] == "-1000.000000"


def test_post_audit_passes_without_a_flag(tmp_path):
    book = _book(tmp_path, control="post-audit")
    book.set_budget(DEPT, ACC, "2026-11", 1000)
    out = book.commit(DEPT, ACC, 5000, ref="PO-9", occurred_on="2026-11-20")
    assert out["overBudget"] is False
    assert "post-audit" in out["note"]
    status = book.status(DEPT, "2026-11")
    assert status["rows"][0]["overBudget"] is True     # 超支只在查询里看得见


def test_no_budget_counts_as_zero(tmp_path):
    book = _book(tmp_path, control="hard-block")
    with pytest.raises(TemplateError) as ei:
        book.commit(DEPT, ACC, 1, ref="PO-X", occurred_on="2026-11-02")
    assert ei.value.detail["budgetRegistered"] is False
    assert ei.value.detail["budget"] == "0.000000"


def test_commit_requires_positive_amount_and_ref(tmp_path):
    book = _book(tmp_path)
    with pytest.raises(TemplateError) as ei:
        book.commit(DEPT, ACC, 10, ref="  ", occurred_on="2026-11-02")
    assert "单据号" in ei.value.message
    with pytest.raises(TemplateError) as ei:
        book.commit(DEPT, ACC, 0, ref="PO-1", occurred_on="2026-11-02")
    assert "必须大于 0" in ei.value.message


# ---- 4) 幂等与释放 ----

def test_commit_is_idempotent_by_ref(tmp_path):
    book = _book(tmp_path, control="hard-block")
    book.set_budget(DEPT, ACC, "2026-11", 10000)
    first = book.commit(DEPT, ACC, 3000, ref="PO-1", occurred_on="2026-11-05")
    again = book.commit(DEPT, ACC, 3000, ref="PO-1", occurred_on="2026-11-05")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert again["remaining"] == first["remaining"]
    assert book.status(DEPT, "2026-11")["rows"][0]["committed"] == "3000.000000"


def test_release_restores_the_quota(tmp_path):
    book = _book(tmp_path, control="hard-block")
    book.set_budget(DEPT, ACC, "2026-11", 10000)
    book.commit(DEPT, ACC, 3000, ref="PO-1", occurred_on="2026-11-05")
    out = book.release("PO-1", reason="采购单退回")
    assert out["duplicate"] is False and out["reason"] == "采购单退回"
    row = book.status(DEPT, "2026-11")["rows"][0]
    assert row["committed"] == "0.000000"
    assert row["remaining"] == "10000.000000"
    assert book.release("PO-1", reason="再释放")["duplicate"] is True


def test_release_requires_reason_and_known_ref(tmp_path):
    book = _book(tmp_path)
    book.set_budget(DEPT, ACC, "2026-11", 1000)
    book.commit(DEPT, ACC, 100, ref="PO-1", occurred_on="2026-11-05")
    with pytest.raises(TemplateError) as ei:
        book.release("PO-1", reason="   ")
    assert "须说明原因" in ei.value.message
    with pytest.raises(TemplateError) as ei:
        book.release("PO-NOPE", reason="不存在")
    assert ei.value.code == "NOT_FOUND"


def test_released_ref_cannot_be_reused(tmp_path):
    book = _book(tmp_path)
    book.set_budget(DEPT, ACC, "2026-11", 1000)
    book.commit(DEPT, ACC, 100, ref="PO-1", occurred_on="2026-11-05")
    book.release("PO-1", reason="退回")
    again = book.commit(DEPT, ACC, 100, ref="PO-1", occurred_on="2026-11-05")
    assert again["duplicate"] is True
    assert "已释放" in again["note"]


# ---- 5) 执行查询：承诺与科目层实际分开 ----

def test_status_shows_budget_committed_and_utilization(tmp_path):
    book = _book(tmp_path)
    book.set_budget(DEPT, ACC, "2026-11", 10000)
    book.commit(DEPT, ACC, 3000, ref="PO-1", occurred_on="2026-11-05")
    status = book.status(DEPT, "2026-11")
    assert status["dimension"] == "department"
    assert status["grain"] == "monthly"
    row = status["rows"][0]
    assert row["budget"] == "10000.000000"
    assert row["committed"] == "3000.000000"
    assert row["remaining"] == "7000.000000"
    assert row["utilization"] == "0.3000"
    assert status["totals"]["remaining"] == "7000.000000"


DIM = {"department": DEPT}


def test_status_actual_comes_from_kernel_entries_in_period(tmp_path):
    entries = [
        _entry(ACC, "debit", "2500", "2026-11-10 09:00:00+08:00", DIM),
        _entry(ACC, "debit", "500", "2026-11-30 23:00:00+08:00", DIM),
        _entry(ACC, "credit", "1000", "2026-11-15 09:00:00+08:00", DIM),
        _entry(ACC, "debit", "999", "2026-12-01 00:00:00+08:00", DIM),  # 期间外
        _entry("4000", "credit", "777", "2026-11-15 09:00:00+08:00", DIM),  # 别的科目
    ]
    book = _book(tmp_path, entries=entries)
    book.set_budget(DEPT, ACC, "2026-11", 10000)
    status = book.status(DEPT, "2026-11")
    row = status["rows"][0]
    assert row["actual"] == "2000.000000"      # 2500 + 500 − 1000
    assert row["actualEntries"] == 3
    assert row["unattributedActual"] == "0.000000"
    assert "按同一维度汇总" in status["caliber_note"]


def test_unattributed_actual_is_reported_separately(tmp_path):
    """没标维度的发生额不许被静默漏掉：单列出来，且不计入任何维度。"""
    entries = [
        _entry(ACC, "debit", "700", "2026-11-10 09:00:00+08:00", DIM),
        _entry(ACC, "debit", "300", "2026-11-11 09:00:00+08:00"),        # 未标注
        _entry(ACC, "credit", "100", "2026-11-12 09:00:00+08:00"),       # 未标注
    ]
    book = _book(tmp_path, entries=entries)
    book.set_budget(DEPT, ACC, "2026-11", 10000)
    status = book.status(DEPT, "2026-11")
    row = status["rows"][0]
    assert row["actual"] == "700.000000"                 # 只算本维度
    assert row["unattributedActual"] == "200.000000"     # 300 − 100
    assert status["totals"]["unattributedActual"] == "200.000000"
    assert "别把它悄悄漏掉" in status["caliber_note"]


def test_status_without_entries_reports_zero_not_an_error(tmp_path):
    book = _book(tmp_path)
    book.set_budget(DEPT, ACC, "2026-11", 1000)
    row = book.status(DEPT, "2026-11")["rows"][0]
    assert row["actual"] == "0.000000" and row["actualEntries"] == 0


def test_status_includes_accounts_with_commit_but_no_budget(tmp_path):
    book = _book(tmp_path, control="warn-only")
    book.commit(DEPT, ACC, 500, ref="PO-1", occurred_on="2026-11-05")
    row = book.status(DEPT, "2026-11")["rows"][0]
    assert row["budget"] is None
    assert row["budgetRegistered"] is False
    assert row["remaining"] == "-500.000000"


def test_status_refuses_empty_dimension_value(tmp_path):
    book = _book(tmp_path)
    with pytest.raises(TemplateError) as ei:
        book.status("  ", "2026-11")
    assert "维度值不能为空" in ei.value.message


def test_budget_dimension_switch_rejects_old_key_semantics(tmp_path):
    """切到 cost-center 后，旧维度的承诺仍在档内可查，但新登记按新口径落。"""
    policy = PolicyBook(TENANT, tmp_path / "data")
    book = BudgetBook(TENANT, tmp_path / "data", _StubKernel([]), policy)
    assert policy.current()["values"]["budget_dimension"] == "cost-center"
    book.set_budget("CC-1", ACC, "2026-11", 1000)
    rows = book.budgets("2026-11")
    assert rows[0]["dimension"] == "cost-center"


# ---- 6) 口径词表 → 内核维度键的映射 ----

def test_policy_dimension_values_map_to_kernel_keys():
    """口径用 kebab-case、内核维度键用 snake_case：必须显式映射，不能靠字符串巧合。

    这条是实打实的缺陷回归：不映射时 `cost-center` 传给内核永远匹配不到，
    actual 恒为 0——而报表上只会显示「这期没花钱」。
    """
    assert kernel_dimension_key("cost-center") == "cost_center"
    assert kernel_dimension_key("department") == "department"
    assert kernel_dimension_key("project") == "project"
    assert kernel_dimension_key("channel") == "channel"
    with pytest.raises(TemplateError) as ei:
        kernel_dimension_key("dept")
    assert ei.value.detail["allowed"]


def test_status_filters_with_the_kernel_key(tmp_path):
    """默认口径是 cost-center；带 cost_center 维度的分录必须被算进来。"""
    entries = [_entry(ACC, "debit", "800", "2026-11-10 09:00:00+08:00",
                      {"cost_center": "CC-1"})]
    book = _book(tmp_path, entries=entries, dimension="cost-center")  # 册内默认
    book.set_budget("CC-1", ACC, "2026-11", 10000)
    status = book.status("CC-1", "2026-11")
    assert status["dimension"] == "cost-center"
    assert status["kernelDimensionKey"] == "cost_center"
    assert status["rows"][0]["actual"] == "800.000000"

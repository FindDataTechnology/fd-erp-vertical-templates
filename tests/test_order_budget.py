"""下单预算闸（agent 自主下单的上限）。

`order_budget.py` 是唯一一处「agent 花钱」的闸：委托有额度就硬上限，
没额度只受租户约束，账本读不到就 fail-closed。这组测试守三件事：
  1. 判定是纯函数，各种边界都能算清
  2. 金额只能来自服务端取价（调用方声明无效——由 accept_prepaid_order 的结构保证）
  3. 账本出问题一律拒绝，不降级成放行
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.order_budget import (
    BudgetScope,
    check_order_budget,
    enforce_order_budget,
)


# ---- 纯判定 ----

def test_no_budget_means_tenant_bounded_only():
    """没有委托额度 ⇒ 放行（只受租户额度约束，与 registry 的 grant_budget 同语义）。"""
    for empty in (None, "", {}):
        v = check_order_budget(budget=empty, used="0", order_total="999999")
        assert v.allowed is True
        assert "tenant_bounded" in v.reason


def test_within_budget_allowed_and_remaining_computed():
    v = check_order_budget(budget="5000", used="1200", order_total="800")
    assert v.allowed is True
    assert v.limit == "5000"
    assert v.used == "1200"
    assert v.remaining == "3000"      # 5000 - 1200 - 800


def test_exactly_at_limit_is_allowed():
    v = check_order_budget(budget="1000", used="600", order_total="400")
    assert v.allowed is True
    assert v.remaining == "0"


def test_one_cent_over_is_rejected():
    v = check_order_budget(budget="1000", used="600", order_total="400.01")
    assert v.allowed is False
    assert v.reason == "GRANT_BUDGET_EXHAUSTED"
    assert v.remaining == "400"


def test_already_spent_rejected_even_for_zero_order():
    """额度已用完时，哪怕 0 元单也不放行——0 > 剩余(-x) 为真，但剩余为负要如实报。"""
    v = check_order_budget(budget="100", used="150", order_total="0.01")
    assert v.allowed is False
    assert v.remaining == "0"          # 负余额按 0 报，不显示负数


def test_negative_total_rejected():
    v = check_order_budget(budget="1000", used="0", order_total="-1")
    assert v.allowed is False
    assert "负" in v.reason


def test_malformed_budget_rejected_not_ignored():
    """非法 budget 值必须拒，不能当成「没额度」放行。"""
    v = check_order_budget(budget="五千", used="0", order_total="1")
    assert v.allowed is False


# ---- 账本交互 ----

class FakeLedger:
    def __init__(self, spent="0", fail_read=False, fail_consume=False):
        self._spent = Decimal(str(spent))
        self._fail_read = fail_read
        self._fail_consume = fail_consume
        self.consumed: list[tuple[str, Decimal]] = []

    def spent(self, key):
        if self._fail_read:
            raise RuntimeError("mongo down")
        return self._spent

    def consume(self, key, amount):
        if self._fail_consume:
            raise RuntimeError("write failed")
        self.consumed.append((key, amount))
        self._spent += amount


def test_enforce_consumes_on_admit():
    led = FakeLedger(spent="100")
    v = enforce_order_budget(led, scope=BudgetScope("jti-1", "run-1"),
                             budget="1000", order_total="300")
    assert v.allowed is True
    assert led.consumed == [("jti-1|run-1", Decimal("300"))]


def test_enforce_does_not_consume_on_reject():
    led = FakeLedger(spent="900")
    v = enforce_order_budget(led, scope=BudgetScope("jti-1"), budget="1000",
                             order_total="200")
    assert v.allowed is False
    assert led.consumed == []


def test_unlimited_grant_does_not_touch_ledger():
    """没有 budget 的委托不记账——它受租户额度约束，不由本层管。"""
    led = FakeLedger()
    v = enforce_order_budget(led, scope=BudgetScope("jti-1"), budget=None,
                             order_total="300")
    assert v.allowed is True
    assert led.consumed == []


def test_ledger_read_failure_fails_closed():
    led = FakeLedger(fail_read=True)
    with pytest.raises(TemplateError) as ei:
        enforce_order_budget(led, scope=BudgetScope("jti-1", "run-1"),
                             budget="1000", order_total="100")
    assert ei.value.code == "RUNTIME_UNREACHABLE"
    assert "fail-closed" in ei.value.message


def test_consume_failure_fails_closed():
    """记账失败也拒——否则「过闸但不记账」= 额度可被无限绕过。"""
    led = FakeLedger(fail_consume=True)
    with pytest.raises(TemplateError) as ei:
        enforce_order_budget(led, scope=BudgetScope("jti-1", "run-1"),
                             budget="1000", order_total="100")
    assert "else 额度可被绕过" in ei.value.message or "额度可被绕过" in ei.value.message


def test_scope_key_distinguishes_runs():
    assert BudgetScope("a", "r1").key() != BudgetScope("a", "r2").key()
    assert BudgetScope("a").key() == "a|-"

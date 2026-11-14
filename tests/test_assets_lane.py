"""固定资产车道（change: add-fixed-assets-lane 任务 2.1-2.6）。

判据（对应 spec `erp-fixed-assets`）：

1. 登记受资本化门槛约束：低于门槛拒绝并说明「应费用化」；同标识幂等；
2. 三种折旧方法各自成立，双倍余额递减末期切直线并精确收口残值；
3. 期末计提走确认门（pending proposal、借贷同额）、同期间幂等、提足不再计提；
4. 工作量法缺产量整期拒绝并点名资产；
5. 净值 = 原值 − 累计折旧，且与计划可复算；
6. 台账归属口径非 platform 如实 NOT_WIRED。

零 DB：替身内核只提供 submit_proposal。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.assets import FixedAssetLane
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.policy import PolicyBook

TENANT = "tpl-supply"


class _StubKernel:
    """提案替身：同 stable_id 幂等返回首次结果（与内核语义一致）。"""

    def __init__(self, status: str = "pending"):
        self.status = status
        self.calls: list[dict] = []
        self._seen: dict[str, dict] = {}

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        lane_title=None, submitted_by="agent", **_kw):
        self.calls.append({"stable_id": stable_id, "lines": lines,
                           "sourceRef": source_ref, "kind": kind,
                           "laneTitle": lane_title, "submittedBy": submitted_by})
        if stable_id in self._seen:
            return {**self._seen[stable_id], "duplicate": True}
        out = {"proposal_id": len(self._seen) + 1, "stable_id": stable_id,
               "status": self.status, "duplicate": False, "lines": lines}
        self._seen[stable_id] = out
        return {**out}


def _lane(tmp_path, *, method="straight-line", owner="platform", kernel=None):
    book = PolicyBook(TENANT, tmp_path / "data")
    changes = {}
    if book.current()["values"]["depreciation_method"] != method:
        changes["depreciation_method"] = method
    if book.current()["values"]["asset_ledger_owner"] != owner:
        changes["asset_ledger_owner"] = owner
    if changes:
        book.confirm(book.propose(changes)["requestId"])
    return FixedAssetLane(TENANT, tmp_path / "data", kernel or _StubKernel(), book)


def _register(lane, asset_id="FA-1", *, cost=12000, salvage=0, months=12,
              acquired="2026-01-15", **kw):
    return lane.register(asset_id, kw.pop("name", "数控机床"), cost=cost,
                         salvage=salvage, useful_life_months=months,
                         acquired_on=acquired, **kw)


# ---- 1) 资本化门槛 ----

def test_no_threshold_means_no_gate(tmp_path):
    lane = _lane(tmp_path)
    assert lane.threshold() is None
    out = _register(lane, cost=3000)
    assert out["duplicate"] is False
    assert out["asset"]["cost"] == "3000.00"


def test_program_must_not_set_threshold(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.set_threshold(5000, approved_by="system")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "人工登记" in ei.value.message
    with pytest.raises(TemplateError):
        lane.set_threshold(5000, approved_by="asset-engine")
    assert lane.threshold() is None  # 两次都不得落下任何门槛


def test_below_threshold_is_expensed_not_capitalized(tmp_path):
    lane = _lane(tmp_path)
    lane.set_threshold(5000)
    with pytest.raises(TemplateError) as ei:
        _register(lane, cost=3000)
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "应费用化" in ei.value.message
    assert ei.value.detail["treatment"] == "expense"
    assert lane.assets() == []


def test_cost_equal_to_threshold_is_capitalizable(tmp_path):
    lane = _lane(tmp_path)
    lane.set_threshold(3000)
    assert _register(lane, cost=3000)["duplicate"] is False


def test_threshold_currency_mismatch_refuses_instead_of_converting(tmp_path):
    lane = _lane(tmp_path)
    lane.set_threshold(5000, currency="CNY")
    with pytest.raises(TemplateError) as ei:
        _register(lane, cost=1000, currency="USD")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "不做汇率换算" in ei.value.message
    assert ei.value.detail["asset_currency"] == "USD"


# ---- 2) 登记校验与幂等 ----

@pytest.mark.parametrize("kwargs,needle", [
    ({"cost": 0}, "取得成本必须大于 0"),
    ({"cost": -100}, "取得成本必须大于 0"),
    ({"salvage": 12000}, "残值必须小于取得成本"),
    ({"months": 0}, "使用月数须为正整数"),
    ({"months": 1.5}, "使用月数须为正整数"),
    ({"acquired": "2026-13-01"}, "取得日期不是合法日期"),
])
def test_register_validation(tmp_path, kwargs, needle):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        _register(lane, **kwargs)
    assert ei.value.code == "INVALID_ARGUMENT"
    assert needle in ei.value.message


def test_expense_account_must_be_a_kernel_expense_account(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        _register(lane, expense_account="1602")
    assert ei.value.detail["allowed"] == ["5000", "5200"]


def test_method_outside_book_refused(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        _register(lane, method="sum-of-years")
    assert "折旧方法不在册" in ei.value.message


def test_duplicate_register_keeps_one_asset(tmp_path):
    lane = _lane(tmp_path)
    first = _register(lane)
    again = _register(lane, cost=9999)
    assert first["duplicate"] is False and again["duplicate"] is True
    assert len(lane.assets()) == 1
    assert again["asset"]["cost"] == "12000.00"  # 首次登记为真，重登不覆盖


# ---- 3) 三种折旧方法 ----

def test_straight_line_first_period_is_1000(tmp_path):
    lane = _lane(tmp_path)
    _register(lane)
    plan = lane.plan("FA-1")["periods"]
    assert plan[0]["period"] == "2026-02"      # 次月起提
    assert plan[0]["amount"] == "1000.00"
    assert len(plan) == 12
    assert Decimal(plan[-1]["cumulative"]) == Decimal("12000")
    assert plan[-1]["netBookValue"] == "0.00"


def test_double_declining_first_period_is_2000(tmp_path):
    lane = _lane(tmp_path)
    _register(lane, method="double-declining")
    plan = lane.plan("FA-1")["periods"]
    assert plan[0]["amount"] == "2000.00"      # 12000 × 2/12
    assert plan[0]["method"] == "double-declining"
    assert Decimal(plan[-1]["cumulative"]) == Decimal("12000")  # 末期精确收口
    amounts = [Decimal(r["amount"]) for r in plan]
    assert all(a > 0 for a in amounts)
    assert amounts == sorted(amounts, reverse=True)  # 前高后低，切换后递减或持平


def test_double_declining_never_dips_below_salvage(tmp_path):
    lane = _lane(tmp_path)
    _register(lane, salvage=1200, method="double-declining")
    plan = lane.plan("FA-1")["periods"]
    assert all(Decimal(r["netBookValue"]) >= Decimal("1200") for r in plan)
    assert plan[-1]["netBookValue"] == "1200.00"
    assert Decimal(plan[-1]["cumulative"]) == Decimal("10800")


def test_straight_line_rounds_to_cents_and_last_period_absorbs(tmp_path):
    lane = _lane(tmp_path)
    _register(lane, cost=1000, months=3)
    amounts = [Decimal(r["amount"]) for r in lane.plan("FA-1")["periods"]]
    assert amounts[:2] == [Decimal("333.33"), Decimal("333.33")]
    assert amounts[2] == Decimal("333.34")
    assert sum(amounts) == Decimal("1000")


def test_asset_method_overrides_the_policy_book(tmp_path):
    lane = _lane(tmp_path)  # 口径 = straight-line
    _register(lane, method="double-declining")
    plan = lane.plan("FA-1")
    assert plan["method"] == "double-declining"
    assert plan["policySource"] == "asset"


def test_plan_follows_the_policy_book_when_asset_is_silent(tmp_path):
    lane = _lane(tmp_path, method="double-declining")
    _register(lane)
    plan = lane.plan("FA-1")
    assert plan["method"] == "double-declining"
    assert plan["policySource"] == "policy-book"


# ---- 4) 工作量法 ----

def test_units_method_without_total_output_refuses_and_names_the_asset(tmp_path):
    lane = _lane(tmp_path, method="units-of-production")
    _register(lane, months=12)
    with pytest.raises(TemplateError) as ei:
        lane.depreciate("2026-02", units={"FA-1": 100})
    assert ei.value.code == "INVALID_ARGUMENT"
    assert ei.value.detail["missing_units_total"] == ["FA-1"]
    assert "预计总产量" in ei.value.message


def test_units_method_missing_period_output_refuses_whole_period(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, method="units-of-production", kernel=kernel)
    _register(lane, units_total=1000)
    with pytest.raises(TemplateError) as ei:
        lane.depreciate("2026-02")
    assert ei.value.detail["missing_units"] == ["FA-1"]
    assert "缺少当期产量" in ei.value.message
    assert kernel.calls == []  # 整期拒绝：一张 proposal 都不出


def test_units_method_amount_follows_output_and_caps_at_salvage(tmp_path):
    lane = _lane(tmp_path, method="units-of-production")
    _register(lane, units_total=1000)
    out = lane.depreciate("2026-02", units={"FA-1": 100})
    assert out["total"] == "1200.00"           # 12000 × 100/1000
    over = lane.depreciate("2026-03", units={"FA-1": 5000})
    assert over["total"] == "10800.00"         # 封顶到剩余可折旧额
    assert lane.book_value("FA-1")["netBookValue"] == "0.00"
    later = lane.depreciate("2026-04", units={"FA-1": 10})
    assert later["assets"] == [] and later["total"] == "0.00"


def test_units_method_negative_output_refused(tmp_path):
    lane = _lane(tmp_path, method="units-of-production")
    _register(lane, units_total=1000)
    with pytest.raises(TemplateError) as ei:
        lane.depreciate("2026-02", units={"FA-1": -5})
    assert "不能为负" in ei.value.message


# ---- 5) 期末计提：确认门 + 幂等 + 提足 ----

def test_depreciation_is_a_pending_balanced_proposal(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel=kernel)
    _register(lane)
    out = lane.depreciate("2026-02")
    assert out["proposal"]["status"] == "pending"     # 只到确认门，不落账
    assert out["total"] == "1000.00"
    call = kernel.calls[0]
    assert call["stable_id"] == "depr:2026-02"
    debits = sum(Decimal(l["amount"]) for l in call["lines"]
                 if l["direction"] == "debit")
    credits = sum(Decimal(l["amount"]) for l in call["lines"]
                  if l["direction"] == "credit")
    assert debits == credits == Decimal("1000")
    assert {l["account_code"] for l in call["lines"]} == {"5200", "1602"}


def test_same_period_is_idempotent(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel=kernel)
    _register(lane)
    first = lane.depreciate("2026-02")
    again = lane.depreciate("2026-02")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert again["total"] == first["total"]
    assert len(kernel.calls) == 1                    # 第二次连内核都没打
    assert lane.book_value("FA-1")["accumulated"] == "1000.00"  # 账上只有一份


def test_fully_depreciated_asset_leaves_the_detail(tmp_path):
    lane = _lane(tmp_path)
    _register(lane, cost=1000, months=2)
    assert lane.depreciate("2026-02")["total"] == "500.00"
    assert lane.depreciate("2026-03")["total"] == "500.00"
    after = lane.depreciate("2026-04")
    assert after["assets"] == []
    assert "已提足" in after["note"] or "无可计提" in after["note"]
    assert lane.book_value("FA-1")["fullyDepreciated"] is True


def test_asset_not_yet_depreciating_is_skipped(tmp_path):
    lane = _lane(tmp_path)
    _register(lane, acquired="2026-05-20")     # 首提期 2026-06
    out = lane.depreciate("2026-05")
    assert out["assets"] == [] and out["proposal"] is None


def test_expense_account_flows_into_the_debit_line(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel=kernel)
    _register(lane, expense_account="5000")
    lane.depreciate("2026-02")
    assert {l["account_code"] for l in kernel.calls[0]["lines"]} == {"5000", "1602"}


# ---- 6) 作废（退回后重提） ----

def test_void_requires_reason_and_unknown_period(tmp_path):
    lane = _lane(tmp_path)
    _register(lane)
    lane.depreciate("2026-02")
    with pytest.raises(TemplateError) as ei:
        lane.void("2026-02", reason="   ")
    assert "须说明原因" in ei.value.message
    with pytest.raises(TemplateError) as ei:
        lane.void("2026-09", reason="提错期间")
    assert ei.value.code == "NOT_FOUND"


def test_void_lets_the_same_period_be_reissued_under_a_new_stable_id(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel=kernel)
    _register(lane)
    lane.depreciate("2026-02")
    lane.void("2026-02", reason="金额填错，退回重提")
    assert lane.book_value("FA-1")["accumulated"] == "0.00"   # 作废后不计
    again = lane.depreciate("2026-02")
    assert again["duplicate"] is False
    assert kernel.calls[-1]["stable_id"] == "depr:2026-02#2"  # 换键才真重提
    assert lane.book_value("FA-1")["accumulated"] == "1000.00"
    assert lane.void("2026-02", reason="再作废一次")["duplicate"] is False


def test_void_refuses_on_a_confirmed_proposal(tmp_path):
    lane = _lane(tmp_path, kernel=_StubKernel(status="confirmed"))
    _register(lane)
    lane.depreciate("2026-02")
    with pytest.raises(TemplateError) as ei:
        lane.void("2026-02", reason="想改")
    assert "先在内核冲正" in ei.value.message


# ---- 7) 净值、计划、台账摘要 ----

def test_book_value_equals_cost_minus_three_periods(tmp_path):
    lane = _lane(tmp_path)
    _register(lane)
    for period in ("2026-02", "2026-03", "2026-04"):
        lane.depreciate(period)
    bv = lane.book_value("FA-1")
    assert bv["accumulated"] == "3000.00"
    assert bv["netBookValue"] == "9000.00"
    assert bv["asOf"] is None
    plan = lane.plan("FA-1")["periods"]
    assert sum(1 for r in plan if r["booked"]) == 3
    assert plan[0]["bookedAmount"] == "1000.00"
    assert plan[3]["booked"] is False
    # 净值与计划可复算：第 3 期末的计划净值就是账面净值
    assert plan[2]["netBookValue"] == bv["netBookValue"]


def test_book_value_as_of_ignores_later_periods(tmp_path):
    lane = _lane(tmp_path)
    _register(lane)
    for period in ("2026-02", "2026-03", "2026-04"):
        lane.depreciate(period)
    assert lane.book_value("FA-1", as_of="2026-03")["accumulated"] == "2000.00"
    assert lane.book_value("FA-1", as_of="2026-03-31")["accumulated"] == "2000.00"
    assert lane.book_value("FA-1", as_of="2026-01")["accumulated"] == "0.00"


def test_book_value_unknown_asset_is_not_found(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(TemplateError) as ei:
        lane.book_value("NOPE")
    assert ei.value.code == "NOT_FOUND"


def test_summary_totals_match_the_assets(tmp_path):
    lane = _lane(tmp_path)
    _register(lane, "FA-1", cost=12000, months=12)
    _register(lane, "FA-2", cost=6000, months=6)
    lane.depreciate("2026-02")
    s = lane.summary()
    assert s["assetCount"] == 2
    assert s["totalCost"] == "18000.00"
    assert s["totalAccumulated"] == "2000.00"        # 1000 + 1000
    assert s["totalNetBookValue"] == "16000.00"
    assert s["policy"]["depreciation_method"] == "straight-line"


# ---- 8) 台账归属口径 ----

@pytest.mark.parametrize("owner", ["customer-readonly", "customer-writeback"])
def test_non_platform_ledger_owner_is_not_wired(tmp_path, owner):
    kernel = _StubKernel()
    lane = _lane(tmp_path, owner=owner, kernel=kernel)
    _register(lane)
    with pytest.raises(TemplateError) as ei:
        lane.depreciate("2026-02")
    assert ei.value.code == "NOT_WIRED"
    assert owner in ei.value.message
    assert kernel.calls == []                        # 一张 proposal 都不出


# ---- 9) 预演：只算不写 ----

def test_preview_computes_without_writing(tmp_path):
    """月末「这期该提多少」是看一眼的事，看一眼不该在账上留提案。"""
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel=kernel)
    _register(lane)                                 # 12000 / 12 月 = 1000/月
    events_before = len(lane._log.events())

    out = lane.depreciate("2026-02", preview=True)
    assert out["preview"] is True and out["proposed"] is False
    assert out["total"] == "1000.00"
    assert out["proposal"] is None
    assert out["blockers"] == [] and out["canSubmit"] is True
    assert kernel.calls == []                        # 一张提案都没出
    assert len(lane._log.events()) == events_before   # 事件档没多一行

    # 预演过的这期真计提照样成立，且金额与预演逐值相同
    real = lane.depreciate("2026-02")
    assert real["proposed"] is True and real["total"] == out["total"]


def test_preview_lists_blockers_instead_of_short_circuiting(tmp_path):
    kernel = _StubKernel()
    lane = _lane(tmp_path, kernel=kernel)
    _register(lane, months=2, cost=2000)             # 只提两个月就提足
    lane.depreciate("2026-02")

    # ① 该期间已计提过：预演照样可用，但可提额如实为 0（这期不会再提），并讲清为什么
    again = lane.depreciate("2026-02", preview=True)
    assert again["canSubmit"] is False
    assert any("已计提过" in b for b in again["blockers"]), again["blockers"]
    assert any("1000.00" in b for b in again["blockers"]), again["blockers"]
    assert again["total"] == "0.00"
    assert len(kernel.calls) == 1                     # 没有第二张提案

    # ② 本期无可计提资产（已提足）：blocker 说清原因，金额为 0
    lane.depreciate("2026-03")
    empty = lane.depreciate("2026-04", preview=True)
    assert empty["total"] == "0.00"
    assert empty["canSubmit"] is False
    assert any("无可计提资产" in b for b in empty["blockers"]), empty["blockers"]

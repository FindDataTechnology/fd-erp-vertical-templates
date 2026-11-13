"""月结清单（change: add-month-end-checklist 任务 1.1-1.4）。

判据（对应 spec `erp-month-end`）：

1. 三步按固定顺序准备，只出待确认 proposal；某步失败即停并点名；
2. 期间已关账 / 有未过门提案时不重复准备；
3. 关账前置=三步成功且期间内无待确认提案；`closer` 未注入时 `NOT_WIRED`；
4. `precheck` / `checklist` 只读。

零 DB：三条车道都用替身（只验编排，不验车道本身）。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.month_end import MonthEndChecklist

TENANT = "tpl-supply"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, pending: list[dict] | None = None):
        self.pending = list(pending or [])

    def list_proposals(self, status=None, tenant_id=None, offset=0, limit=100):
        return {"proposals": list(self.pending)}


class _StubCost:
    def __init__(self, uncosted: list[dict] | None = None, fail: str | None = None):
        self.uncosted = list(uncosted or [])
        self.fail = fail
        self.calls: list[str] = []

    def replay(self):
        return {"issues": self.uncosted}

    def post_cogs(self, period, *, partial=False, submitted_by="cost-engine"):
        self.calls.append(period)
        if self.fail:
            raise TemplateError("INVALID_ARGUMENT", self.fail)
        return {"period": period, "proposal": {"proposal_id": 1, "booked_on": period + "-30"},
                "total": "400.00"}


class _StubAssets:
    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.calls: list[str] = []

    def depreciate(self, period, *, units=None, submitted_by="asset-engine"):
        self.calls.append(str(period))
        if self.fail:
            raise TemplateError("NOT_WIRED", self.fail)
        return {"period": period, "proposal": {"proposal_id": 2}, "total": "1000.00"}


class _StubBadDebt:
    def __init__(self, registered: bool = True, noop: bool = False):
        self.registered = registered
        self.noop = noop
        self.calls: list[str] = []

    def schedule(self):
        return {"registered": self.registered, "bands": []}

    def provision(self, period, **kw):
        self.calls.append(str(period))
        return {"period": period, "noop": self.noop, "delta": "0.00" if self.noop else "650.00",
                "proposal": None if self.noop else {"proposal_id": 3}}


class _StubCloser:
    def __init__(self, closed: bool = False, fail: str | None = None):
        self._closed = closed
        self.fail = fail
        self.closed_calls: list[str] = []

    def status(self, period):
        return {"period": period, "closed": self._closed, "actor": "cfo" if self._closed else None}

    def close(self, period, *, actor, note=None):
        if self.fail:
            raise TemplateError("INVALID_ARGUMENT", self.fail)
        self.closed_calls.append(str(period))
        self._closed = True
        return {"period": period, "closed": True}


def _book(kernel=None, cost=None, assets=None, bad=None, closer=None):
    return MonthEndChecklist(
        TENANT, kernel or _StubKernel(),
        cost=cost or _StubCost(), assets=assets or _StubAssets(),
        bad_debts=bad or _StubBadDebt(), closer=closer)


# ---- 1) precheck 五类 ----

def test_precheck_is_clean_when_everything_is_ready():
    closer = _StubCloser()
    out = _book(closer=closer).precheck(PERIOD)
    assert out["readyToRun"] is True
    assert out["blockers"] == []
    assert {i["item"] for i in out["items"]} == {
        "period_status", "pending_proposals", "cost_coverage", "bad_debt_schedule"}


def test_precheck_blocks_on_closed_period():
    out = _book(closer=_StubCloser(closed=True)).precheck(PERIOD)
    assert out["readyToRun"] is False
    blocked = {b["item"]: b for b in out["blockers"]}
    assert "period_status" in blocked
    assert "重开" in blocked["period_status"]["fix"]


def test_precheck_blocks_on_uncosted_and_missing_schedule():
    cost = _StubCost(uncosted=[{"eventId": 7, "sku": "SKU-1", "cost": None,
                               "occurredAt": "2026-11-05T09:00:00+08:00"}])
    out = _book(cost=cost, bad=_StubBadDebt(registered=False)).precheck(PERIOD)
    names = {b["item"] for b in out["blockers"]}
    assert names == {"cost_coverage", "bad_debt_schedule"}
    assert out["readyToRun"] is False


def test_precheck_warns_when_closer_missing():
    out = _book(closer=None).precheck(PERIOD)
    assert out["readyToRun"] is True                  # 能准备，只是不能关
    assert [w["item"] for w in out["warnings"]] == ["period_status"]
    assert "未接线" in out["warnings"][0]["detail"]


# ---- 2) 三步顺序与失败即停 ----

def test_run_prepares_three_steps_in_order():
    cost, assets, bad = _StubCost(), _StubAssets(), _StubBadDebt()
    out = _book(cost=cost, assets=assets, bad=bad).run(PERIOD)
    assert [s["step"] for s in out["steps"]] == ["cogs", "depreciation", "bad_debt"]
    assert [s["state"] for s in out["steps"]] == ["prepared", "prepared", "prepared"]
    assert out["closed"] is False
    assert cost.calls == assets.calls == bad.calls == [PERIOD]


def test_failure_stops_the_chain():
    cost, assets, bad = _StubCost(fail="成本未知"), _StubAssets(), _StubBadDebt()
    out = _book(cost=cost, assets=assets, bad=bad).run(PERIOD)
    assert [s["step"] for s in out["steps"]] == ["cogs"]
    assert out["steps"][0]["state"] == "failed"
    assert assets.calls == [] and bad.calls == []      # 后续步骤没有执行
    assert out["blocked"] is True
    assert "修好 cogs" in out["nextActions"][0]


def test_closed_period_refuses_to_run():
    with pytest.raises(TemplateError) as ei:
        _book(closer=_StubCloser(closed=True)).run(PERIOD)
    assert "重开" in ei.value.message


def test_pending_proposals_short_circuit_the_run():
    kernel = _StubKernel(pending=[{"stable_id": "depr:2026-11", "booked_on": "2026-11-30"}])
    cost = _StubCost()
    out = _book(kernel=kernel, cost=cost).run(PERIOD)
    assert out["steps"] == []
    assert out["closed"] is False
    assert "过确认门" in out["nextActions"][0]
    assert cost.calls == []                            # 不在有未过门提案时重复准备


# ---- 3) 关账前置 ----

def test_does_not_close_while_proposals_are_pending():
    closer = _StubCloser()
    kernel = _StubKernel()                             # 准备前没有待办
    book = _book(kernel=kernel, closer=closer)
    # 模拟「本轮准备产生的三张提案仍在待确认」
    kernel.pending = [{"stable_id": "cogs:2026-11", "booked_on": "2026-11-30"}]
    out = book.run(PERIOD, close=True)
    assert out["closed"] is False
    assert closer.closed_calls == []
    assert "过确认门" in out["nextActions"][0]


def test_closes_when_nothing_is_pending_and_all_steps_noop():
    closer = _StubCloser()
    kernel = _StubKernel()
    book = _book(kernel=kernel, closer=closer, bad=_StubBadDebt(noop=True))
    out = book.run(PERIOD, close=True)
    assert out["closed"] is True
    assert closer.closed_calls == [PERIOD]
    assert out["blocked"] is False


def test_close_without_closer_is_not_wired():
    out = _book(closer=None, bad=_StubBadDebt(noop=True)).run(PERIOD, close=True)
    assert out["closed"] is False
    assert out["closeError"]["code"] == "NOT_WIRED"
    assert out["blocked"] is True


def test_close_failure_is_reported_not_swallowed():
    out = _book(closer=_StubCloser(fail="顺序关账：2026-10 未关"),
                bad=_StubBadDebt(noop=True)).run(PERIOD, close=True)
    assert out["closed"] is False
    assert "2026-10" in out["closeError"]["message"]


# ---- 4) checklist ----

def test_checklist_says_who_does_what():
    kernel = _StubKernel(pending=[{"stable_id": "cogs:2026-11", "booked_on": "2026-11-30"}])
    out = _book(kernel=kernel).checklist(PERIOD)
    assert out["period"] == PERIOD
    assert any("decider" in w for w in out["nextOwners"])
    assert out["pending"][0]["stable_id"] == "cogs:2026-11"

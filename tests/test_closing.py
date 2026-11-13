"""期末损益结转（change: add-profit-closing 任务 1.x / 2.1）。

判据（对应 spec `erp-profit-closing` 与 `erp-financial-statements` 的结转口径）：

1. 逐科目反向结平，差额入 3100，借贷必平；盈利/亏损两向都对；
2. 结转分录带 `close:` 前缀，**不进利润表与费用维度表**，但试算平衡表与资产负债表必须包含；
3. 同期间幂等（重复调用不重复过账）；已关账期间拒绝；无损益不生成 0 元分录；
4. 结转分录不成为下一次结转的基数（否则第二次会结出 0 或负数）。

零 DB：替身内核返回内核真实形状（`entries` / `proposals`）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.closing import PeriodClosing
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.statements import StatementBook

TENANT = "tpl-supply"
PERIOD = "2026-11"


class _StubKernel:
    """内核替身：分录 + 提案。`submit_proposal` 与内核一样按 stable_id 幂等。"""

    def __init__(self, entries: list[dict] | None = None,
                 proposals: list[dict] | None = None):
        self.entries = list(entries or [])
        self.proposals = list(proposals or [])
        self.submitted: list[dict] = []

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100, **_kw):
        return {"entries": self.entries[offset:offset + limit]}

    def list_proposals(self, status=None, **_kw):
        rows = [p for p in self.proposals
                if status is None or p.get("status") == status]
        return {"proposals": rows}

    def submit_proposal(self, stable_id, lines, **kw):
        existing = next((p for p in self.proposals if p["stable_id"] == stable_id), None)
        if existing is not None:
            return dict(existing, duplicate=True)
        prop = {"id": len(self.proposals) + 1, "stable_id": stable_id,
                "status": "pending", "lines": lines, **kw}
        self.proposals.append(prop)
        self.submitted.append(prop)
        return prop


class _StubPeriods:
    def __init__(self, closed: bool = False):
        self._closed = closed

    def status(self, period):
        return {"period": period, "closed": self._closed,
                "closedAt": "2026-11-30T23:00:00+08:00" if self._closed else None}


def _e(code, direction, amount, period="2026-11", source_ref=None):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "posted_at": f"{period}-10T09:00:00+08:00",
            "booked_on": f"{period}-10", "source_ref": source_ref, "dimensions": {}}


def _profit_entries() -> list[dict]:
    """收入 3000、费用 500。"""
    return [_e("1000", "debit", "3000"), _e("4000", "credit", "3000"),
            _e("5200", "debit", "500"), _e("1000", "credit", "500")]


def _closing_entries() -> list[dict]:
    return [_e("4000", "debit", "3000", source_ref="close:2026-11"),
            _e("5200", "credit", "500", source_ref="close:2026-11"),
            _e("3100", "credit", "2500", source_ref="close:2026-11")]


# ---- 1) 预览 ----

def test_preview_says_what_and_how_much_without_touching_anything():
    kernel = _StubKernel(_profit_entries())
    book = PeriodClosing(TENANT, kernel)
    out = book.preview(PERIOD)
    assert out["netProfit"] == "2500.00"
    assert out["retainedDirection"] == "credit"
    assert out["retainedAmount"] == "2500.00"
    assert out["bookedOn"] == "2026-11-30"
    assert out["status"]["state"] == "not_closed"
    rows = {r["accountCode"]: r for r in out["balances"]}
    assert rows["4000"]["closingDirection"] == "debit"
    assert rows["4000"]["closingAmount"] == "3000.00"
    assert rows["5200"]["closingDirection"] == "credit"
    assert out["closingLines"][0]["account"] == "营业收入"
    assert kernel.submitted == []          # 预览不产生提案


def test_preview_is_stable_across_calls():
    book = PeriodClosing(TENANT, _StubKernel(_profit_entries()))
    assert book.preview(PERIOD) == book.preview(PERIOD)


# ---- 2) 结转分录 ----

def test_transfer_balances_profit_period():
    kernel = _StubKernel(_profit_entries())
    out = PeriodClosing(TENANT, kernel).transfer(PERIOD)
    lines = {(l["account_code"], l["direction"]): l["amount"] for l in out["lines"]}
    assert lines == {("4000", "debit"): "3000.00", ("5200", "credit"): "500.00",
                     ("3100", "credit"): "2500.00"}
    debit = sum(Decimal(v) for (c, d), v in lines.items() if d == "debit")
    credit = sum(Decimal(v) for (c, d), v in lines.items() if d == "credit")
    assert debit == credit == Decimal("3000")
    assert out["stableId"] == "close:2026-11"
    assert out["bookedOn"] == "2026-11-30"
    assert kernel.submitted[0]["source_ref"] == "close:2026-11"
    assert out["status"]["state"] == "pending"


def test_transfer_handles_a_loss():
    entries = [_e("1000", "debit", "300"), _e("4000", "credit", "300"),
               _e("5200", "debit", "800"), _e("1000", "credit", "800")]
    out = PeriodClosing(TENANT, _StubKernel(entries)).transfer(PERIOD)
    lines = {(l["account_code"], l["direction"]): l["amount"] for l in out["lines"]}
    assert lines == {("4000", "debit"): "300.00", ("5200", "credit"): "800.00",
                     ("3100", "debit"): "500.00"}   # 亏损借记留存收益
    debit = sum(Decimal(v) for (c, d), v in lines.items() if d == "debit")
    credit = sum(Decimal(v) for (c, d), v in lines.items() if d == "credit")
    assert debit == credit == Decimal("800")


# ---- 3) 幂等与边界 ----

def test_transfer_is_idempotent_per_period():
    kernel = _StubKernel(_profit_entries())
    book = PeriodClosing(TENANT, kernel)
    first = book.transfer(PERIOD)
    second = book.transfer(PERIOD)
    assert first["proposal"]["id"] == second["proposal"]["id"]
    assert second["duplicate"] is True and second["posted"] is False
    assert len(kernel.submitted) == 1                     # 不重复提交

    kernel.proposals[0]["status"] = "confirmed"
    third = book.transfer(PERIOD)
    assert third["posted"] is True and third["duplicate"] is True
    assert book.status(PERIOD)["state"] == "posted"
    assert len(kernel.submitted) == 1


def test_no_activity_yields_noop_and_does_not_burn_the_key():
    kernel = _StubKernel([_e("1000", "debit", "100"), _e("3000", "credit", "100")])
    out = PeriodClosing(TENANT, kernel).transfer(PERIOD)
    assert out["noop"] is True and out["proposal"] is None
    assert kernel.submitted == []
    assert out["status"]["state"] == "not_closed"


def test_closed_period_is_refused():
    book = PeriodClosing(TENANT, _StubKernel(_profit_entries()),
                         periods=_StubPeriods(closed=True))
    with pytest.raises(TemplateError) as ei:
        book.transfer(PERIOD)
    assert "已关账" in ei.value.message and "重开" in ei.value.message


def test_posted_closing_does_not_become_the_next_base():
    """结转分录本身不能进下一次结转的基数——否则第二次会结出 0（或把损益再搬一遍）。"""
    kernel = _StubKernel(_profit_entries() + _closing_entries())
    out = PeriodClosing(TENANT, kernel).preview(PERIOD)
    assert out["netProfit"] == "2500.00"


# ---- 4) 报表口径：排除结转、但资产负债表照样算它 ----

def test_closing_entries_are_excluded_from_income_statement():
    entries = _profit_entries() + _closing_entries()
    book = StatementBook(TENANT, _StubKernel(entries))
    inc = book.income_statement(PERIOD)
    assert inc["revenue"] == "3000.00"          # 不是 0
    assert inc["otherOpex"] == "500.00"
    assert inc["operatingProfit"] == "2500.00"


def test_closing_entries_are_excluded_from_expense_dimension_report():
    entries = _profit_entries() + _closing_entries()
    book = StatementBook(TENANT, _StubKernel(entries))
    dim = book.expenses_by_dimension(PERIOD, "cost_center")
    assert dim["total"] == "500.00"
    assert dim["rows"] == [{"dimensionValue": "unattributed", "amount": "500.00",
                            "attributed": False}]


def test_balance_sheet_includes_closing_and_unclosed_profit_goes_to_zero():
    entries = _profit_entries() + _closing_entries()
    book = StatementBook(TENANT, _StubKernel(entries))
    bs = book.balance_sheet(PERIOD)
    assert bs["balanced"] is True, bs.get("error")
    assert bs["unclosedProfit"] == "0.00"
    equity = {line["accountCode"]: line["amount"] for line in bs["equity"]["lines"]}
    assert equity["3100"] == "2500.00"
    assert bs["assets"]["total"] == "2500.00"


def test_trial_balance_includes_closing_and_stays_balanced():
    entries = _profit_entries() + _closing_entries()
    book = StatementBook(TENANT, _StubKernel(entries))
    tb = book.trial_balance(PERIOD)
    assert tb["balanced"] is True
    rows = {r["accountCode"]: r for r in tb["rows"]}
    assert rows["4000"]["debit"] == "3000.00"   # 结平的借记确实进了试算表
    assert rows["4000"]["balance"] == "0.00"

"""建账期初余额（change: add-opening-balances 任务 1.1-1.5）。

判据（对应 spec `erp-opening-balances`）：

1. 不平就拒（给两侧合计与差额），不补差、不塞 3100；
2. 只此一次：重复提交返回既有提案 + `duplicate=true`，不重复过账；
3. 只能在空账上录；`allow_with_entries=True` 必须带理由且回显；
4. 科目在册、金额非零、同科目不得重复行；
5. `preview()` 把已落账期初单投影成逐科目期初，账簿期初列与它相等。

零 DB：替身内核返回内核真实形状（`entries` / `proposals`）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.books import LedgerBooks
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.opening import OpeningBalances

TENANT = "tpl-supply"


class _StubKernel:
    def __init__(self, entries: list[dict] | None = None,
                 proposals: list[dict] | None = None):
        self.entries = list(entries or [])
        self.proposals = list(proposals or [])
        self.submitted: list[dict] = []

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100, **_kw):
        return {"entries": self.entries[offset:offset + limit],
                "rows_returned": len(self.entries)}

    def list_proposals(self, status=None, **_kw):
        rows = [p for p in self.proposals
                if status is None or p.get("status") == status]
        return {"proposals": rows, "rows_returned": len(rows)}

    def submit_proposal(self, stable_id, lines, **kw):
        existing = next((p for p in self.proposals if p["stable_id"] == stable_id), None)
        if existing is not None:
            return dict(existing, duplicate=True)
        prop = {"id": len(self.proposals) + 1, "stable_id": stable_id,
                "status": "pending", "lines": lines, **kw}
        self.proposals.append(prop)
        self.submitted.append(prop)
        return prop


def _opening(entries=None, proposals=None) -> OpeningBalances:
    return OpeningBalances(TENANT, _StubKernel(entries, proposals),
                           today="2026-01-01")


def _typical() -> list[dict]:
    return [
        {"account_code": "1100", "direction": "debit", "amount": "3000"},
        {"account_code": "1200", "direction": "debit", "amount": "2000"},
        {"account_code": "2000", "direction": "credit", "amount": "2000"},
        {"account_code": "3000", "direction": "credit", "amount": "3000"},
    ]


# ---- 1) 借贷必平 ----

def test_register_submits_a_balanced_opening_as_pending():
    out = _opening().register(_typical(), as_of="2026-01-01")
    assert out["duplicate"] is False
    assert out["debit"] == out["credit"] == "5000.00"
    assert out["stableId"] == "opening:tpl-supply"
    assert out["bookedOn"] == "2026-01-01"
    assert out["proposal"]["status"] == "pending"
    assert out["proposal"]["source_ref"] == "opening"


def test_unbalanced_opening_is_refused_with_the_gap():
    lines = _typical()[:-1]        # 去掉 3000 的贷方 → 借 5000 / 贷 2000
    with pytest.raises(TemplateError) as ei:
        _opening().register(lines, as_of="2026-01-01")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert ei.value.detail == {"debit": "5000.00", "credit": "2000.00",
                               "difference": "3000.00"}
    assert "不能拿某个科目去补差" in ei.value.message


def test_period_as_of_resolves_to_period_end():
    out = _opening().register(_typical(), as_of="2026-01")
    assert out["bookedOn"] == "2026-01-31"


# ---- 2) 只此一次 ----

def test_second_register_returns_the_same_proposal():
    kernel = _StubKernel()
    book = OpeningBalances(TENANT, kernel, today="2026-01-01")
    first = book.register(_typical())
    second = book.register(_typical())
    assert second["duplicate"] is True
    assert second["proposal"]["id"] == first["proposal"]["id"]
    assert len(kernel.submitted) == 1
    assert book.status()["state"] == "pending"

    kernel.proposals[0]["status"] = "confirmed"
    third = book.register(_typical())
    assert third["duplicate"] is True
    assert book.status()["state"] == "posted"


# ---- 3) 仅限空账 ----

def test_existing_entries_block_the_opening():
    entries = [{"account_code": "1100", "direction": "debit", "amount": "100",
                "amount_base": "100", "booked_on": "2026-03-05", "txn_id": "t1"}]
    with pytest.raises(TemplateError) as ei:
        _opening(entries).register(_typical())
    assert ei.value.detail["entries"] == 1
    assert ei.value.detail["earliest"] == "2026-03-05"
    assert "空账" in ei.value.message


def test_allow_with_entries_requires_a_reason():
    entries = [{"account_code": "1100", "direction": "debit", "amount": "100",
                "amount_base": "100", "booked_on": "2026-03-05", "txn_id": "t1"}]
    with pytest.raises(TemplateError) as ei:
        _opening(entries).register(_typical(), allow_with_entries=True)
    assert "理由" in ei.value.message


def test_allow_with_entries_echoes_the_reason():
    entries = [{"account_code": "1100", "direction": "debit", "amount": "100",
                "amount_base": "100", "booked_on": "2026-03-05", "txn_id": "t1"}]
    out = _opening(entries).register(_typical(), allow_with_entries=True,
                                     reason="历史迁移")
    assert out["allowWithEntries"] is True and out["reason"] == "历史迁移"


# ---- 4) 行级校验 ----

def test_off_chart_account_is_refused():
    lines = _typical() + [{"account_code": "9999", "direction": "debit",
                           "amount": "1"}]
    with pytest.raises(TemplateError) as ei:
        _opening().register(lines)
    assert "9999" in ei.value.message and ei.value.detail["account"] == "9999"


def test_duplicate_account_rows_are_refused():
    lines = _typical() + [{"account_code": "1100", "direction": "credit",
                           "amount": "3000"}]
    with pytest.raises(TemplateError) as ei:
        _opening().register(lines)
    assert "出现了两次" in ei.value.message


def test_zero_amount_is_refused():
    lines = _typical() + [{"account_code": "1602", "direction": "credit",
                           "amount": "0"}]
    with pytest.raises(TemplateError) as ei:
        _opening().register(lines)
    assert "大于零" in ei.value.message


# ---- 5) 回读与账簿对照 ----

def test_status_and_preview_before_any_opening():
    book = _opening()
    assert book.status()["state"] == "not_opened"
    assert book.preview()["rows"] == []
    assert book.preview()["total"] == "0.00"


def test_preview_matches_the_books_opening_column_after_posting():
    """落账后：账簿期初列 == preview 逐科目余额。这是「期初到底录进去没有」的唯一判据。"""
    kernel = _StubKernel()
    book = OpeningBalances(TENANT, kernel, today="2026-01-01")
    book.register(_typical())
    proposal = kernel.proposals[0]
    proposal["status"] = "confirmed"
    kernel.entries = [
        {"account_code": line["account_code"], "direction": line["direction"],
         "amount": line["amount"], "amount_base": line["amount"],
         "booked_on": "2026-01-01", "posted_at": "2026-01-01T00:00:00+08:00",
         "txn_id": "t-open", "line_no": i + 1,
         "stable_id": proposal["stable_id"]}
        for i, line in enumerate(proposal["lines"])]

    pre = book.preview()
    assert pre["status"] == "posted"
    assert {r["accountCode"]: r["balance"] for r in pre["rows"]} == {
        "1100": "3000.00", "1200": "2000.00", "2000": "-2000.00",
        "3000": "-3000.00"}
    assert Decimal(pre["total"]) == 0          # 借贷必平 ⇒ 合计恒零

    balances = LedgerBooks(TENANT, kernel).account_balances("2026-02")
    opening = {r["accountCode"]: r["opening"] for r in balances["rows"]}
    assert opening == {r["accountCode"]: r["balance"] for r in pre["rows"]}

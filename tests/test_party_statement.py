"""往来对账单（change: add-party-statement 任务 1.1-1.5）。

判据（对应 spec `erp-ledger-books` 的「往来对账单」要求）：

1. 按维度值跨科目串联：赊销与回款串成一条线，逐行余额闭合；
2. 分科目小计与合计自洽（小计 == 明细之和）；
3. 指定 `accounts` 时对回总账：本单期末 + 未标该维度净额 == 总账期末；
4. 未指定科目时不做总账对账并明说；
5. 空往来方如实返回；跨月确认的单据归业务日期月。

零 DB：替身内核返回内核真实形状（`entries`）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.books import LedgerBooks
from fd_erp_vertical_templates.errors import TemplateError

TENANT = "tpl-supply"


class _StubKernel:
    def __init__(self, entries: list[dict]):
        self._entries = list(entries)

    def query_entries(self, offset=0, limit=100, **_kw):
        return {"entries": self._entries[offset:offset + limit],
                "rows_returned": len(self._entries)}


def _e(code, direction, amount, booked_on="2026-11-10", customer="C-A",
       line_no=1, txn=None, account_of_customer=True):
    dims = {"customer": customer} if account_of_customer and customer else {}
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": booked_on,
            "posted_at": f"{booked_on}T09:00:00+08:00",
            "txn_id": txn or f"t-{code}-{direction}-{booked_on}",
            "line_no": line_no, "source_ref": None, "dimensions": dims}


def _customer_flow() -> list[dict]:
    """C-A：赊销 1000（借应收 / 贷收入）→ 回款 600（借现金 / 贷应收）。"""
    return [
        _e("1100", "debit", "1000", "2026-11-05", line_no=1, txn="t-sale"),
        _e("4000", "credit", "1000", "2026-11-05", line_no=2, txn="t-sale"),
        _e("1000", "debit", "600", "2026-11-08", line_no=1, txn="t-pay"),
        _e("1100", "credit", "600", "2026-11-08", line_no=2, txn="t-pay"),
    ]


def _books(entries=None) -> LedgerBooks:
    return LedgerBooks(TENANT, _StubKernel(_customer_flow() if entries is None else entries))


# ---- 1) 跨科目串联 + 逐行余额 ----

def test_party_statement_links_the_whole_relationship():
    out = _books().party_statement("customer", "C-A", "2026-11")
    assert [r["accountCode"] for r in out["rows"]] == ["1100", "4000", "1000", "1100"]
    # 逐行余额按科目走：应收 1000 → 400；收入 −1000；现金 600
    assert [r["balance"] for r in out["rows"]] == [
        "1000.00", "-1000.00", "600.00", "400.00"]
    assert out["opening"] == "0.00"
    assert out["totals"] == {"debit": "1600.00", "credit": "1600.00",
                             "closing": "0.00"}
    assert out["rows"][0]["txnId"] == "t-sale"


def test_subtotals_add_up_to_the_total():
    out = _books().party_statement("customer", "C-A", "2026-11")
    subs = {s["accountCode"]: s for s in out["subtotals"]}
    assert subs["1100"]["debit"] == "1000.00" and subs["1100"]["credit"] == "600.00"
    assert subs["1100"]["closing"] == "400.00"      # 应收净额
    assert subs["4000"]["closing"] == "-1000.00"
    assert subs["1000"]["closing"] == "600.00"
    assert Decimal(out["totals"]["debit"]) == sum(
        Decimal(s["debit"]) for s in out["subtotals"])
    assert Decimal(out["totals"]["credit"]) == sum(
        Decimal(s["credit"]) for s in out["subtotals"])


# ---- 2) 对回总账 ----

def test_reconciles_with_the_ledger_when_nothing_is_unattributed():
    out = _books().party_statement("customer", "C-A", "2026-11",
                                   accounts=["1100", "4000", "1000"])
    assert out["reconciled"] is True and out["consistent"] is True
    assert {c["accountCode"]: c["difference"] for c in out["reconciliation"]} == {
        "1100": "0.00", "4000": "0.00", "1000": "0.00"}
    assert {c["accountCode"]: c["unattributed"] for c in out["reconciliation"]} == {
        "1100": "0.00", "4000": "0.00", "1000": "0.00"}


def test_unattributed_entries_are_named_so_the_totals_still_tie():
    """同一个客户之外还有一笔没标客户的分录：它进 unattributed，对账仍成立。"""
    entries = _customer_flow() + [_e("1100", "debit", "250", "2026-11-09",
                                     customer=None, line_no=1, txn="t-nodim")]
    out = _books(entries).party_statement("customer", "C-A", "2026-11",
                                          accounts=["1100"])
    assert out["consistent"] is True
    check = out["reconciliation"][0]
    assert check["thisParty"] == "400.00"
    assert check["unattributed"] == "250.00"
    assert check["ledgerClosing"] == "650.00"        # 400 + 250
    assert check["difference"] == "0.00"


def test_a_real_gap_is_reported_not_papered_over():
    """构造「按客户拆不开」的情形：某笔分录标了别的客户，却要求只看 C-A 且不给未标注口径。"""
    entries = [
        _e("1100", "debit", "100", "2026-11-05", customer="C-OTHER", txn="t-other"),
        _e("1100", "debit", "50", "2026-11-06", customer="C-A", txn="t-mine"),
    ]
    out = _books(entries).party_statement("customer", "C-A", "2026-11",
                                          accounts=["1100"])
    # 另一客户不属于 C-A，也不算「未标注」——差额如实报出来
    check = out["reconciliation"][0]
    assert out["consistent"] is False
    assert check["difference"] == "100.00"
    assert "对不上" in out["error"]


def test_without_accounts_there_is_no_ledger_reconciliation():
    out = _books().party_statement("customer", "C-A", "2026-11")
    assert out["reconciled"] is False
    assert "reconciliation" not in out
    assert "未指定 accounts" in out["caliber_note"]


# ---- 3) 空与边界 ----

def test_empty_party_is_reported_honestly():
    out = _books().party_statement("customer", "C-NONE", "2026-11")
    assert out["rows"] == [] and out["subtotals"] == []
    assert out["closingBalance"] == "0.00"
    assert "无发生额" in out["note"]


def test_dimension_and_value_are_required():
    book = _books()
    with pytest.raises(TemplateError):
        book.party_statement("", "C-A")
    with pytest.raises(TemplateError):
        book.party_statement("customer", "")


def test_party_with_only_opening_balance_still_gets_a_row():
    entries = [_e("1100", "debit", "300", "2026-10-05", txn="t-oct")]
    out = _books(entries).party_statement("customer", "C-A", "2026-11",
                                          accounts=["1100"])
    assert out["opening"] == "300.00"
    assert out["subtotals"][0]["opening"] == "300.00"
    assert out["subtotals"][0]["closing"] == "300.00"
    assert out["rows"] == []


def test_period_follows_booked_on_not_posting_time():
    posted = "2026-11-02T09:00:00+08:00"
    entries = [
        {"account_code": "1100", "direction": "debit", "amount": "500",
         "amount_base": "500", "booked_on": "2026-10-31", "posted_at": posted,
         "txn_id": "t-oct", "line_no": 1, "dimensions": {"customer": "C-A"}},
    ]
    book = _books(entries)
    oct_ = book.party_statement("customer", "C-A", "2026-10")
    assert oct_["totals"]["closing"] == "500.00"
    nov = book.party_statement("customer", "C-A", "2026-11")
    assert nov["rows"] == [] and nov["opening"] == "500.00"

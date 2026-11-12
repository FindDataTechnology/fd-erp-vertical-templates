"""应付账龄（change: add-payable-aging 任务 1.2-1.4）。

判据（对应 spec `erp-payable-aging`）：

1. 与应收**同一份**核销分桶实现，只反转开放方向（不是复制一份镜像代码）；
2. 付款按 FIFO 冲最早的未结应付，剩余按截至日分桶，桶标签与应收一致；
3. 多付报 `debitBalance`（预付/多付），不出负账龄；
4. 各桶合计 − 预付 = 应付科目净额（`2000` 贷方为正）。

零 DB：替身内核按内核真实形状返回 `entries`。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.aging import (AgingReport, PayableAging,
                                             ReceivableAging)
from fd_erp_vertical_templates.errors import TemplateError

TENANT = "tpl-supply"
CUTOFF = "2026-11-30"


class _StubKernel:
    def __init__(self, entries: list[dict]):
        self._entries = list(entries)

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100, **_kw):
        rows = [e for e in self._entries
                if account_code is None or e["account_code"] == account_code]
        return {"entries": rows[offset:offset + limit]}


def _e(direction, amount, posted_at, supplier=None, account="2000"):
    dims = {"customer": supplier} if supplier else {}
    return {"account_code": account, "direction": direction, "amount": amount,
            "amount_base": amount, "posted_at": f"{posted_at}T09:00:00+08:00",
            "line_no": 1, "dimensions": dims}


def _report(entries, **kw):
    return PayableAging(TENANT, _StubKernel(entries)).report(CUTOFF, **kw)


def _rows(out):
    return {r["dimensionValue"]: r for r in out["rows"]}


def _bucket(row, label):
    return next(b["amount"] for b in row["buckets"] if b["label"] == label)


# ---- 1) 复用而不是镜像复制 ----

def test_payable_and_receivable_share_one_implementation():
    """两份实现必然在边界上漂移——所以绑定必须指向同一个函数对象。"""
    assert PayableAging.report is AgingReport.report
    assert ReceivableAging.report is AgingReport.report
    assert PayableAging.SIDE != ReceivableAging.SIDE
    assert (PayableAging.ACCOUNT, ReceivableAging.ACCOUNT) == ("2000", "1100")


def test_bucket_labels_and_unattributed_behave_identically_on_both_sides():
    entries = [
        _e("credit", "100", "2026-11-10", "S-A"),
        _e("credit", "400", "2026-11-10"),
    ]
    ap = _report(entries)
    ar = ReceivableAging(TENANT, _StubKernel([
        {**e, "account_code": "1100", "direction": "debit"} for e in entries
    ])).report(CUTOFF)
    assert ap["bucketLabels"] == ar["bucketLabels"]
    assert ap["dimension"] == ar["dimension"] == "customer"
    assert _rows(ap)["unattributed"]["attributed"] is False
    assert _rows(ap)["S-A"]["outstanding"] == "100.00"
    assert _rows(ap)["unattributed"]["outstanding"] == "400.00"


# ---- 2) 贷方是开放项，付款按 FIFO 核销 ----

def test_payment_clears_the_oldest_payable():
    out = _report([
        _e("credit", "500", "2026-08-22", "S-A"),      # 100 天前
        _e("credit", "300", "2026-11-20", "S-A"),      # 10 天前
        _e("debit", "500", "2026-11-25", "S-A"),       # 付款
    ])
    row = _rows(out)["S-A"]
    assert out["side"] == "credit"
    assert out["account"] == "2000"
    assert row["outstanding"] == "300.00"
    assert _bucket(row, "91天以上") == "0.00"
    assert _bucket(row, "0-30天") == "300.00"
    assert "debitBalance" not in row


def test_entries_after_cutoff_are_ignored():
    out = _report([
        _e("credit", "100", "2026-11-10", "S-A"),
        _e("debit", "999", "2026-12-05", "S-A"),
    ])
    assert _rows(out)["S-A"]["outstanding"] == "100.00"


# ---- 3) 多付如实报预付 ----

def test_overpayment_reports_debit_balance_not_negative_aging():
    out = _report([
        _e("credit", "200", "2026-11-01", "S-D"),
        _e("debit", "350", "2026-11-10", "S-D"),
    ])
    row = _rows(out)["S-D"]
    assert row["outstanding"] == "0.00"
    assert row["debitBalance"] == "150.00"
    assert "creditBalance" not in row
    assert "预付" in row["note"]
    assert all(Decimal(b["amount"]) >= 0 for b in row["buckets"])
    assert out["totalDebitBalance"] == "150.00"
    assert out["consistent"] is True


# ---- 4) 一致性：合计 − 预付 = 科目净额 ----

def test_bucket_total_minus_prepayment_equals_payable_balance():
    out = _report([
        _e("credit", "500", "2026-09-01", "S-A"),
        _e("debit", "200", "2026-11-01", "S-A"),
        _e("credit", "300", "2026-11-10", "S-B"),
        _e("credit", "100", "2026-11-10"),             # 未标供应商也计入净额
        _e("debit", "150", "2026-11-10", "S-C"),       # 纯预付
    ])
    assert out["consistent"] is True, out.get("error")
    assert out["totalOutstanding"] == "700.00"         # (500−200) + 300 + 100
    assert out["totalDebitBalance"] == "150.00"
    assert out["accountNetBalance"] == "550.00"         # 贷 900 − 借 350
    assert Decimal(out["totalOutstanding"]) - Decimal(out["totalDebitBalance"]) \
        == Decimal(out["accountNetBalance"])


def test_consistent_report_carries_no_error_note():
    out = _report([_e("credit", "100", "2026-11-10", "S-A")])
    assert out["consistent"] is True
    assert out["accountNetBalance"] == "100.00"
    assert "error" not in out


# ---- 5) 参数校验 ----

def test_illegal_side_is_refused():
    with pytest.raises(TemplateError) as ei:
        _report([], side="left")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "debit" in ei.value.message and "credit" in ei.value.message


def test_side_override_still_reads_one_implementation():
    """显式传 `side` 与用绑定类走的是同一条路径。"""
    entries = [_e("credit", "100", "2026-11-10", "S-A")]
    via_class = PayableAging(TENANT, _StubKernel(entries)).report(CUTOFF)
    via_param = AgingReport(TENANT, _StubKernel(entries)).report(
        CUTOFF, account="2000", side="credit")
    assert via_class == via_param


def test_report_only_reads_the_given_account():
    out = _report([
        _e("credit", "100", "2026-11-10", "S-A"),
        _e("credit", "999", "2026-11-10", "S-A", account="2100"),
    ])
    assert _rows(out)["S-A"]["outstanding"] == "100.00"

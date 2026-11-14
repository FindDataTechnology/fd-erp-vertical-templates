"""应收账龄（change: add-receivable-aging 任务 2.1-2.4）。

判据（对应 spec `erp-receivable-aging`）：

1. 客户归属走 `customer` 维度，未标客户单列；
2. FIFO 核销（收款先冲最早的未核销），剩余按截至日分桶；
3. 各桶合计必须等于科目净余额（不等就如实报，不调桶）；
4. 多收报 `creditBalance`，不出现负账龄。

零 DB：替身内核按内核真实形状返回 `entries`。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.aging import ReceivableAging
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


def _e(direction, amount, posted_at, customer=None, account="1100"):
    dims = {"customer": customer} if customer else {}
    return {"account_code": account, "direction": direction, "amount": amount,
            "amount_base": amount, "posted_at": f"{posted_at}T09:00:00+08:00",
            "line_no": 1, "dimensions": dims}


def _report(entries, **kw):
    return ReceivableAging(TENANT, _StubKernel(entries)).report(CUTOFF, **kw)


def _rows(out):
    return {r["dimensionValue"]: r for r in out["rows"]}


def _bucket(out, row, label):
    return next(b["amount"] for b in row["buckets"] if b["label"] == label)


# ---- 1) 归属与未标注 ----

def test_two_customers_and_unattributed_are_three_rows():
    out = _report([
        _e("debit", "100", "2026-11-10", "C-A"),
        _e("debit", "200", "2026-11-10", "C-B"),
        _e("debit", "300", "2026-11-10"),
    ])
    rows = _rows(out)
    assert set(rows) == {"C-A", "C-B", "unattributed"}
    assert rows["unattributed"]["attributed"] is False
    assert rows["C-A"]["attributed"] is True
    assert out["totalOutstanding"] == "600.00"
    assert sum(Decimal(t["amount"]) for t in out["totals"]) == Decimal("600")


def test_unattributed_is_not_spread_over_customers():
    out = _report([_e("debit", "100", "2026-11-10", "C-A"),
                   _e("debit", "400", "2026-11-10")])
    rows = _rows(out)
    assert rows["C-A"]["outstanding"] == "100.00"     # 不是 250


# ---- 2) FIFO 核销与分桶 ----

def test_payment_clears_the_oldest_invoice():
    out = _report([
        _e("debit", "100", "2026-08-22", "C-A"),      # 100 天前
        _e("debit", "200", "2026-11-20", "C-A"),      # 10 天前
        _e("credit", "100", "2026-11-25", "C-A"),
    ])
    row = _rows(out)["C-A"]
    assert row["outstanding"] == "200.00"
    assert _bucket(out, row, "91天以上") == "0.00"
    assert _bucket(out, row, "0-30天") == "200.00"


def test_buckets_follow_days_from_cutoff():
    out = _report([
        _e("debit", "10", "2026-11-30", "C-A"),       # 0 天
        _e("debit", "20", "2026-11-01", "C-A"),       # 29 天
        _e("debit", "30", "2026-10-31", "C-A"),       # 30 天
        _e("debit", "40", "2026-10-01", "C-A"),       # 60 天
        _e("debit", "50", "2026-09-01", "C-A"),       # 90 天
        _e("debit", "60", "2026-08-01", "C-A"),       # 121 天
    ])
    row = _rows(out)["C-A"]
    assert _bucket(out, row, "0-30天") == "60.00"     # 10 + 20 + 30
    assert _bucket(out, row, "31-60天") == "40.00"
    assert _bucket(out, row, "61-90天") == "50.00"
    assert _bucket(out, row, "91天以上") == "60.00"


def test_entries_after_cutoff_are_ignored():
    out = _report([
        _e("debit", "100", "2026-11-10", "C-A"),
        _e("debit", "999", "2026-12-05", "C-A"),
    ])
    assert _rows(out)["C-A"]["outstanding"] == "100.00"


# ---- 3) 一致性与多收 ----

def test_bucket_total_equals_account_net_balance():
    out = _report([
        _e("debit", "500", "2026-09-01", "C-A"),
        _e("credit", "200", "2026-11-01", "C-A"),
        _e("debit", "300", "2026-11-10", "C-B"),
        _e("debit", "100", "2026-11-10"),              # 未标客户也计入净额
    ])
    assert out["consistent"] is True
    assert out["totalOutstanding"] == out["accountNetBalance"] == "700.00"


def test_overpayment_reports_credit_balance_not_negative_aging():
    out = _report([
        _e("debit", "100", "2026-11-01", "C-C"),
        _e("credit", "150", "2026-11-10", "C-C"),
    ])
    row = _rows(out)["C-C"]
    assert row["outstanding"] == "0.00"
    assert row["creditBalance"] == "50.00"
    assert "预收" in row["note"]
    assert all(Decimal(b["amount"]) >= 0 for b in row["buckets"])
    assert out["totalCreditBalance"] == "50.00"
    assert out["consistent"] is True                   # 净额 −50 = 账龄 0 − 预收 50


def test_custom_buckets_and_dimension_validation():
    out = _report([_e("debit", "100", "2026-11-10", "C-A")], buckets=(15, 45))
    assert out["bucketLabels"] == ["0-15天", "16-45天", "46天以上"]
    with pytest.raises(TemplateError):
        _report([], buckets=(60, 30))                  # 非递增
    with pytest.raises(TemplateError) as ei:
        _report([], dimension="client")                # 维度不在册
    assert ei.value.detail["allowed"]


def test_report_only_reads_the_given_account():
    out = _report([
        _e("debit", "100", "2026-11-10", "C-A"),
        _e("debit", "999", "2026-11-10", "C-A", account="1200"),
    ])
    assert _rows(out)["C-A"]["outstanding"] == "100.00"


# ---- 4) 账龄从业务日期起算（change: add-period-close）----

def test_aging_starts_from_booked_on_when_it_differs_from_posting_time():
    """10 月的单据 11 月才落账：账龄必须从业务日期算，否则逾期凭空变好。"""
    late = _e("debit", "100", "2026-11-20", "C-A")   # posted_at = 11-20
    late["booked_on"] = "2026-08-22"                  # 业务日期 = 100 天前
    out = _report([late])
    row = _rows(out)["C-A"]
    assert _bucket(out, row, "91天以上") == "100.00"
    assert _bucket(out, row, "0-30天") == "0.00"


# ---- 5) 空账：一条分录都没有时也必须出得来表（回归）----

def test_empty_ledger_reports_zeroes_instead_of_crashing():
    """空账曾 500：`sum(空生成器)` 返回 **int 0**，而 `_fmt` 调的是 `Decimal.quantize`。

    新客户第一天、或刚切到应付侧还没有往来时，账龄表一定跑在空账上——这条不是边角。
    """
    out = _report([])
    assert out["rows"] == []
    # 桶清单照常给（前端要渲染表头），只是每桶 0
    assert [b["amount"] for b in out["totals"]] == ["0.00"] * len(out["totals"])
    assert out["totalOutstanding"] == "0.00"
    assert out["accountNetBalance"] == "0.00"
    assert out["consistent"] is True
    assert "error" not in out


# ---- 6) 不平必须如实报（红证）----

def test_imbalance_is_reported_not_papered_over():
    """`consistent=false` + `error` 这条分支的**红证**。

    注意它的可达性：内核 `validate_lines` 拒 `amount <= 0`，所以正常记账造不出不平——
    这个分支是**防御性断言**（万一将来有人放开负数分录、或 replace 掉核销实现），
    替身内核绕过校验才能造出它。写这条是为了让「不调桶凑平」这句话有能被证伪的形状：
    若有人把 `consistent` 改成恒 True、或把差额摊进桶里，这里会立刻红。
    """
    out = _report([_e("debit", "100", "2026-11-10", "C-A"),
                   _e("debit", "-30", "2026-11-10", "C-A")])
    assert out["consistent"] is False
    assert "不调桶凑合计数" in out["error"]
    # 桶里只有正的 100（负额项被跳过、没被摊掉），差额如实留在 error 里
    assert out["totalOutstanding"] == "100.00"
    assert out["accountNetBalance"] == "70.00"

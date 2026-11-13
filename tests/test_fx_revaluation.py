"""期末外币调汇（change: add-fx-revaluation 任务 2.1-2.5）。

判据（对应 spec `erp-fx-revaluation`）：

1. 期末汇率人工登记、必须带出处；同期间同币种修订留痕（后一次为准）；
2. 只调货币性项目：外币存货/固定资产不出现；
3. 科目 ↔ 6603 成对、借贷必平、业务日期=期间末日；
4. 不猜汇率（点名 missing）、无余额 noop、**落账后重跑差额为零**、汇率修订后只调新增差额；
5. 利润表单列 `fxGainLoss`，不并入 otherOpex；结转口径含 6603。

零 DB：替身内核；汇率台账走真实 EventLog（临时目录）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.fx_revaluation import FxRevaluation
from fd_erp_vertical_templates.statements import StatementBook

TENANT = "tpl-supply"
PERIOD = "2026-11"


class _StubKernel:
    """内核替身：分录 + 提案；`submit_proposal` 与内核一样按 stable_id 幂等。"""

    def __init__(self, entries: list[dict] | None = None,
                 proposals: list[dict] | None = None):
        self.entries = list(entries or [])
        self.proposals = list(proposals or [])
        self.submitted: list[dict] = []

    def query_entries(self, offset=0, limit=100, **_kw):
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


def _usd(code, direction, foreign, base, booked_on="2026-11-05", currency="USD"):
    return {"account_code": code, "direction": direction, "amount": foreign,
            "amount_base": base, "currency": currency,
            "booked_on": booked_on, "posted_at": f"{booked_on}T09:00:00+08:00",
            "txn_id": f"t-{code}-{direction}", "line_no": 1}


def _book(tmp_path, entries=None) -> FxRevaluation:
    return FxRevaluation(TENANT, tmp_path, _StubKernel(entries))


def _usd_receivable() -> list[dict]:
    """USD 1000 应收按 7.00 记 7000（分录本身借贷必平：对侧是收入）。"""
    return [_usd("1100", "debit", "1000", "7000"),
            _usd("4000", "credit", "1000", "7000")]


# ---- 1) 汇率登记 ----

def test_rates_require_a_source(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _book(tmp_path).register_rates(PERIOD, {"USD": "7.2"}, source="  ")
    assert "出处" in ei.value.message


def test_rates_revisions_keep_the_latest_and_count(tmp_path):
    book = _book(tmp_path)
    book.register_rates(PERIOD, {"USD": "7.15"}, source="pboc")
    out = book.register_rates(PERIOD, {"USD": "7.18"}, source="pboc-revised")
    assert out["rates"]["USD"]["rate"] == "7.18"
    assert out["rates"]["USD"]["source"] == "pboc-revised"
    assert book.rates(PERIOD)["revisions"] == 2


def test_base_currency_cannot_be_registered(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _book(tmp_path).register_rates(PERIOD, {"CNY": "1"}, source="pboc")
    assert "本位币" in ei.value.message


def test_bad_currency_code_and_rate_are_refused(tmp_path):
    book = _book(tmp_path)
    with pytest.raises(TemplateError):
        book.register_rates(PERIOD, {"US": "7.1"}, source="pboc")
    with pytest.raises(TemplateError):
        book.register_rates(PERIOD, {"USD": "0"}, source="pboc")


# ---- 2) 只调货币性项目 ----

def test_preview_covers_monetary_items_only(tmp_path):
    entries = _usd_receivable() + [
        _usd("1200", "debit", "500", "3500"),      # 外币存货：非货币性，不重估
        _usd("1601", "debit", "800", "5600"),      # 外币固定资产：不重估
    ]
    book = _book(tmp_path, entries)
    book.register_rates(PERIOD, {"USD": "7.2"}, source="pboc")
    pre = book.preview(PERIOD)
    assert [(r["accountCode"], r["currency"]) for r in pre["rows"]] == [("1100", "USD")]
    row = pre["rows"][0]
    assert row["foreignBalance"] == "1000.00"
    assert row["bookedBase"] == "7000.00"
    assert row["restatedBase"] == "7200.00"
    assert row["originalDifference"] == "200.00"
    assert {a["accountCode"]: a["difference"] for a in pre["adjustments"]} == {
        "1100": "200.00"}
    assert pre["missingRates"] == []


# ---- 3) 调汇提案 ----

def test_revalue_receivable_posts_a_gain_pair(tmp_path):
    book = _book(tmp_path, _usd_receivable())
    book.register_rates(PERIOD, {"USD": "7.2"}, source="pboc")
    out = book.revalue(PERIOD)
    lines = {(l["account_code"], l["direction"]): l["amount"] for l in out["lines"]}
    assert lines == {("1100", "debit"): "200.00", ("6603", "credit"): "200.00"}
    debit = sum(Decimal(v) for (c, d), v in lines.items() if d == "debit")
    credit = sum(Decimal(v) for (c, d), v in lines.items() if d == "credit")
    assert debit == credit == Decimal("200")
    assert out["bookedOn"] == "2026-11-30"
    assert out["stableId"] == "fx-reval:2026-11"
    assert out["proposal"]["source_ref"] == "fx-reval:2026-11"


def test_revalue_payable_rising_is_a_loss(tmp_path):
    entries = [_usd("2000", "credit", "1000", "7000"),
               _usd("1200", "debit", "1000", "7000")]
    book = _book(tmp_path, entries)
    book.register_rates(PERIOD, {"USD": "7.2"}, source="pboc")
    out = book.revalue(PERIOD)
    lines = {(l["account_code"], l["direction"]): l["amount"] for l in out["lines"]}
    # 负债按期末汇率变大 200 → 贷记应付 200、借记汇兑损益 200（损失）
    assert lines == {("2000", "credit"): "200.00", ("6603", "debit"): "200.00"}


# ---- 4) 不猜汇率 / 幂等 / 零差额 ----

def test_missing_rate_names_the_currency_and_refuses(tmp_path):
    entries = _usd_receivable() + [
        {"account_code": "1100", "direction": "debit", "amount": "100",
         "amount_base": "780", "currency": "EUR", "booked_on": "2026-11-06",
         "posted_at": "2026-11-06T09:00:00+08:00", "txn_id": "t-eur", "line_no": 1}]
    book = _book(tmp_path, entries)
    book.register_rates(PERIOD, {"USD": "7.2"}, source="pboc")
    with pytest.raises(TemplateError) as ei:
        book.revalue(PERIOD)
    assert ei.value.detail["missing"] == ["EUR"]
    assert "不猜汇率" in ei.value.message


def test_no_foreign_balance_is_noop(tmp_path):
    book = _book(tmp_path, [])
    book.register_rates(PERIOD, {"USD": "7.2"}, source="pboc")
    out = book.revalue(PERIOD)
    assert out["noop"] is True and out["proposal"] is None


def test_rerun_after_posting_yields_zero_difference(tmp_path):
    """落账后账上本位币已等于「外币 × 期末汇率」→ 重跑差额为零（不重复调汇）。"""
    kernel = _StubKernel(_usd_receivable())
    book = FxRevaluation(TENANT, tmp_path, kernel)
    book.register_rates(PERIOD, {"USD": "7.2"}, source="pboc")
    first = book.revalue(PERIOD)
    kernel.proposals[0]["status"] = "confirmed"
    # 模拟内核落账：把调汇分录追加进账本
    base = len(kernel.entries)
    for i, line in enumerate(first["lines"]):
        kernel.entries.append({
            "account_code": line["account_code"], "direction": line["direction"],
            "amount": line["amount"], "amount_base": line["amount"],
            "currency": "CNY", "booked_on": "2026-11-30",
            "posted_at": "2026-11-30T23:00:00+08:00", "txn_id": "t-reval",
            "source_ref": "fx-reval:2026-11", "line_no": base + i})
    again = book.revalue(PERIOD)
    assert again["noop"] is True and again["proposal"] is None
    # 待调额（按科目）为零；rows 里的 originalDifference 仍是原始差异＝追溯，不是待调额
    assert all(a["difference"] == "0.00" for a in again["adjustments"])


def test_revised_rate_after_posting_only_adjusts_the_increment(tmp_path):
    kernel = _StubKernel(_usd_receivable())
    book = FxRevaluation(TENANT, tmp_path, kernel)
    book.register_rates(PERIOD, {"USD": "7.2"}, source="pboc")
    first = book.revalue(PERIOD)
    kernel.proposals[0]["status"] = "confirmed"
    for i, line in enumerate(first["lines"]):
        kernel.entries.append({
            "account_code": line["account_code"], "direction": line["direction"],
            "amount": line["amount"], "amount_base": line["amount"],
            "currency": "CNY", "booked_on": "2026-11-30",
            "posted_at": "2026-11-30T23:00:00+08:00", "txn_id": "t-reval",
            "source_ref": "fx-reval:2026-11", "line_no": i + 1})
    book.register_rates(PERIOD, {"USD": "7.25"}, source="pboc-revised")
    second = book.revalue(PERIOD)
    lines = {(l["account_code"], l["direction"]): l["amount"]
             for l in second["lines"]}
    assert lines == {("1100", "debit"): "50.00", ("6603", "credit"): "50.00"}
    assert second["stableId"] == "fx-reval:2026-11#2"


# ---- 5) 利润表口径 ----

def test_income_statement_lists_fx_separately():
    entries = [
        {"account_code": "6603", "direction": "debit", "amount": "200",
         "amount_base": "200", "booked_on": "2026-11-30",
         "posted_at": "2026-11-30T09:00:00+08:00", "dimensions": {}},
        {"account_code": "5200", "direction": "debit", "amount": "500",
         "amount_base": "500", "booked_on": "2026-11-30",
         "posted_at": "2026-11-30T09:00:00+08:00", "dimensions": {}},
    ]
    inc = StatementBook(TENANT, _StubKernel(entries)).income_statement(PERIOD)
    assert inc["fxGainLoss"] == "200.00"
    assert inc["otherOpex"] == "500.00"        # 汇兑不混进"其他经营费用"
    assert inc["operatingExpenses"] == "700.00"
    assert inc["operatingProfit"] == "-700.00"
    assert {"line": "汇兑损益", "amount": "200.00"} in inc["lines"]

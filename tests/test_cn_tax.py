"""增值税计提与结转（change: add-cn-localization-pack 任务 3.3）。

判据（对应 spec `erp-cn-localization` 第二、三条）：

1. 价税合计 113 / 13% 拆成净额 100 + 税额 13，且两段之和等于 113（只舍入一次，不差分）；
2. 税率是封闭集，11% 被拒并列出可选值；
3. 科目码与国别包一致（2221.01.07 是转出未交增值税，2221.01.05 是出口退税）；
4. 只出待确认提案：过门前账上余额不变；
5. 月末结转 销项−进项 → 2221.01.07 / 2221.02，同期间幂等，零额/留抵 noop。

零 DB：替身内核只记提案。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.cn_tax import (CnVatLane, INPUT_VAT, OUTPUT_VAT,
                                             TRANSFER_OUT, VAT_UNPAID, split_tax)
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.localization import CnPack

TENANT = "tpl-cn"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self):
        self.entries: list[dict] = []
        self.submitted: list[dict] = []

    def query_entries(self, offset=0, limit=100, **_kw):
        return {"entries": self.entries[offset:offset + limit],
                "rows_returned": len(self.entries)}

    def submit_proposal(self, stable_id, lines, **kw):
        prop = {"proposal_id": len(self.submitted) + 1, "stable_id": stable_id,
                "status": "pending", "lines": lines, **kw}
        for old in self.submitted:
            if old["stable_id"] == stable_id:            # 内核按 stable_id 幂等
                return {**old, "duplicate": True}
        self.submitted.append(prop)
        return prop


def _lane(kernel=None) -> CnVatLane:
    return CnVatLane(TENANT, kernel or _StubKernel(), pack=CnPack())


def _lines(prop) -> dict:
    return {(l["account_code"], l["direction"]): l["amount"] for l in prop["lines"]}


# ---- 1) 价税分离 ----

def test_gross_113_at_13_percent_splits_without_a_cent_gap():
    out = split_tax("113", "13", gross=True)
    assert out == {"gross": "113.00", "net": "100.00", "tax": "13.00",
                   "basis": "gross", "rate": "13"}
    assert Decimal(out["net"]) + Decimal(out["tax"]) == Decimal(out["gross"])


def test_gross_split_never_rounds_twice():
    """99.99 / 13%：分别舍入会得到 net 88.49 + tax 11.50 ≠ 99.99。"""
    out = split_tax("99.99", "13", gross=True)
    assert Decimal(out["net"]) + Decimal(out["tax"]) == Decimal("99.99")


def test_net_basis_adds_tax_on_top():
    assert split_tax("100", "13") == {"gross": "113.00", "net": "100.00",
                                      "tax": "13.00", "basis": "net", "rate": "13"}


def test_zero_rate_has_no_tax():
    assert split_tax("100", "0", gross=True)["tax"] == "0.00"


def test_rate_outside_the_closed_set_is_refused():
    with pytest.raises(TemplateError) as ei:
        split_tax("100", "11")
    assert ei.value.detail["allowed"] == ["13", "9", "6", "5", "3", "0"]


def test_negative_amount_is_refused():
    with pytest.raises(TemplateError):
        split_tax("-100", "13")


# ---- 2) 科目码来自数据包 ----

def test_codes_match_the_country_pack():
    lane = _lane()
    codes = lane.account_codes()
    assert codes["transferOut"] == "2221.01.07"
    assert lane._pack.account("2221.01.07")["name"].endswith("转出未交增值税）")
    assert lane._pack.account("2221.01.05")["name"].endswith("出口退税）")


def test_a_pack_missing_a_vat_account_is_refused():
    class _Pack:
        def account(self, code):
            return None
    with pytest.raises(TemplateError) as ei:
        CnVatLane(TENANT, _StubKernel(), pack=_Pack()).account_codes()
    assert ei.value.detail["missing"]


# ---- 3) 计提只出提案 ----

def test_sale_posts_output_vat_against_revenue():
    kernel = _StubKernel()
    lane = _lane(kernel)
    out = lane.post_sale("S-1", "113", rate="13", gross=True,
                         customer="C-A", booked_on="2026-11-10")
    assert out["net"] == "100.00" and out["tax"] == "13.00"
    assert _lines(out["proposal"]) == {
        ("1100", "debit"): "113.00", ("4000", "credit"): "100.00",
        (OUTPUT_VAT, "credit"): "13.00"}
    assert out["proposal"]["dimensions"] == {"customer": "C-A"}
    assert out["proposal"]["booked_on"] == "2026-11-10"
    # 账上没有分录——只有待确认提案
    assert kernel.entries == []


def test_purchase_books_input_vat():
    kernel = _StubKernel()
    out = _lane(kernel).post_purchase("P-1", "113", rate="13", gross=True,
                                      booked_on="2026-11-11")
    assert _lines(out["proposal"]) == {
        ("5000", "debit"): "100.00", (INPUT_VAT, "debit"): "13.00",
        ("2000", "credit"): "113.00"}


def test_same_ref_is_idempotent():
    kernel = _StubKernel()
    lane = _lane(kernel)
    first = lane.post_sale("S-1", "113", rate="13", gross=True,
                           booked_on="2026-11-10")
    again = lane.post_sale("S-1", "113", rate="13", gross=True,
                           booked_on="2026-11-10")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert len(kernel.submitted) == 1


def test_zero_rate_sale_has_only_two_lines():
    prop = _lane().post_sale("S-0", "100", rate="0", booked_on="2026-11-10")["proposal"]
    assert _lines(prop) == {("1100", "debit"): "100.00", ("4000", "credit"): "100.00"}


# ---- 4) 月末结转 ----

def _with_ledger(output: str, inputs: str) -> _StubKernel:
    kernel = _StubKernel()
    kernel.entries = [
        {"account_code": OUTPUT_VAT, "direction": "credit", "amount": output,
         "amount_base": output, "booked_on": "2026-11-10", "txn_id": "t1", "line_no": 1},
        {"account_code": INPUT_VAT, "direction": "debit", "amount": inputs,
         "amount_base": inputs, "booked_on": "2026-11-11", "txn_id": "t2", "line_no": 1},
    ]
    return kernel


def test_position_reports_output_input_and_payable():
    pos = _lane(_with_ledger("130", "65")).position(PERIOD)
    assert pos["outputVat"] == "130.00" and pos["inputVat"] == "65.00"
    assert pos["payable"] == "65.00"


def test_close_transfers_payable_to_unpaid_vat():
    kernel = _with_ledger("130", "65")
    out = _lane(kernel).close_vat(PERIOD)
    assert out["noop"] is False and out["amount"] == "65.00"
    assert _lines(kernel.submitted[0]) == {
        (TRANSFER_OUT, "debit"): "65.00", (VAT_UNPAID, "credit"): "65.00"}
    assert kernel.submitted[0]["booked_on"] == "2026-11-30"
    assert kernel.submitted[0]["source_ref"] == "vat-close:2026-11"


def test_close_is_idempotent_per_period():
    kernel = _with_ledger("130", "65")
    lane = _lane(kernel)
    lane.close_vat(PERIOD)
    again = lane.close_vat(PERIOD)
    assert again["duplicate"] is True
    assert len(kernel.submitted) == 1


def test_credit_carry_forward_is_a_noop():
    kernel = _with_ledger("40", "100")            # 进项 > 销项：留抵
    out = _lane(kernel).close_vat(PERIOD)
    assert out["noop"] is True and out["creditCarryForward"] == "60.00"
    assert kernel.submitted == []


def test_close_requires_a_valid_period():
    with pytest.raises(TemplateError):
        _lane().close_vat("2026/11")


# ---- 6) 进项税额转出（change: add-cn-red-letter-and-input-transfer-out）----

def _with_entries(rows: list[dict], kernel=None) -> CnVatLane:
    k = kernel or _StubKernel()
    k.entries = list(rows)
    return _lane(k)


def _row(code, direction, amount, when=PERIOD + "-10"):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "txn_id": f"t-{code}-{direction}-{when}", "line_no": 1}


def _month(output="130", inputs="65") -> list[dict]:
    """销项 130 / 进项 65 → 应纳 65。"""
    return [_row(OUTPUT_VAT, "credit", output), _row(INPUT_VAT, "debit", inputs)]


def test_transfer_out_raises_payable_and_names_the_reason():
    kernel = _StubKernel()
    lane = _with_entries(_month(), kernel)
    out = lane.transfer_input_out(
        PERIOD, reason="collective-welfare", amount="13", ref="W-1")
    assert out["reasonZh"] == "集体福利或个人消费"
    assert out["costAccount"] == "5200"
    assert _lines(kernel.submitted[0]) == {
        ("5200", "debit"): "13.00", ("2221.01.04", "credit"): "13.00"}
    assert out["remainingAfter"] == "52.00"


def test_position_includes_the_transfer_out():
    lane = _with_entries(_month() + [_row("2221.01.04", "credit", "13")])
    pos = lane.position(PERIOD)
    assert pos["outputVat"] == "130.00" and pos["inputVat"] == "65.00"
    assert pos["inputTransferOut"] == "13.00"
    assert pos["payable"] == "78.00"          # 65 + 13，不是 65


def test_transferring_more_than_deducted_is_refused_with_the_room():
    lane = _with_entries(_month())
    with pytest.raises(TemplateError) as ei:
        lane.transfer_input_out(PERIOD, reason="abnormal-loss", amount="100",
                                ref="W-2")
    assert ei.value.detail["remaining"] == "65.00"
    assert "超过本期可转出额" in ei.value.message


def test_transfer_out_is_idempotent_per_reference():
    kernel = _StubKernel()
    lane = _with_entries(_month(), kernel)
    first = lane.transfer_input_out(PERIOD, reason="tax-exempt", amount="5",
                                    ref="W-3")
    again = lane.transfer_input_out(PERIOD, reason="tax-exempt", amount="5",
                                    ref="W-3")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert len(kernel.submitted) == 1


def test_transfer_out_refuses_a_non_profit_and_loss_counterparty():
    lane = _with_entries(_month())
    with pytest.raises(TemplateError) as ei:
        lane.transfer_input_out(PERIOD, reason="abnormal-loss", amount="5",
                                ref="W-4", cost_account="1200")
    assert "不是损益类" in ei.value.message
    assert "5200" in ei.value.detail["allowed"]


def test_transfer_out_refuses_an_unknown_reason():
    lane = _with_entries(_month())
    with pytest.raises(TemplateError) as ei:
        lane.transfer_input_out(PERIOD, reason="办公室喝水", amount="5", ref="W-5")
    assert set(ei.value.detail["allowed"]) == {
        "collective-welfare", "abnormal-loss", "tax-exempt", "simplified"}


def test_transfer_out_consumes_the_room_across_documents():
    """两张单合计不能超过本期进项：第一张 60、第二张 10 拒（只剩 5）。"""
    kernel = _StubKernel()
    lane = _with_entries(_month(), kernel)
    lane.transfer_input_out(PERIOD, reason="abnormal-loss", amount="60", ref="W-6")
    kernel.entries.append(_row("2221.01.04", "credit", "60"))
    with pytest.raises(TemplateError) as ei:
        lane.transfer_input_out(PERIOD, reason="abnormal-loss", amount="10",
                                ref="W-7")
    assert ei.value.detail["remaining"] == "5.00"


def test_close_vat_follows_the_new_caliber():
    """留抵月转出后变成应纳：结转必须按「销项 − 进项 + 转出」出提案。"""
    kernel = _StubKernel()
    lane = _with_entries(_month(output="40", inputs="100")
                         + [_row("2221.01.04", "credit", "70")], kernel)
    out = lane.close_vat(PERIOD)
    assert out["payable"] == "10.00"          # 40 − 100 + 70
    assert _lines(kernel.submitted[0]) == {
        ("2221.01.07", "debit"): "10.00", ("2221.02", "credit"): "10.00"}

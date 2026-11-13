"""附加税与所得税预缴（change: add-cn-surcharge-and-income-tax 任务 2.5）。

判据（对应 spec `erp-cn-localization` 新增四条）：

1. 税率是外部事实：无出处拒、城建税只许 7/5/1、减免系数 ∈[0,1]、按生效期间取最近一次、缺率拒（不猜）；
2. 附加税基数 = **应纳增值税**（不是销售额/销项），留抵为 0 → noop；
3. 所得税按**本年累计利润**算目标、减去已预缴得本期应缴（第二个月只补差额），亏损 noop；
4. 月度税表三税一表并与账面勾稽；口径自报「未做纳税调整」。

零 DB：替身内核只记提案。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.cn_filing import CnFilingLane
from fd_erp_vertical_templates.errors import TemplateError

TENANT = "tpl-filing"
P1 = "2026-11"
P2 = "2026-12"


class _StubKernel:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = list(rows or [])
        self.submitted: list[dict] = []

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}

    def submit_proposal(self, stable_id, lines, **kw):
        for old in self.submitted:
            if old["stable_id"] == stable_id:
                return {**old, "duplicate": True}
        prop = {"proposal_id": len(self.submitted) + 1, "stable_id": stable_id,
                "lines": lines, **kw}
        self.submitted.append(prop)
        return prop


def _e(code, direction, amount, when=P1 + "-10"):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "txn_id": f"t-{code}-{direction}-{when}", "line_no": 1}


def _vat_month(output="130", inputs="65", when=P1 + "-10") -> list[dict]:
    """销项 130 / 进项 65 → 应纳 65。"""
    return [_e("2221.01.01", "credit", output, when),
            _e("2221.01.02", "debit", inputs, when)]


RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


def _lane(tmp_path, rows=None, *, rates=RATES,
          effective_from="2026-01") -> tuple[CnFilingLane, _StubKernel]:
    # 每个用例独立事件档：共用 /tmp 会让税率在用例之间串（踩过一次，8 条假红）
    kernel = _StubKernel(rows if rows is not None else _vat_month())
    lane = CnFilingLane(TENANT, tmp_path, kernel)
    if rates is not None:
        lane.register_rates(rates, effective_from=effective_from, source="现行税法")
    return lane, kernel


def _lines(prop) -> dict:
    return {(l["account_code"], l["direction"]): l["amount"] for l in prop["lines"]}


# ---- 1) 税率 ----

def test_city_rate_outside_the_three_brackets_is_refused(tmp_path):
    lane, _k = _lane(tmp_path, rates=None)
    with pytest.raises(TemplateError) as ei:
        lane.register_rates({**RATES, "city_construction": "6"},
                            effective_from="2026-01", source="税法")
    assert ei.value.detail["allowed"] == ["7", "5", "1"]


def test_rates_need_a_source(tmp_path):
    lane, _k = _lane(tmp_path, rates=None)
    with pytest.raises(TemplateError):
        lane.register_rates(RATES, effective_from="2026-01", source="  ")


def test_relief_must_be_in_unit_interval(tmp_path):
    lane, _k = _lane(tmp_path, rates=None)
    with pytest.raises(TemplateError):
        lane.register_rates({**RATES, "surcharge_relief": "1.5"},
                            effective_from="2026-01", source="税法")


def test_rates_are_taken_by_effective_period(tmp_path):
    lane, _k = _lane(tmp_path, rates=None)
    lane.register_rates({**RATES, "city_construction": "7"},
                        effective_from="2026-01", source="旧")
    lane.register_rates({**RATES, "city_construction": "5"},
                        effective_from="2026-07", source="新")
    assert lane.rates("2026-06")["rates"]["city_construction"] == "7"
    assert lane.rates("2026-08")["rates"]["city_construction"] == "5"


def test_missing_rates_are_refused_not_defaulted(tmp_path):
    lane, _k = _lane(tmp_path, rates=None)
    lane.register_rates({"city_construction": "7"}, effective_from="2026-01",
                        source="税法")
    with pytest.raises(TemplateError) as ei:
        lane.surcharges(P1)
    assert ei.value.code == "NOT_WIRED"
    assert set(ei.value.detail["missing"]) == {"education_surcharge",
                                               "local_education_surcharge",
                                               "income_tax"}


def test_unknown_rate_key_is_refused(tmp_path):
    lane, _k = _lane(tmp_path, rates=None)
    with pytest.raises(TemplateError) as ei:
        lane.register_rates({"vat": "13"}, effective_from="2026-01", source="税法")
    assert "vat" in ei.value.message


# ---- 2) 附加税 ----

def test_surcharges_use_payable_vat_as_the_base(tmp_path):
    lane, kernel = _lane(tmp_path)
    out = lane.surcharges(P1)
    assert out["base"] == "65.00"                 # 应纳，不是销项 130、更不是销售额
    assert [(i["label"], i["amount"]) for i in out["items"]] == [
        ("城市维护建设税", "4.55"), ("教育费附加", "1.95"), ("地方教育附加", "1.30")]
    assert out["total"] == "7.80"
    assert _lines(kernel.submitted[0]) == {
        ("5403", "debit"): "7.80", ("2221.12", "credit"): "4.55",
        ("2221.13", "credit"): "1.95", ("2221.14", "credit"): "1.30"}


def test_a_credit_carry_forward_month_has_no_surcharge(tmp_path):
    lane, kernel = _lane(tmp_path, _vat_month(output="40", inputs="100"))
    out = lane.surcharges(P1)
    assert out["noop"] is True and out["total"] == "0.00"
    assert kernel.submitted == []


def test_half_relief_halves_every_surcharge(tmp_path):
    lane, _k = _lane(tmp_path, rates={**RATES, "surcharge_relief": "0.5"})
    out = lane.surcharges(P1)
    assert [i["amount"] for i in out["items"]] == ["2.28", "0.98", "0.65"]


def test_surcharge_is_idempotent_per_period(tmp_path):
    lane, kernel = _lane(tmp_path)
    lane.surcharges(P1)
    again = lane.surcharges(P1)
    # 同一个 stable_id 只落一份提案：第二次是 duplicate，不是又提一笔
    assert again["duplicate"] is True and len(kernel.submitted) == 1


# ---- 3) 所得税预缴 ----

def _profit_rows(month: str, profit: str) -> list[dict]:
    """造一个「收入 − 费用 = profit」的月份。"""
    return [_e("4000", "credit", profit, month + "-10"),
            _e("1100", "debit", profit, month + "-10")]


def test_first_month_prepays_on_cumulative_profit(tmp_path):
    rows = _vat_month() + _profit_rows("2026-11", "100")
    lane, kernel = _lane(tmp_path, rows)
    out = lane.income_tax_prepay("2026-11")
    assert out["cumulativeProfit"] == "100.00"
    assert out["target"] == "25.00" and out["due"] == "25.00"
    assert _lines(kernel.submitted[0]) == {("5801", "debit"): "25.00",
                                           ("2221.11", "credit"): "25.00"}


def test_second_month_only_tops_up_the_difference(tmp_path):
    """1 月利润 100 已预缴 25；2 月累计 200 → 应预缴 25（不是 50）。"""
    rows = (_vat_month() + _profit_rows("2026-11", "100")
            + [{"account_code": "2221.11", "direction": "credit", "amount": "25",
                "amount_base": "25", "booked_on": "2026-11-30", "txn_id": "t-tax",
                "line_no": 1},
               {"account_code": "5801", "direction": "debit", "amount": "25",
                "amount_base": "25", "booked_on": "2026-11-30", "txn_id": "t-tax",
                "line_no": 2}]
            + _profit_rows("2026-12", "100"))
    lane, _k = _lane(tmp_path, rows)
    out = lane.income_tax_prepay("2026-12")
    assert out["cumulativeProfit"] == "200.00"
    assert out["target"] == "50.00" and out["alreadyPrepaid"] == "25.00"
    assert out["due"] == "25.00"


def test_a_loss_month_does_not_prepay(tmp_path):
    rows = [_e("5200", "debit", "300", "2026-11-10"),
            _e("1100", "credit", "300", "2026-11-10")]
    lane, kernel = _lane(tmp_path, rows)
    out = lane.income_tax_prepay("2026-11")
    assert out["noop"] is True and out["due"] == "0.00"
    assert kernel.submitted == []


def test_prepay_never_reverses_an_overpayment(tmp_path):
    rows = (_profit_rows("2026-11", "100")
            + [{"account_code": "2221.11", "direction": "credit", "amount": "80",
                "amount_base": "80", "booked_on": "2026-11-30", "txn_id": "t-tax",
                "line_no": 1}])
    lane, kernel = _lane(tmp_path, rows)
    out = lane.income_tax_prepay("2026-11")
    assert out["noop"] is True                 # 目标 25 < 已预缴 80
    assert out["overPrepaid"] == "55.00"
    assert kernel.submitted == []
    assert "未做纳税调整" in out["caliberNote"]


def test_prepay_is_idempotent_per_period(tmp_path):
    rows = _vat_month() + _profit_rows("2026-11", "100")
    lane, kernel = _lane(tmp_path, rows)
    lane.income_tax_prepay("2026-11")
    again = lane.income_tax_prepay("2026-11")
    assert again["duplicate"] is True


# ---- 4) 月度税表 ----

def test_filing_summary_puts_three_taxes_on_one_page(tmp_path):
    rows = _vat_month() + _profit_rows("2026-11", "100")
    lane, _k = _lane(tmp_path, rows)
    out = lane.filing_summary(P1)
    assert out["vat"]["payable"] == "65.00"
    assert out["surcharges"]["base"] == "65.00"
    assert out["surcharges"]["total"] == "7.80"
    assert out["incomeTax"] == {"cumulativeProfit": "100.00", "target": "25.00",
                                "alreadyPrepaid": "0.00", "accrued": "25.00",
                                "due": "25.00"}
    assert out["consistent"] is False          # 账上还没计提附加税与所得税
    diffs = {r["item"]: r["difference"] for r in out["reconciliation"]}
    assert diffs == {"surcharge": "-7.80", "incomeTax": "-25.00"}
    assert "申报**数据**到此为止" in out["caliberNote"]


def test_filing_summary_reconciles_once_the_entries_are_booked(tmp_path):
    """账做对（附加税先计提，所得税按计提后的利润总额算）→ 两项差额全为 0。

    注意这里**不是** 25：附加税 7.80 计入损益后利润总额变 92.20，所得税目标随之变 23.05。
    这既是真实顺序（申报表上税金及附加在利润总额之前），也是「先附加税、后所得税」
    这条依赖必须写进用例的原因——按 100 算出来的 25 计提，勾稽会稳定差 1.95。
    """
    rows = (_vat_month() + _profit_rows("2026-11", "100")
            + [_e("5403", "debit", "7.80"), _e("2221.12", "credit", "4.55"),
               _e("2221.13", "credit", "1.95"), _e("2221.14", "credit", "1.30"),
               _e("5801", "debit", "23.05"), _e("2221.11", "credit", "23.05")])
    lane, _k = _lane(tmp_path, rows)
    out = lane.filing_summary(P1)
    assert out["incomeTax"]["cumulativeProfit"] == "92.20"
    assert out["incomeTax"]["target"] == "23.05"
    assert out["consistent"] is True
    assert {r["difference"] for r in out["reconciliation"]} == {"0.00"}


def test_surcharge_booking_lowers_the_income_tax_target(tmp_path):
    """附加税是税前扣除项：计提后所得税目标按 92.20 算，不是按 100。

    判据对「先附加税、后所得税」这条顺序敏感：反过来算（先按 100 提 25 再提附加税）
    会让本月的所得税多缴 1.95，而两张表各自都自洽、报不出错。
    """
    rows = _vat_month() + _profit_rows("2026-11", "100")
    lane, _k = _lane(tmp_path, rows)
    before = lane.income_tax_prepay(P1)
    assert before["target"] == "25.00"
    rows += [_e("5403", "debit", "7.80"), _e("2221.12", "credit", "4.55"),
             _e("2221.13", "credit", "1.95"), _e("2221.14", "credit", "1.30")]
    lane2, _k2 = _lane(tmp_path, rows)
    after = lane2.income_tax_prepay(P1)
    assert after["cumulativeProfit"] == "92.20"
    assert after["target"] == "23.05"

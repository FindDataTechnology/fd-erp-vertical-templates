"""增值税车道（change: add-cn-localization-pack 任务 3）。

中国客户记账绕不开增值税：销售要计提销项、采购要确认进项、月末把「销项 − 进项」转到未交增值税。
没有这条车道，销项只能挤进收入（收入虚增）、进项只能挤进成本（成本虚增）——两处同时错，
而利润表「看起来正常」。

## 科目码来自数据，不来自记忆

六个科目码取自 `localization/cn` 数据包（Odoo `l10n_cn` 实测）。本车道最初按记忆写成
`2221.01.05 转出未交增值税`，实测那是**出口退税**，正确的是 `2221.01.07`。所以这里直接断言
科目码与数据包一致，不一致就拒——法定口径的编码不该靠人名记。

## 三条纪律

1. **税率是封闭集**（13/9/6/5/3/0）。负数档（加计抵减/差额征税）本版不做，传进来就拒并说清。
2. **价税分离只四舍五入一次**：价税合计口径先算 `净额 = 合计 ÷ (1 + 税率)`，再 `税额 = 合计 − 净额`。
   分别把净额和税额各自四舍五入会差一分钱，而这一分钱会在申报时变成「账表不符」。
3. **只出待确认 proposal**，且以凭证号 `ref` 幂等：同一张单据重提不会记两遍。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_period, period_end
from .errors import TemplateError
from .kernel_rows import read_entries

Q2 = Decimal("0.01")
SCHEMA = "fd-erp-vertical-templates/cn-vat/v1"

#: 增值税科目（码取自 localization/cn 数据包；数据包变了这里应该跟着红）。
TAX_ROOT = "2221"
VAT_PAYABLE = "2221.01"
OUTPUT_VAT = "2221.01.01"
INPUT_VAT = "2221.01.02"
TRANSFER_OUT = "2221.01.07"
VAT_UNPAID = "2221.02"

#: 本版接的税率档（封闭集）。负数档（加计抵减/差额征税）不做。
RATES = ("13", "9", "6", "5", "3", "0")

#: 默认对方科目：赊销挂应收、赊购挂应付；现销/现购由调用方显式传 1000。
DEFAULT_AR = "1100"
DEFAULT_AP = "2000"
DEFAULT_REVENUE = "4000"
DEFAULT_COST = "5000"

CLOSE_PREFIX = "vat-close:"
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}") from None


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-11"})
    return key


def _check_day(value: Any, what: str = "业务日期") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    if _PERIOD_RE.match(raw):
        return period_end(raw)
    raise TemplateError("INVALID_ARGUMENT", f"{what}须为 YYYY-MM-DD 或 YYYY-MM：{value!r}")


def _rate(value: Any) -> str:
    raw = str(value if value not in (None, "") else "0").strip().rstrip("%")
    if raw not in RATES:
        raise TemplateError(
            "INVALID_ARGUMENT",
            f"税率 {value!r} 不在本版封闭集内：{'/'.join(RATES)}"
            "（负数档加计抵减/差额征税未实现）",
            detail={"allowed": list(RATES), "received": str(value)})
    return raw


def split_tax(amount: Any, rate: Any, *, gross: bool = False) -> dict[str, str]:
    """按税额分离。`gross=True` 表示传入的是价税合计。

    合计口径只四舍五入一次：先算净额，税额 = 合计 − 净额。分别舍入会差一分。
    """
    total = _dec(amount, "金额")
    if total <= 0:
        raise TemplateError("INVALID_ARGUMENT", f"金额须大于零：{amount!r}")
    r = _dec(_rate(rate), "税率") / Decimal(100)
    if gross:
        net = (total / (Decimal(1) + r)).quantize(Q2, rounding=ROUND_HALF_UP)
        tax = (total - net).quantize(Q2, rounding=ROUND_HALF_UP)
        return {"gross": _fmt(total), "net": _fmt(net), "tax": _fmt(tax),
                "basis": "gross", "rate": str(rate)}
    net = total.quantize(Q2, rounding=ROUND_HALF_UP)
    tax = (net * r).quantize(Q2, rounding=ROUND_HALF_UP)
    return {"gross": _fmt(net + tax), "net": _fmt(net), "tax": _fmt(tax),
            "basis": "net", "rate": str(rate)}


class CnVatLane:
    """增值税计提与月末结转（只出待确认提案）。"""

    def __init__(self, tenant_id: str, kernel: Any, *, base_currency: str = "CNY",
                 pack: Any = None):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency
        self._pack = pack

    # ---- 科目码自检 ----

    def account_codes(self) -> dict[str, str]:
        codes = {"root": TAX_ROOT, "vatPayable": VAT_PAYABLE, "output": OUTPUT_VAT,
                 "input": INPUT_VAT, "transferOut": TRANSFER_OUT,
                 "unpaid": VAT_UNPAID}
        if self._pack is not None:
            missing = [c for c in codes.values() if self._pack.account(c) is None]
            if missing:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"增值税科目 {', '.join(missing)} 不在国别包里：数据包变了，"
                    "先核对 localization/cn 再改本车道",
                    detail={"missing": missing})
        return codes

    # ---- 计提 ----

    def post_sale(self, ref: Any, amount: Any, *, rate: Any = "13", gross: bool = False,
                  settle_account: str = DEFAULT_AR,
                  revenue_account: str = DEFAULT_REVENUE,
                  booked_on: Any = None, customer: str | None = None,
                  submitted_by: str = "vat-engine") -> dict[str, Any]:
        """销售计提销项：借 应收（含税）/ 贷 收入（净额）+ 贷 销项税额。"""
        tag = str(ref or "").strip()
        if not tag:
            raise TemplateError("INVALID_ARGUMENT", "凭证号 ref 不能为空（幂等键就是它）")
        self.account_codes()
        parts = split_tax(amount, rate, gross=gross)
        day = _check_day(booked_on, "业务日期")
        lines = [{"account_code": settle_account, "direction": "debit",
                  "amount": parts["gross"], "currency": self._base},
                 {"account_code": revenue_account, "direction": "credit",
                  "amount": parts["net"], "currency": self._base}]
        if Decimal(parts["tax"]) > 0:
            lines.append({"account_code": OUTPUT_VAT, "direction": "credit",
                          "amount": parts["tax"], "currency": self._base})
        dims = {"customer": str(customer).strip()} if customer else None
        proposal = self._kernel.submit_proposal(
            f"vat-sale:{tag}", lines, source_ref=f"vat-sale:{tag}", kind="journal",
            lane_title=f"{tag} 含税销售", dimensions=dims,
            booked_on=day, submitted_by=submitted_by)
        return {**parts, "ref": tag, "proposal": proposal,
                "stableId": f"vat-sale:{tag}", "bookedOn": day,
                "duplicate": bool(proposal and proposal.get("duplicate")),
                "caliber_note": "价税分离后：收入为净额，销项税额单列；只出待确认提案"}

    def post_purchase(self, ref: Any, amount: Any, *, rate: Any = "13",
                      gross: bool = False, settle_account: str = DEFAULT_AP,
                      cost_account: str = DEFAULT_COST, booked_on: Any = None,
                      deductible: bool = True,
                      submitted_by: str = "vat-engine") -> dict[str, Any]:
        """采购确认进项：借 成本（净额）+ 借 进项税额 / 贷 应付（含税）。

        `deductible=False` 时**税额并入成本**，不出进项行：普通发票的税是价款的一部分，
        不是资产。默认 True 只是为兼容直连调用；正确的判定在发票台账里
        （`cn_invoice.CnInvoiceBook.purchase` 按票种 + 认证决定），直连调用须自证可抵。
        """
        tag = str(ref or "").strip()
        if not tag:
            raise TemplateError("INVALID_ARGUMENT", "凭证号 ref 不能为空（幂等键就是它）")
        self.account_codes()
        parts = split_tax(amount, rate, gross=gross)
        day = _check_day(booked_on, "业务日期")
        if deductible:
            lines = [{"account_code": cost_account, "direction": "debit",
                      "amount": parts["net"], "currency": self._base}]
            if Decimal(parts["tax"]) > 0:
                lines.append({"account_code": INPUT_VAT, "direction": "debit",
                              "amount": parts["tax"], "currency": self._base})
        else:
            # 不可抵扣：税与净额一起进成本（不挂待抵扣，那会让资产虚增且永远抵不掉）
            lines = [{"account_code": cost_account, "direction": "debit",
                      "amount": parts["gross"], "currency": self._base}]
        lines.append({"account_code": settle_account, "direction": "credit",
                      "amount": parts["gross"], "currency": self._base})
        proposal = self._kernel.submit_proposal(
            f"vat-buy:{tag}", lines, source_ref=f"vat-buy:{tag}", kind="journal",
            lane_title=f"{tag} 含税采购", booked_on=day, submitted_by=submitted_by)
        return {**parts, "ref": tag, "proposal": proposal, "deductible": deductible,
                "stableId": f"vat-buy:{tag}", "bookedOn": day,
                "duplicate": bool(proposal and proposal.get("duplicate")),
                "caliber_note": ("进项税额按可抵扣口径单列；只出待确认提案" if deductible
                                 else "该票不可抵扣：税额并入成本，不出进项行")}

    # ---- 账面口径 ----

    def position(self, period: Any) -> dict[str, Any]:
        """该期间销项/进项发生额与应纳税额（账面口径，不做任何调整）。"""
        key = _check_period(period)
        output = Decimal(0)
        inputs = Decimal(0)
        for row in read_entries(self._kernel):
            if entry_period(row) != key:
                continue
            code = str(row.get("account_code") or "")
            amount = _dec(row.get("amount_base") or row.get("amount"), "金额")
            if code == OUTPUT_VAT:
                output += amount if row.get("direction") == "credit" else -amount
            elif code == INPUT_VAT:
                inputs += amount if row.get("direction") == "debit" else -amount
        payable = output - inputs
        return {"period": key, "outputVat": _fmt(output), "inputVat": _fmt(inputs),
                "payable": _fmt(payable),
                "creditCarryForward": _fmt(-payable) if payable < 0 else "0.00"}

    # ---- 月末结转 ----

    def close_vat(self, period: Any, *, submitted_by: str = "vat-engine"
                  ) -> dict[str, Any]:
        """把「销项 − 进项」转到未交增值税：借 2221.01.07 / 贷 2221.02。

        差值为负（留抵）时不结转：留抵是挂在进项里的资产性余额，硬转成「未交」负数没有意义。
        """
        key = _check_period(period)
        self.account_codes()
        pos = self.position(key)
        payable = Decimal(pos["payable"])
        if payable <= 0:
            return {"period": key, "noop": True, "amount": "0.00", **pos,
                    "caliber_note": "本期无应交增值税（销项 ≤ 进项），留抵结转下期，不生成提案"}
        amount = _fmt(payable)
        stable_id = f"{CLOSE_PREFIX}{key}"
        proposal = self._kernel.submit_proposal(
            stable_id,
            [{"account_code": TRANSFER_OUT, "direction": "debit",
              "amount": amount, "currency": self._base},
             {"account_code": VAT_UNPAID, "direction": "credit",
              "amount": amount, "currency": self._base}],
            source_ref=f"{CLOSE_PREFIX}{key}", kind="journal",
            lane_title=f"{key} 转出未交增值税", booked_on=period_end(key),
            submitted_by=submitted_by)
        return {"period": key, "noop": False, "amount": amount, **pos,
                "stableId": stable_id, "proposal": proposal,
                "bookedOn": period_end(key),
                "duplicate": bool(proposal and proposal.get("duplicate")),
                "caliber_note": "转出后 2221.01 归零、2221.02 挂待缴数；缴纳是资金动作，本车道不做"}


__all__ = ["CnVatLane", "split_tax", "RATES", "OUTPUT_VAT", "INPUT_VAT",
           "TRANSFER_OUT", "VAT_UNPAID", "TAX_ROOT"]

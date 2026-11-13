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
#: 进项税额转出（change: add-cn-red-letter-and-input-transfer-out）。与 `TRANSFER_OUT`
#: （2221.01.07 转出**未交**增值税）是两件事：前者把不可抵的进项转出去，后者把应交额转到未交户。
INPUT_TRANSFER_OUT = "2221.01.04"
TRANSFER_OUT = "2221.01.07"
VAT_UNPAID = "2221.02"

#: 进项转出的原因封闭集与默认对方科目。对方科目必须是损益类——转出额的实质是
#: 「这笔进项变成了费用」，记到资产或负债上会让报表口径失真。
TRANSFER_OUT_REASONS: dict[str, dict[str, str]] = {
    "collective-welfare": {"zh": "集体福利或个人消费", "account": "5200"},
    "abnormal-loss": {"zh": "非正常损失", "account": "5711"},
    "tax-exempt": {"zh": "免税项目", "account": "5000"},
    "simplified": {"zh": "简易计税项目", "account": "5000"},
}
TRANSFER_OUT_PREFIX = "input-transfer-out:"

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
                 "input": INPUT_VAT, "inputTransferOut": INPUT_TRANSFER_OUT,
                 "transferOut": TRANSFER_OUT, "unpaid": VAT_UNPAID}
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

    def _period_sums(self, period: str) -> dict[str, Decimal]:
        """本期三个增值税明细科目的净额（贷正借负，与 `_sums` 同一约定）。

        一个期间只读一遍分录：`position()` 与「可转出额」都要这三个数，
        分两次读会让两者在并发写入时看到不同快照。
        """
        out = {OUTPUT_VAT: Decimal(0), INPUT_VAT: Decimal(0),
               INPUT_TRANSFER_OUT: Decimal(0)}
        for row in read_entries(self._kernel):
            if entry_period(row) != period:
                continue
            code = str(row.get("account_code") or "")
            if code not in out:
                continue
            amount = _dec(row.get("amount_base") or row.get("amount"), "金额")
            out[code] += amount if row.get("direction") == "credit" else -amount
        return out

    def position(self, period: Any) -> dict[str, Any]:
        """该期间销项/进项/进项转出与应纳税额（账面口径，不做任何调整）。

        应纳 = 销项 − 进项 **+ 进项转出**。漏掉最后一项，用于集体福利或发生非正常损失的
        那部分进项就白抵了，而附加税以应纳为基数——少缴会一路传导。
        """
        key = _check_period(period)
        sums = self._period_sums(key)
        output = sums[OUTPUT_VAT]
        inputs = -sums[INPUT_VAT]
        transferred = sums[INPUT_TRANSFER_OUT]
        payable = output - inputs + transferred
        return {"period": key, "outputVat": _fmt(output), "inputVat": _fmt(inputs),
                "inputTransferOut": _fmt(transferred), "payable": _fmt(payable),
                "creditCarryForward": _fmt(-payable) if payable < 0 else "0.00"}

    # ---- 进项税额转出 ----

    def transferable_input(self, period: Any) -> dict[str, Any]:
        """本期还能转出多少：已抵扣进项 − 已转出。负值不出现（转出超过进项时按 0 报）。"""
        key = _check_period(period)
        sums = self._period_sums(key)
        inputs = -sums[INPUT_VAT]
        transferred = sums[INPUT_TRANSFER_OUT]
        remaining = inputs - transferred
        return {"period": key, "inputVat": _fmt(inputs),
                "transferred": _fmt(transferred),
                "remaining": _fmt(remaining if remaining > 0 else Decimal(0)),
                "overTransferred": (_fmt(-remaining) if remaining < 0 else "0.00")}

    def transfer_input_out(self, period: Any, *, reason: Any, amount: Any, ref: Any,
                           cost_account: Any = None, booked_on: Any = None,
                           submitted_by: str = "vat-engine") -> dict[str, Any]:
        """把已抵扣进项按用途转出：借 <损益科目> / 贷 2221.01.04。

        `reason` 是封闭集；对方科目默认按原因取，可覆盖但必须是损益类。
        转出额不超过本期可转出额——超出说明"转的不是本期已抵的进项"，期间或金额错了。
        """
        key = _check_period(period)
        tag = str(ref or "").strip()
        if not tag:
            raise TemplateError("INVALID_ARGUMENT",
                                "单据号 ref 不能为空（幂等键就是它）")
        reason_key = str(reason or "").strip()
        if reason_key not in TRANSFER_OUT_REASONS:
            allowed = "、".join(k + "（" + v["zh"] + "）"
                                for k, v in TRANSFER_OUT_REASONS.items())
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"转出原因 {reason!r} 不在封闭集内：{allowed}",
                detail={"allowed": list(TRANSFER_OUT_REASONS)})
        value = _dec(amount, "转出税额")
        if value <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"转出税额须大于零：{amount!r}")
        from .statements import CHART
        account = str(cost_account or TRANSFER_OUT_REASONS[reason_key]["account"]).strip()
        kind = CHART.get(account, (None, None))[1]
        if kind not in ("revenue", "expense"):
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"对方科目 {account} 不是损益类：进项转出的实质是这笔进项变成费用，"
                "记到资产/负债上会让报表口径失真",
                detail={"allowed":
                        [c for c, (_l, k) in CHART.items() if k in ("revenue", "expense")]})
        room = Decimal(self.transferable_input(key)["remaining"])
        if value > room:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"转出 {_fmt(value)} 超过本期可转出额 {_fmt(room)}："
                "只能转本期已抵扣的进项，超出说明期间或金额错了",
                detail={"period": key, "remaining": _fmt(room),
                        "requested": _fmt(value)})
        day = _check_day(booked_on or key, "业务日期")
        stable_id = f"{TRANSFER_OUT_PREFIX}{tag}"
        proposal = self._kernel.submit_proposal(
            stable_id,
            [{"account_code": account, "direction": "debit",
              "amount": _fmt(value), "currency": self._base},
             {"account_code": INPUT_TRANSFER_OUT, "direction": "credit",
              "amount": _fmt(value), "currency": self._base}],
            source_ref=stable_id, kind="journal",
            lane_title=f"{tag} 进项税额转出（{TRANSFER_OUT_REASONS[reason_key]['zh']}）",
            booked_on=day, submitted_by=submitted_by)
        return {"period": key, "ref": tag, "reason": reason_key,
                "reasonZh": TRANSFER_OUT_REASONS[reason_key]["zh"],
                "costAccount": account, "amount": _fmt(value),
                "stableId": stable_id, "proposal": proposal, "bookedOn": day,
                "remainingAfter": _fmt(room - value),
                "duplicate": bool(proposal and proposal.get("duplicate")),
                "caliberNote": "已抵扣进项改为不可抵扣用途时转出（增加应纳增值税）；"
                               "应纳 = 销项 − 进项 + 转出"}

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
           "INPUT_TRANSFER_OUT", "TRANSFER_OUT", "VAT_UNPAID", "TAX_ROOT",
           "TRANSFER_OUT_REASONS"]

"""附加税与所得税预缴（change: add-cn-surcharge-and-income-tax）。

一张月度税表不止增值税。这里补两块，两处的**基数**都容易错，而且错了不会报错：

1. **附加税**（城建税 + 教育费附加 + 地方教育附加）的基数是**应纳增值税**，不是销售额、
   也不是销项税额。拿销售额算，留抵月份也会产生附加税——多缴。
2. **所得税预缴**的基数是**本年累计利润**，不是当期利润。按月各算各的，第二个月就把第一个月
   的税再算一遍。

## 三条纪律

- **税率是外部事实**：城建税 7/5/1 按地区、减免系数按政策，人工登记且必须带出处；
  缺税率就拒，不拿默认值顶上（拿 7% 顶，县城镇的客户就多缴）。
- **只出待确认 proposal**：缴款是资金动作（借 2221.x / 贷 货币资金），不由车道代做。
- **口径说清两条**：附加税按「应纳」计提而非「实缴时点」；所得税按会计利润总额预缴，
  **不做纳税调整**（调整在汇算清缴）。不写就等于宣称这是应纳税所得额。
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .cn_statements import CnStatements
from .cn_tax import CnVatLane
from .dates import entry_period
from .errors import TemplateError
from .eventlog import EventLog
from .kernel_rows import read_entries
from .statements import CHART, is_closing_entry

Q2 = Decimal("0.01")
SCHEMA = "fd-erp-vertical-templates/cn-filing/v1"
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

SURCHARGE_ACCOUNT = "5403"          # 税金及附加
INCOME_TAX_ACCOUNT = "5801"         # 所得税费用
CITY_TAX_ACCOUNT = "2221.12"
EDU_SURCHARGE_ACCOUNT = "2221.13"
LOCAL_EDU_ACCOUNT = "2221.14"
INCOME_TAX_PAYABLE = "2221.11"

#: 税率键封闭集。城建税只允许 7/5/1（市区/县城镇/其他）。
RATE_KEYS = ("city_construction", "education_surcharge", "local_education_surcharge",
             "income_tax", "surcharge_relief")
CITY_RATES = ("7", "5", "1")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except Exception:                                  # noqa: BLE001
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}") from None


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-11"})
    return key


class CnFilingLane:
    """附加税计提与所得税预缴（只出待确认 proposal）。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any, *,
                 vat: CnVatLane | None = None,
                 statements: CnStatements | None = None,
                 invoices: Any = None,
                 base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"cn-filing-{tenant_id}.jsonl")
        self._kernel = kernel
        self._vat = vat or CnVatLane(tenant_id, kernel, base_currency=base_currency)
        self._statements = statements or CnStatements(tenant_id, kernel,
                                                      base_currency=base_currency)
        #: 发票台账：申报口径要给出进项三档（可抵/待认证/不可抵），账上只有可抵那一档。
        self._invoices = invoices
        self._base = base_currency

    # ---- 税率（人工登记的外部事实） ----

    def register_rates(self, rates: dict[str, Any], *, effective_from: Any,
                       source: Any, registered_by: str = "cfo") -> dict[str, Any]:
        """登记税率。`effective_from` 为生效期间；同期间重复登记以后一次为准（追加式）。"""
        key = _check_period(effective_from)
        note = str(source or "").strip()
        if not note:
            raise TemplateError("INVALID_ARGUMENT",
                                "税率必须带出处（source）：地区与政策不同，无出处无法复核",
                                detail={"effectiveFrom": key})
        if not isinstance(rates, dict) or not rates:
            raise TemplateError("INVALID_ARGUMENT",
                                f"税率不能为空：{{'city_construction': '7', …}}")
        unknown = sorted(k for k in rates if k not in RATE_KEYS)
        if unknown:
            raise TemplateError("INVALID_ARGUMENT",
                                f"未知税率键：{'、'.join(unknown)}",
                                detail={"allowed": list(RATE_KEYS)})
        cleaned: dict[str, str] = {}
        for k, raw in rates.items():
            value = _dec(raw, f"税率 {k}")
            if k == "city_construction":
                if str(raw).strip() not in CITY_RATES:
                    raise TemplateError(
                        "INVALID_ARGUMENT",
                        f"城建税率须为 7/5/1（市区/县城镇/其他）：{raw!r}",
                        detail={"allowed": list(CITY_RATES)})
            elif k == "surcharge_relief":
                if not (Decimal(0) <= value <= Decimal(1)):
                    raise TemplateError("INVALID_ARGUMENT",
                                        f"减免系数须在 [0,1]：{raw!r}")
            elif not (Decimal(0) <= value <= Decimal(100)):
                raise TemplateError("INVALID_ARGUMENT", f"税率须在 0-100：{k}={raw!r}")
            cleaned[k] = str(value)
        self._log.append({"kind": "rates", "effectiveFrom": key, "rates": cleaned,
                          "source": note, "registeredBy": str(registered_by)})
        return {"effectiveFrom": key, "registered": cleaned, "source": note,
                "caliberNote": "追加式：同生效期间以后一次为准；取 rates(period) 得该期适用值"}

    def rates(self, period: Any) -> dict[str, Any]:
        """该期间适用的税率（取 `effectiveFrom <= period` 的最后一次登记）。"""
        key = _check_period(period)
        applicable: dict[str, str] = {}
        source = None
        effective = None
        for rec in self._log.events():
            if rec.get("kind") != "rates":
                continue
            eff = str(rec.get("effectiveFrom") or "")
            if eff > key:
                continue
            if effective is None or eff >= effective:
                effective = eff
                applicable.update(rec.get("rates") or {})
                source = rec.get("source")
        missing = [k for k in ("city_construction", "education_surcharge",
                               "local_education_surcharge", "income_tax")
                   if k not in applicable]
        applicable.setdefault("surcharge_relief", "1")
        return {"period": key, "effectiveFrom": effective,
                "rates": applicable, "source": source, "missing": missing}

    def _require(self, period: str) -> dict[str, str]:
        got = self.rates(period)
        if got["missing"]:
            raise TemplateError(
                "NOT_WIRED",
                f"{period} 缺税率：{'、'.join(got['missing'])}——先 register_rates 登记"
                "（地区/政策不同，不拿默认值顶）",
                detail={"period": period, "missing": got["missing"]})
        return got["rates"]

    # ---- 增值税申报数（外部事实，带出处登记） ----

    #: 申报口径的六项；顺序与报表对照表一致，差额逐项给出。
    VAT_ITEMS = ("sales", "outputVat", "inputVat", "inputTransferOut",
                 "openingCredit")
    VAT_LABELS = {"sales": "销售额", "outputVat": "销项税额", "inputVat": "进项税额",
                  "inputTransferOut": "进项税额转出", "openingCredit": "上期留抵",
                  "payable": "应纳税额"}

    def register_vat_return(self, period: Any, *, source: Any,
                            registered_by: str = "cfo", **items: Any) -> dict[str, Any]:
        """登记某期间的**申报数**（电子税局实际报出去的数）。

        为什么必须带出处：申报是外部事实，与税率同理——没有出处，几个月后没人能复核
        「这个数是报出去的、还是有人手填进来试算的」。所以 `source` 必填，且整条记录
        进本车道事件档（追加式，同期间以后一次为准）。
        """
        key = _check_period(period)
        note = str(source or "").strip()
        if not note:
            raise TemplateError(
                "INVALID_ARGUMENT",
                "申报数必须带出处（source）：无出处无法复核这是报出去的还是试算的",
                detail={"period": key, "required": "source"})
        unknown = sorted(k for k in items if k not in self.VAT_ITEMS)
        if unknown:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"未知申报项：{'、'.join(unknown)}",
                detail={"allowed": list(self.VAT_ITEMS)})
        cleaned: dict[str, str] = {}
        for name in self.VAT_ITEMS:
            value = _dec(items.get(name, 0), f"申报项 {name}")
            if value < 0:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"申报项 {name} 不能为负：{items.get(name)!r}"
                    "（红字/退回冲减在账面侧处理，申报表没有负数栏）",
                    detail={"period": key, "item": name})
            cleaned[name] = str(value)
        self._log.append({"kind": "vat-return", "period": key, "declared": cleaned,
                          "source": note, "registeredBy": str(registered_by)})
        return {"period": key, "declared": cleaned, "source": note,
                "caliberNote": "追加式：同期间以后一次登记为准；用 vat_declared 回读"}

    def vat_declared(self, period: Any) -> dict[str, str] | None:
        """该期间最后一次登记的申报数；**从未登记返回 None**。

        `None` 与「登记了全 0」必须是两种事实：前者是「没提供申报数，不能当作已核对」，
        后者是「报了零」。混成一个，报表就会把「没人报过」显示成「报了零」。
        """
        key = _check_period(period)
        latest = None
        effective = ""
        for rec in self._log.events():
            if rec.get("kind") != "vat-return":
                continue
            when = str(rec.get("period") or "")
            if when != key and not when.startswith(f"{key}#"):
                continue
            # 同期间多次登记（重报/更正）以后写入者为准；事件档是追加式，顺序即时间序。
            latest = rec
            effective = when
        if latest is None:
            return None
        return {k: str(v) for k, v in (latest.get("declared") or {}).items()}

    def vat_reconciliation(self, period: Any) -> dict[str, Any]:
        """把「账面 vs 申报」两侧拼齐，并带上进项三档。

        为什么要这一个方法而不是让调用方自己拼：`declared` 与 `invoices` 都是
        「忘了传就静默少半张表」的形态——报表照样返回、`provided=false`、三档干脆没有，
        而读者只会以为「这期就是没差异」。供给方在这里定死，调用方不需要记得。
        """
        key = _check_period(period)
        declared = self.vat_declared(key)
        out = self._statements.vat_return(key, declared=declared,
                                          invoices=self._invoices)
        out["declaredSource"] = None if declared is None else next(
            (str(rec.get("source")) for rec in reversed(self._log.events())
             if rec.get("kind") == "vat-return" and rec.get("period") == key), None)
        return out

    # ---- 附加税 ----

    def surcharges(self, period: Any, *, submitted_by: str = "cn-filing-engine"
                   ) -> dict[str, Any]:
        """按本期应纳增值税计提三项附加税；留抵为 0 → noop。"""
        key = _check_period(period)
        rates = self._require(key)
        pos = self._vat.position(key)
        payable = Decimal(pos["payable"])
        if payable <= 0:
            return {"period": key, "noop": True, "base": "0.00",
                    "total": "0.00", "items": [],
                    "caliberNote": "本期无应纳增值税（留抵），附加税为 0，不生成提案"}
        relief = Decimal(rates["surcharge_relief"])
        items = [
            ("city_construction", CITY_TAX_ACCOUNT, "城市维护建设税"),
            ("education_surcharge", EDU_SURCHARGE_ACCOUNT, "教育费附加"),
            ("local_education_surcharge", LOCAL_EDU_ACCOUNT, "地方教育附加"),
        ]
        computed: list[dict[str, Any]] = []
        total = Decimal(0)
        for rate_key, account, label in items:
            amount = (payable * Decimal(rates[rate_key]) / Decimal(100) * relief
                      ).quantize(Q2, rounding=ROUND_HALF_UP)
            computed.append({"key": rate_key, "label": label, "account": account,
                             "rate": rates[rate_key], "amount": _fmt(amount)})
            total += amount
        lines = [{"account_code": SURCHARGE_ACCOUNT, "direction": "debit",
                  "amount": _fmt(total), "currency": self._base}]
        for item in computed:
            if Decimal(item["amount"]) > 0:
                lines.append({"account_code": item["account"], "direction": "credit",
                              "amount": item["amount"], "currency": self._base})
        stable_id = f"surcharge:{key}"
        proposal = self._kernel.submit_proposal(
            stable_id, lines, source_ref=stable_id, kind="journal",
            lane_title=f"{key} 附加税计提", booked_on=_period_end(key),
            submitted_by=submitted_by)
        return {"period": key, "noop": False, "base": _fmt(payable),
                "relief": str(relief), "items": computed, "total": _fmt(total),
                "stableId": stable_id, "proposal": proposal,
                "bookedOn": _period_end(key),
                "duplicate": bool(proposal and proposal.get("duplicate")),
                "caliberNote": "基数为本期**应纳增值税**（非销售额、非销项税额）；"
                               "按应纳而非实缴时点计提，跨期差异体现在缴款期"}

    # ---- 所得税预缴 ----

    def _cumulative_profit(self, period: str) -> Decimal:
        """本年 1 月至该期间的累计**利润总额**——**取利润表，不自己遍历分录**。

        这里原本自己扫一遍账，于是同一口径在本仓有两份实现：`CnStatements` 当时没排除
        结转分录，这份排除了。两个数在结账月就会分叉（一个 0、一个真实值），
        而两侧都不报错——`drivers.py` 里那句「不同源就会算出两份口径」说的正是它。
        现在只有一份：利润表累计列的 `totalProfit`。

        两个口径坑由报表那边保证，不再各写一遍：

        - **损益方向**：收入在贷方、费用在借方，带符号金额是「借正贷负」，两类都是
          `profit -= signed`；按类别分别加减会把费用反过来加成利润（亏损月因此算出利润、
          还去预缴）。
        - **税前不是净利**：利润总额在所得税费用（5801）之前，扣掉它再乘税率等于重复计税；
          结转分录（`close:`）也不能计，它把损益搬进 3100，计入会把已结账月份的累计利润
          清零，于是「已预缴的不再补差」变成「重新算一遍」。
        """
        key = _check_period(period)
        report = self._statements.income_statement(key)
        return Decimal(str(report["cumulative"]["totals"]["totalProfit"]))

    def _booked_income_tax(self, period: str, *, before: bool = False) -> Decimal:
        """截至该期间已预缴的所得税（2221.11 贷方累计）；`before=True` 只算本期之前。"""
        total = Decimal(0)
        for row in read_entries(self._kernel):
            when = entry_period(row)
            if when in ("", "unknown"):
                continue
            if not (when < period if before else when <= period):
                continue
            if str(row.get("account_code") or "") != INCOME_TAX_PAYABLE:
                continue
            amount = _dec(row.get("amount_base") or row.get("amount"), "金额")
            total += -amount if row.get("direction") == "debit" else amount
        return total

    def income_tax_prepay(self, period: Any, *,
                          submitted_by: str = "cn-filing-engine") -> dict[str, Any]:
        """按本年累计利润 × 税率 − 已预缴，出本期应预缴。"""
        key = _check_period(period)
        rates = self._require(key)
        profit = self._cumulative_profit(key)
        rate = Decimal(rates["income_tax"]) / Decimal(100)
        target = (max(profit, Decimal(0)) * rate).quantize(Q2, rounding=ROUND_HALF_UP)
        booked = self._booked_income_tax(key)
        due = target - booked
        note = ("基数为**会计利润总额**，未做纳税调整（业务招待费/广告费/研发加计等调整"
                "在汇算清缴，不在本版）；累计口径从当年 1 月起算")
        if due <= 0:
            return {"period": key, "noop": True, "cumulativeProfit": _fmt(profit),
                    "target": _fmt(target), "alreadyPrepaid": _fmt(booked),
                    "due": "0.00", "overPrepaid": _fmt(max(-due, Decimal(0))),
                    "rate": rates["income_tax"], "caliberNote": note}
        amount = _fmt(due)
        stable_id = f"income-tax:{key}"
        proposal = self._kernel.submit_proposal(
            stable_id,
            [{"account_code": INCOME_TAX_ACCOUNT, "direction": "debit",
              "amount": amount, "currency": self._base},
             {"account_code": INCOME_TAX_PAYABLE, "direction": "credit",
              "amount": amount, "currency": self._base}],
            source_ref=stable_id, kind="journal",
            lane_title=f"{key} 所得税预缴", booked_on=_period_end(key),
            submitted_by=submitted_by)
        return {"period": key, "noop": False, "cumulativeProfit": _fmt(profit),
                "target": _fmt(target), "alreadyPrepaid": _fmt(booked),
                "due": amount, "rate": rates["income_tax"], "stableId": stable_id,
                "proposal": proposal, "bookedOn": _period_end(key),
                "duplicate": bool(proposal and proposal.get("duplicate")),
                "caliberNote": note}

    # ---- 月度税表 ----

    def filing_summary(self, period: Any) -> dict[str, Any]:
        """增值税 + 附加税 + 所得税一张表，并与账面逐项对比。"""
        key = _check_period(period)
        vat = self._vat.position(key)
        rates = self.rates(key)
        payable = Decimal(vat["payable"])
        relief = Decimal(rates["rates"].get("surcharge_relief", "1"))
        items: list[dict[str, Any]] = []
        total_surcharge = Decimal(0)
        if payable > 0 and not rates["missing"]:
            for rate_key, _account, label in (
                    ("city_construction", CITY_TAX_ACCOUNT, "城市维护建设税"),
                    ("education_surcharge", EDU_SURCHARGE_ACCOUNT, "教育费附加"),
                    ("local_education_surcharge", LOCAL_EDU_ACCOUNT, "地方教育附加")):
                amount = (payable * Decimal(rates["rates"][rate_key]) / Decimal(100)
                          * relief).quantize(Q2, rounding=ROUND_HALF_UP)
                items.append({"label": label, "rate": rates["rates"][rate_key],
                              "amount": _fmt(amount)})
                total_surcharge += amount
        profit = self._cumulative_profit(key)
        income_rate = (Decimal(rates["rates"]["income_tax"]) / Decimal(100)
                       if not rates["missing"] else Decimal(0))
        target_income = (max(profit, Decimal(0)) * income_rate
                         ).quantize(Q2, rounding=ROUND_HALF_UP)
        booked_income = self._booked_income_tax(key)
        due_income = max(target_income - booked_income, Decimal(0))
        # 勾稽对象是**本期应计提**（目标 − 本期之前已预缴），不是「剩余应缴」（目标 − 含本期已预缴）。
        # 拿剩余应缴去比 5801 本期发生额，账一做好差额就变成「本期已计提」，永远勾不上；
        # 那种勾稽会稳定地报不一致，于是没人再看它。
        booked_before = self._booked_income_tax(key, before=True)
        accrued_income = max(target_income - booked_before, Decimal(0))

        # 与账面比：附加税看 5403 本期发生额，所得税看 5801 本期发生额
        ledger_surcharge = self._period_amount(SURCHARGE_ACCOUNT, key)
        ledger_income = self._period_amount(INCOME_TAX_ACCOUNT, key)
        reconciliation = [
            {"item": "surcharge", "label": "附加税",
             "computed": _fmt(total_surcharge), "ledger": _fmt(ledger_surcharge),
             "difference": _fmt(ledger_surcharge - total_surcharge)},
            {"item": "incomeTax", "label": "所得税本期应计提",
             "computed": _fmt(accrued_income), "ledger": _fmt(ledger_income),
             "difference": _fmt(ledger_income - accrued_income)},
        ]
        consistent = all(Decimal(r["difference"]) == 0 for r in reconciliation)
        return {
            "schema": SCHEMA, "period": key,
            "vat": vat,
            "surcharges": {"base": _fmt(max(payable, Decimal(0))),
                           "items": items, "total": _fmt(total_surcharge),
                           "relief": str(relief)},
            "incomeTax": {"cumulativeProfit": _fmt(profit),
                          "target": _fmt(target_income),
                          "alreadyPrepaid": _fmt(booked_income),
                          "accrued": _fmt(accrued_income),
                          "due": _fmt(due_income)},
            "rates": rates["rates"], "ratesEffectiveFrom": rates["effectiveFrom"],
            "reconciliation": reconciliation, "consistent": consistent,
            "caliberNote": "附加税基数为应纳增值税；所得税基数为本年累计会计利润、未做纳税调整；"
                           "`accrued` 是本期应计提（与 5801 本期发生额勾稽），"
                           "`due` 是扣除本期已计提后的剩余应缴；"
                           "申报**数据**到此为止，申报**提交**仍需客户渠道",
        }

    def _period_amount(self, account: str, period: str) -> Decimal:
        total = Decimal(0)
        for row in read_entries(self._kernel):
            if entry_period(row) != period:
                continue
            if str(row.get("account_code") or "") != account:
                continue
            amount = _dec(row.get("amount_base") or row.get("amount"), "金额")
            total += amount if row.get("direction") == "debit" else -amount
        return total


def _period_end(key: str) -> str:
    from .dates import period_end
    return period_end(key)


__all__ = ["CnFilingLane", "RATE_KEYS", "CITY_RATES", "SURCHARGE_ACCOUNT",
           "INCOME_TAX_ACCOUNT", "CITY_TAX_ACCOUNT", "EDU_SURCHARGE_ACCOUNT",
           "LOCAL_EDU_ACCOUNT", "INCOME_TAX_PAYABLE"]

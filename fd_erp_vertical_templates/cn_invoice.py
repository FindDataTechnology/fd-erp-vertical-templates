"""中国发票台账与进项抵扣资格（change: add-cn-invoice-register）。

增值税两条腿不对称：**销项是「开了票就要交」，进项是「拿到合规票、勾选认证了才能抵」**。
上一版增值税车道不看票种也不看认证就把税额记进 `2221.01.02`——客户拿一张普票走一遍采购，
进项就被虚增、应纳税额被低估。报表照样平、申报数也有，只是税少缴了。

所以这里把「资格」这件事做成数据：

- **票种封闭集**：专票（含全电专票）与海关进口缴款书可抵，普票（含全电普票）不可抵；
- **认证是人工事实**：可抵票种还要勾选认证，`certify()` 记认证日期与经办人，追加式可回溯；
- **不可抵扣的税进成本**：普票的税是你付的价款的一部分，不是资产；挂「待抵扣进项税额」会让
  资产虚增且永远抵不掉；
- **待认证是中间态**：票到了还没勾选，真能抵、只是时间问题，单列一档，既不进账也不进成本。

台账只记「有什么票、认证了没有」，过账仍旧只出待确认 proposal——台账改不了账。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .cn_tax import INPUT_VAT, OUTPUT_VAT, RATES, CnVatLane, split_tax
from .dates import entry_period
from .errors import TemplateError
from .eventlog import EventLog
from .kernel_rows import read_entries

Q2 = Decimal("0.01")
SCHEMA = "fd-erp-vertical-templates/cn-invoice/v1"
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: 票种封闭集 → 是否属于「可抵票种」。能否抵还要看认证（见 `deduction`）。
INVOICE_KINDS: dict[str, dict[str, Any]] = {
    "vat-special": {"zh": "增值税专用发票", "deductible": True},
    "e-special": {"zh": "全电发票（专用）", "deductible": True},
    "customs-payment": {"zh": "海关进口增值税专用缴款书", "deductible": True},
    "vat-general": {"zh": "增值税普通发票", "deductible": False},
    "e-general": {"zh": "全电发票（普通）", "deductible": False},
}

DIRECTIONS = {"sale": "销项", "purchase": "进项"}

#: 未认证的可抵票进项归这一档（真能抵，只是还没勾选）。
TIER_DEDUCTIBLE = "deductible"
TIER_PENDING = "pendingCertification"
TIER_NON_DEDUCTIBLE = "nonDeductible"


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any, what: str) -> Decimal:
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


def _check_day(value: Any, what: str) -> str:
    raw = str(value or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        raise TemplateError("INVALID_ARGUMENT", f"{what}须为 YYYY-MM-DD：{value!r}")
    return raw


class CnInvoiceBook:
    """销项/进项发票台账（事实档，不改账）。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any, *,
                 lane: CnVatLane | None = None, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"cn-invoice-{tenant_id}.jsonl")
        self._kernel = kernel
        self._lane = lane or CnVatLane(tenant_id, kernel, base_currency=base_currency)
        self._base = base_currency

    # ---- 读面 ----

    def _events(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in self._log.events() if isinstance(r, dict)]
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def invoices(self, *, direction: str | None = None) -> list[dict[str, Any]]:
        """当前有效发票（同一「方向+票种+号码」取首次登记，认证信息合并上去）。"""
        latest: dict[str, dict[str, Any]] = {}
        for rec in self._events("invoice"):
            latest[str(rec["key"])] = dict(rec)
        for rec in self._events("certification"):
            key = str(rec["key"])
            if key in latest:
                latest[key]["certified"] = True
                latest[key]["certifiedOn"] = rec.get("certifiedOn")
                latest[key]["certifiedBy"] = rec.get("certifiedBy")
        # 「这张票已经被哪张凭证用过」必须并回发票：不并回去，同一张进项票就能抵两次
        # ——少缴的税不会报错，只会体现在申报与稽查的差额里。
        for rec in self._events("usage"):
            key = str(rec["key"])
            if key in latest:
                latest[key]["usedBy"] = rec.get("usedBy")
                latest[key]["usedByRef"] = rec.get("ref")
        out = [latest[k] for k in sorted(latest)]
        if direction:
            out = [r for r in out if r.get("direction") == direction]
        return out

    def invoice(self, key_or_number: str) -> dict[str, Any] | None:
        want = str(key_or_number or "").strip()
        for row in self.invoices():
            if row["key"] == want or row.get("number") == want:
                return row
        return None

    # ---- 登记 ----

    def register(self, direction: Any, kind: Any, number: Any, net: Any, *,
                 rate: Any = "13", tax: Any = None, counterparty: Any = None,
                 issued_on: Any = None, submitted_by: str = "owner") -> dict[str, Any]:
        """登记一张发票。税额按金额×税率算，与传入值不符即拒（票面与台账不符最难查）。"""
        side = str(direction or "").strip()
        if side not in DIRECTIONS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"发票方向须为 sale/purchase：{direction!r}",
                                detail={"allowed": list(DIRECTIONS)})
        kind_key = str(kind or "").strip()
        if kind_key not in INVOICE_KINDS:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"票种 {kind!r} 不在封闭集内：{'、'.join(INVOICE_KINDS)}",
                detail={"allowed": list(INVOICE_KINDS)})
        num = str(number or "").strip()
        if not num:
            raise TemplateError("INVALID_ARGUMENT", "发票号码不能为空")
        parts = split_tax(net, rate, gross=False)
        if tax not in (None, ""):
            given = _dec(tax, "税额")
            if given != Decimal(parts["tax"]):
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"票面税额 {_fmt(given)} 与按 {rate}% 算出的 {parts['tax']} 不一致："
                    "台账与票面不符是最难查的错，先核对金额与税率",
                    detail={"expected": parts["tax"], "received": _fmt(given)})
        day = _check_day(issued_on or date.today().isoformat(), "开票日期")
        key = f"{side}:{kind_key}:{num}"
        existing = self.invoice(key)
        if existing is not None:
            return {**existing, "duplicate": True,
                    "caliber_note": "同一方向+票种+号码已登记，返回首次记录"}
        rec = {"kind": "invoice", "key": key, "direction": side, "invoiceKind": kind_key,
               "kindZh": INVOICE_KINDS[kind_key]["zh"], "number": num,
               "net": parts["net"], "rate": str(rate), "tax": parts["tax"],
               "gross": parts["gross"],
               "counterparty": (str(counterparty).strip() or None),
               "issuedOn": day, "period": day[:7],
               "deductibleKind": bool(INVOICE_KINDS[kind_key]["deductible"]),
               # 红字标记与认证经办人一律显式落位（蓝字票是 `False`/`None`，**不是缺键**）：
               # 缺键让消费侧 `row["red"]` 直接 KeyError，而「列表里有的行有、有的行没有」
               # 是那种只在遇到红字票那天才炸的形状。台账事实照旧——只有红字票带
               # `redOf`/`redReason` 的**值**，蓝字票带的是 `None`。
               "red": False, "redOf": None, "redReason": None,
               "certified": False, "certifiedBy": None, "certifiedOn": None,
               "submittedBy": str(submitted_by)}
        self._log.append(rec)
        return {**rec, "duplicate": False,
                "caliber_note": "台账事实；是否可抵由票种 + 认证共同决定"}

    def certify(self, number: Any, *, certified_on: Any = None,
                by: Any = "owner") -> dict[str, Any]:
        """记录勾选认证（人工事实，追加式）。不可抵票种也允许记录，但不会因此变可抵。"""
        row = self.invoice(number)
        if row is None:
            raise TemplateError("NOT_FOUND", f"发票不存在：{number!r}",
                                detail={"number": str(number)})
        if row.get("certified"):
            return {**row, "duplicate": True, "caliber_note": "该票已认证过"}
        day = _check_day(certified_on or date.today().isoformat(), "认证日期")
        who = str(by or "").strip()
        if not who or who in ("system", "agent"):
            raise TemplateError("INVALID_ARGUMENT",
                                f"认证须人工经办：{who or '（空）'} 不得自证抵扣",
                                detail={"by": who})
        self._log.append({"kind": "certification", "key": row["key"],
                          "number": row["number"], "certifiedOn": day,
                          "certifiedBy": who})
        return {**self.invoice(row["key"]), "duplicate": False,
                "caliber_note": "认证是人工事实；不可抵票种认证后仍不可抵"}

    # ---- 红字发票（销售退回与折让） ----

    def _red_total(self, original: dict[str, Any]) -> Decimal:
        """该蓝字票已被冲减的净额（正数）。"""
        total = Decimal(0)
        for row in self.invoices():
            if row.get("red") and row.get("redOf") == original["number"]:
                total += abs(Decimal(row["net"]))
        return total

    def register_red(self, direction: Any, kind: Any, number: Any, net: Any, *,
                     red_of: Any, reason: Any = None, issued_on: Any = None,
                     submitted_by: str = "owner") -> dict[str, Any]:
        """登记红字发票冲减蓝字原票。负数记账，原票不动。

        红字票是**独立票据**（自己的号码与开票日期），不是"删原票"：删了就同时丢掉开票事实
        和「冲减发生在哪一期」。所以这里按负数金额存，申报矩阵与销项合计自然净额。

        进项红字（供应商退回）本版不做：它要同时冲应付、成本与存货，超出本层能算对的范围，
        显式挂 `NOT_WIRED`，不静默接受。
        """
        side = str(direction or "").strip()
        if side == "purchase":
            raise TemplateError(
                "NOT_WIRED",
                "进项红字（供应商退回）本版不做：它要同时冲减应付、成本与存货，"
                "台账层算不对。进项侧的不可抵扣用途请走 "
                "CnVatLane.transfer_input_out；红字冲回车道待接入",
                detail={"direction": side, "alternative": "CnVatLane.transfer_input_out"})
        if side not in DIRECTIONS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"发票方向须为 sale/purchase：{direction!r}",
                                detail={"allowed": list(DIRECTIONS)})
        kind_key = str(kind or "").strip()
        if kind_key not in INVOICE_KINDS:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"票种 {kind!r} 不在封闭集内：{'、'.join(INVOICE_KINDS)}",
                detail={"allowed": list(INVOICE_KINDS)})
        num = str(number or "").strip()
        if not num:
            raise TemplateError("INVALID_ARGUMENT", "红字发票号码不能为空")
        orig_num = str(red_of or "").strip()
        original = self.invoice(orig_num) if orig_num else None
        if original is None:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"红字票必须引用已登记的蓝字原票：{red_of!r} 在台账里不存在",
                detail={"redOf": orig_num})
        if original.get("red"):
            raise TemplateError("INVALID_ARGUMENT",
                                f"{original['number']} 本身是红字票，不能再被冲减",
                                detail={"redOf": orig_num})
        if original.get("direction") != side or original["invoiceKind"] != kind_key:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"红字票须与原票同方向同票种：原票是 {original['direction']}/"
                f"{original['invoiceKind']}，本票是 {side}/{kind_key}",
                detail={"direction": original["direction"],
                        "invoiceKind": original["invoiceKind"]})
        magnitude = _dec(net, "红字金额")
        if magnitude <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"红字净额须填正数：{net!r}")
        already = self._red_total(original)
        room = Decimal(original["net"]) - already
        if magnitude > room:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"红字冲减 {_fmt(magnitude)} 超过原票剩余可冲额 {_fmt(room)}："
                f"原票净额 {original['net']}，已冲 {_fmt(already)}",
                detail={"original": original["number"],
                        "originalNet": original["net"],
                        "alreadyRed": _fmt(already), "remaining": _fmt(room)})
        parts = split_tax(magnitude, original["rate"], gross=False)
        day = _check_day(issued_on or date.today().isoformat(), "开票日期")
        key = f"{side}:{kind_key}:{num}"
        existing = self.invoice(key)
        if existing is not None:
            return {**existing, "duplicate": True,
                    "caliber_note": "同一红字票号已登记，返回首次记录"}
        rec = {"kind": "invoice", "key": key, "direction": side,
               "invoiceKind": kind_key, "kindZh": INVOICE_KINDS[kind_key]["zh"],
               "number": num, "red": True, "redOf": original["number"],
               "net": _fmt(-Decimal(parts["net"])),
               "rate": original["rate"], "tax": _fmt(-Decimal(parts["tax"])),
               "gross": _fmt(-Decimal(parts["gross"])),
               "counterparty": original.get("counterparty"),
               "issuedOn": day, "period": day[:7],
               "redReason": (str(reason).strip() or None),
               "deductibleKind": bool(INVOICE_KINDS[kind_key]["deductible"]),
               "certified": False, "certifiedBy": None, "certifiedOn": None,
               "submittedBy": str(submitted_by)}
        self._log.append(rec)
        return {**rec, "duplicate": False, "remainingAfter": _fmt(room - magnitude),
                "caliber_note": "红字票是独立票据、负数记账；原票保留，"
                                "冲减落在本票所属期间"}

    def post_red(self, ref: Any, invoice_number: Any, amount: Any, *,
                 revenue_account: str = "4000", settle_account: str = "1100",
                 booked_on: Any = None,
                 submitted_by: str = "vat-engine") -> dict[str, Any]:
        """红字销售票过账：借收入 / 借销项 / 贷应收（与 post_sale 逐行反向）。"""
        row = self.invoice(invoice_number)
        if row is None:
            raise TemplateError("NOT_FOUND",
                                f"红字发票未登记：{invoice_number!r}",
                                detail={"number": str(invoice_number)})
        if not row.get("red"):
            raise TemplateError("INVALID_ARGUMENT",
                                f"{row['number']} 不是红字票：冲减只能用红字票过账",
                                detail={"number": row["number"]})
        if row.get("usedBy"):
            raise TemplateError("INVALID_ARGUMENT",
                                f"红字票 {row['number']} 已被凭证 {row['usedBy']} 使用",
                                detail={"number": row["number"], "usedBy": row["usedBy"]})
        parts = split_tax(amount, row["rate"], gross=True)
        if Decimal(parts["gross"]) != abs(Decimal(row["gross"])):
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"过账金额 {parts['gross']} 与红字票面 {abs(Decimal(row['gross']))} 不一致："
                "冲减不能按另一个金额做",
                detail={"invoiceGross": abs(Decimal(row["gross"])),
                        "postingGross": parts["gross"]})
        day = _check_day(booked_on or date.today().isoformat(), "业务日期")
        stable_id = f"vat-red:{row['number']}"
        lines = [{"account_code": revenue_account, "direction": "debit",
                  "amount": parts["net"], "currency": self._base},
                 {"account_code": settle_account, "direction": "credit",
                  "amount": parts["gross"], "currency": self._base}]
        if Decimal(parts["tax"]) > 0:
            lines.insert(1, {"account_code": OUTPUT_VAT, "direction": "debit",
                             "amount": parts["tax"], "currency": self._base})
        proposal = self._kernel.submit_proposal(
            stable_id, lines, source_ref=stable_id, kind="journal",
            lane_title=f"{row['number']} 销售红字冲减", booked_on=day,
            submitted_by=submitted_by)
        if not proposal.get("duplicate"):
            self._log.append({"kind": "usage", "key": row["key"],
                              "number": row["number"], "usedBy": stable_id,
                              "ref": str(ref), "deductible": False})
        return {"ref": str(ref), "invoice": row["number"], "redOf": row.get("redOf"),
                "net": parts["net"], "tax": parts["tax"], "gross": parts["gross"],
                "proposal": proposal, "stableId": stable_id, "bookedOn": day,
                "duplicate": bool(proposal.get("duplicate")),
                "caliber_note": "红字冲减出反向分录：借收入、借销项、贷应收；"
                                "只出待确认提案"}

    # ---- 抵扣资格 ----

    @staticmethod
    def tier_of(row: dict[str, Any]) -> str:
        if not row.get("deductibleKind"):
            return TIER_NON_DEDUCTIBLE
        return TIER_DEDUCTIBLE if row.get("certified") else TIER_PENDING

    def deduction(self, period: Any) -> dict[str, Any]:
        """本期进项三档：可抵扣 / 待认证 / 不可抵扣（税额合计 + 逐票）。"""
        key = _check_period(period)
        buckets: dict[str, list[dict[str, Any]]] = {
            TIER_DEDUCTIBLE: [], TIER_PENDING: [], TIER_NON_DEDUCTIBLE: []}
        for row in self.invoices(direction="purchase"):
            if row.get("period") != key:
                continue
            buckets[self.tier_of(row)].append(
                {"number": row["number"], "kind": row["kindZh"],
                 "net": row["net"], "tax": row["tax"],
                 "certified": bool(row.get("certified")),
                 "usedBy": row.get("usedBy")})
        totals = {tier: sum((Decimal(r["tax"]) for r in rows), Decimal(0))
                  for tier, rows in buckets.items()}
        total = sum(totals.values(), Decimal(0))
        return {"period": key,
                "tiers": {tier: {"tax": _fmt(amount), "count": len(buckets[tier]),
                                 "invoices": buckets[tier]}
                          for tier, amount in totals.items()},
                "totalTax": _fmt(total),
                "caliberNote": "可抵票种 + 已认证才可抵；普票不可抵（税进成本）；"
                               "可抵票种未认证归待认证"}

    # ---- 按台账过账 ----

    def purchase(self, ref: Any, invoice_number: Any, amount: Any, *,
                 cost_account: str = "5000", settle_account: str = "2000",
                 gross: bool = True, booked_on: Any = None,
                 submitted_by: str = "vat-engine") -> dict[str, Any]:
        """用一张已登记的进项票过账：按台账判抵扣，不可抵的税进成本。

        默认按**价税合计**入账（采购付款额通常就是含税总额）。
        """
        row = self.invoice(invoice_number)
        if row is None:
            raise TemplateError("NOT_FOUND",
                                f"发票未登记：{invoice_number!r}——先用 register 登记票面信息",
                                detail={"number": str(invoice_number)})
        if row.get("direction") != "purchase":
            raise TemplateError("INVALID_ARGUMENT",
                                f"{row['number']} 是销项票，不能用于采购过账",
                                detail={"direction": row.get("direction")})
        if row.get("usedBy"):
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"发票 {row['number']} 已被凭证 {row['usedBy']} 使用：同一张进项票不能抵两次",
                detail={"number": row["number"], "usedBy": row["usedBy"]})
        parts = split_tax(amount, row["rate"], gross=gross)
        if Decimal(parts["tax"]) != Decimal(row["tax"]):
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"过账税额 {parts['tax']} 与票面 {row['tax']} 不一致："
                "同一张票不能按另一个金额抵扣",
                detail={"invoiceTax": row["tax"], "postingTax": parts["tax"]})
        deductible = self.tier_of(row) == TIER_DEDUCTIBLE
        out = self._lane.post_purchase(ref, amount, rate=row["rate"], gross=gross,
                                       settle_account=settle_account,
                                       cost_account=cost_account,
                                       booked_on=booked_on,
                                       submitted_by=submitted_by,
                                       deductible=deductible)
        stable = out.get("stableId")
        if not out.get("duplicate"):
            self._log.append({"kind": "usage", "key": row["key"],
                              "number": row["number"], "usedBy": stable,
                              "ref": str(ref), "deductible": deductible})
        return {**out, "invoice": row["number"], "invoiceKind": row["kindZh"],
                "tier": self.tier_of(row), "deductible": deductible,
                "caliber_note": ("已认证专票：税额单独列进项" if deductible
                                 else "该票不可抵（普票或未认证）：税额并入成本")}

    # ---- 申报口径 ----

    def filing_summary(self, period: Any) -> dict[str, Any]:
        """销项「税率 × 票种」矩阵 + 进项三档（申报表附列资料一的形状）。

        红字票以负数进矩阵并单独计数：矩阵是**净额**，但"开了几张红票、冲了多少"必须看得见——
        只看净额，一张 100 的蓝票加一张 100 的红票会被读成"本月没开票"。
        """
        key = _check_period(period)
        matrix: dict[str, dict[str, Decimal]] = {}
        red_count = 0
        red_net = red_tax = Decimal(0)
        for row in self.invoices(direction="sale"):
            if row.get("period") != key:
                continue
            if row.get("red"):
                red_count += 1
                red_net += Decimal(row["net"])
                red_tax += Decimal(row["tax"])
            bucket = matrix.setdefault(str(row["rate"]), {})
            for kind_key in INVOICE_KINDS:
                bucket.setdefault(kind_key, Decimal(0))
            if row["invoiceKind"] in bucket:
                bucket[row["invoiceKind"]] += Decimal(row["tax"])
        sales: list[dict[str, Any]] = []
        for rate in sorted(matrix, key=lambda r: float(r), reverse=True):
            for kind_key, amount in matrix[rate].items():
                if amount == 0:
                    continue
                rows = [r for r in self.invoices(direction="sale")
                        if r.get("period") == key and str(r["rate"]) == rate
                        and r["invoiceKind"] == kind_key]
                blue = [r for r in rows if not r.get("red")]
                sales.append({
                    "rate": rate, "invoiceKind": kind_key,
                    "kindZh": INVOICE_KINDS[kind_key]["zh"],
                    "sales": _fmt(sum((Decimal(r["net"]) for r in rows), Decimal(0))),
                    "tax": _fmt(amount), "count": len(blue),
                    "redCount": len(rows) - len(blue)})
        return {"period": key, "salesByRateAndKind": sales,
                "redLetters": {"count": red_count, "net": _fmt(red_net),
                               "tax": _fmt(red_tax)},
                "inputDeduction": self.deduction(key),
                "caliberNote": "销项按「税率 × 票种」分列（对应申报表附列资料一），"
                               "红字票以负数净额并单独计数；"
                               "进项按可抵/待认证/不可抵三档"}

    def reconcile(self, period: Any) -> dict[str, Any]:
        """可抵扣发票税额 vs 账上 2221.01.02 期间借方发生额。"""
        key = _check_period(period)
        ledger = Decimal(0)
        for row in read_entries(self._kernel):
            if entry_period(row) != key:
                continue
            if str(row.get("account_code") or "") != INPUT_VAT:
                continue
            amount = _dec(row.get("amount_base") or row.get("amount"), "金额")
            ledger += amount if row.get("direction") == "debit" else -amount
        invoices = Decimal(self.deduction(key)["tiers"][TIER_DEDUCTIBLE]["tax"])
        diff = ledger - invoices
        out = {"period": key, "ledgerInputVat": _fmt(ledger),
               "invoiceInputVat": _fmt(invoices),
               "difference": _fmt(diff), "consistent": diff == 0,
               "caliberNote": "差额通常是手工直接记进项科目的金额：它不在台账里，无法证明可抵"}
        if diff != 0:
            out["warning"] = (f"账上进项比台账多 {_fmt(diff)}："
                              "查是否有未经台账直接记入 2221.01.02 的分录")
        return out

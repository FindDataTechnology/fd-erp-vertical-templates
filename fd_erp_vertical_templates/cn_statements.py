"""中国法定报表（change: add-cn-localization-pack 任务 4）。

按《小企业会计准则》的报表项目（会小企 01/02 表）出资产负债表与利润表，并给出增值税申报口径。

## 为什么报表项目是数据、不是代码

准则报表项目会随准则修订变，写成代码就得改代码；写成 `statements-small.json` 里的
「科目码规则」（全码/前缀 + 符号 + 合计关系）只是改数据。更重要的是规则能反过来当**覆盖闸**：
某个已发生额的科目落不进任何项目，说明映射有洞——必须点名，不许静默。

**漏项的报表照样平**：资产与负债同时少一笔，合计仍然相等。所以 `balanced` 与 `complete`
是两个判断：前者说「账平不平」，后者说「该有的都在表上」。

## 为什么用准则科目码聚合、而不是直接数内核科目

内核只有 24 个记账码（行业车道都写在上面），准则是 156 个。中间的映射（`mapping.json`）
是**我们做的判断**，所以每条都写了理由，且映射目标必须是包里真有的科目（加载期校验）。
`1231` 这种同名不同义的坑就是这么避开的：准则里 1231 是「其他流动资产」，我们的 1231 是坏账准备。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_period
from .errors import TemplateError
from .localization import CnPack
from .cn_tax import vat_position
from .statements import CHART

Q2 = Decimal("0.01")
SCHEMA = "fd-erp-vertical-templates/cn-statements/v1"
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: 增值税两个明细科目（与 cn_tax 同源；这里只读账，不写账）。
OUTPUT_VAT = "2221.01.01"
INPUT_VAT = "2221.01.02"
INPUT_TRANSFER_OUT = "2221.01.04"   # 进项税额转出（change: add-cn-red-letter-and-input-transfer-out）


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-11"})
    return key


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):       # pragma: no cover - 内核保证数字
        return Decimal(0)


def _signed(row: dict[str, Any]) -> Decimal:
    """按内核科目类别归符号：资产/费用借记为正，负债/权益/收入贷记为正。"""
    amount = _dec(row.get("amount_base") or row.get("amount"))
    net = amount if row.get("direction") == "debit" else -amount
    kind = CHART.get(str(row.get("account_code") or ""), (None, None))[1]
    return -net if kind in ("liability", "equity", "revenue") else net


class CnStatements:
    """中国法定报表（小企业会计准则）+ 增值税申报口径。"""

    def __init__(self, tenant_id: str, kernel: Any, *, pack: CnPack | None = None,
                 base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency
        self._pack = pack or CnPack()
        self._map = self._pack.kernel_to_cn()
        self._rules = self._pack.statements()
        #: 报表口径名随所加载的准则（两套报表同一引擎，只是行次与科目码不同）。
        self._chart = str(self._rules.get("chart") or "cn-small-business")

    # ---- 取数 ----

    def _rows(self, period: str, *, cumulative: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self._read_all():
            key = entry_period(row)
            if cumulative:
                if key in ("", "unknown") or key <= period:
                    out.append(row)
            elif key == period:
                out.append(row)
        return out

    def _read_all(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = (self._kernel.query_entries(offset=offset, limit=200) or {})
            batch = [r for r in (page.get("entries") or page.get("items")
                                 or page.get("data") or []) if isinstance(r, dict)]
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 200:
                break
            offset += len(batch)
        return rows

    def _by_cn(self, rows: list[dict[str, Any]]
               ) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, str]]:
        """(按准则科目聚合的金额, 按内核科目的金额, 未映射的内核科目及原因)。"""
        by_cn: dict[str, Decimal] = {}
        by_kernel: dict[str, Decimal] = {}
        unmapped: dict[str, str] = {}
        for row in rows:
            code = str(row.get("account_code") or "")
            if not code:
                continue
            amount = _signed(row)
            by_kernel[code] = by_kernel.get(code, Decimal(0)) + amount
            target = self._map.get(code)
            if not target:
                unmapped[code] = "内核科目没有映射到准则科目"
                continue
            by_cn[target] = by_cn.get(target, Decimal(0)) + amount
        return by_cn, by_kernel, unmapped

    # ---- 项目求值 ----

    @staticmethod
    def _covers(codes: list[str], target: str) -> bool:
        return any(target == c or target.startswith(c + ".") for c in codes)

    def _value(self, line: dict[str, Any], by_cn: dict[str, Decimal],
               computed: dict[str, Decimal], *, unclosed: Decimal | None = None
               ) -> Decimal:
        if line.get("unclosed"):
            return unclosed or Decimal(0)
        if "subtotal_of" in line:
            return sum((computed.get(k, Decimal(0)) for k in line["subtotal_of"]),
                       Decimal(0))
        if "formula" in line:
            total = Decimal(0)
            for term in line["formula"]:
                sign = -1 if str(term).startswith("-") else 1
                key = str(term).lstrip("+-")
                total += sign * computed.get(key, Decimal(0))
            return total
        total = Decimal(0)
        sign = int(line.get("sign", 1))
        for target, amount in by_cn.items():
            if self._covers(line.get("codes") or [], target):
                total += sign * amount
        return total

    def _build(self, lines: list[dict[str, Any]], by_cn: dict[str, Decimal], *,
               unclosed: Decimal | None = None) -> tuple[list[dict[str, Any]],
                                                         dict[str, Decimal]]:
        computed: dict[str, Decimal] = {}
        out: list[dict[str, Any]] = []
        for line in lines:
            value = self._value(line, by_cn, computed, unclosed=unclosed)
            computed[str(line["key"])] = value
            out.append({"key": line["key"], "label": line.get("label"),
                        "amount": _fmt(value),
                        "kind": ("subtotal" if ("subtotal_of" in line
                                                or "formula" in line
                                                or line.get("unclosed"))
                                 else "detail")})
        return out, computed

    def _unmapped(self, by_kernel: dict[str, Decimal],
                  missing: dict[str, str], lines: list[dict[str, Any]], *,
                  include_kinds: tuple[str, ...] | None = None
                  ) -> list[dict[str, Any]]:
        covered = [c for line in lines for c in (line.get("codes") or [])]
        out: list[dict[str, Any]] = []
        for code in sorted(by_kernel):
            amount = by_kernel[code]
            if amount == 0:
                continue
            if include_kinds is not None:
                kind = CHART.get(code, (None, None))[1]
                if kind not in include_kinds:
                    continue
            target = self._map.get(code)
            if not target:
                out.append({"accountCode": code, "amount": _fmt(amount),
                            "reason": missing.get(code, "未映射")})
                continue
            if not self._covers(covered, target):
                out.append({"accountCode": code, "amount": _fmt(amount),
                            "reason": f"映射到准则 {target}，但该科目不在任何报表项目里"})
        return out

    # ---- 资产负债表 ----

    def balance_sheet(self, period: Any) -> dict[str, Any]:
        key = _check_period(period)
        rows = self._rows(key, cumulative=True)
        by_cn, by_kernel, missing = self._by_cn(rows)
        bs = self._rules["balanceSheet"]
        # 未结转损益：账上仍挂着的损益净额（结转后进 3100/留存收益）
        unclosed = Decimal(0)
        for row in rows:
            kind = CHART.get(str(row.get("account_code") or ""), (None, None))[1]
            if kind == "revenue":
                unclosed += _signed(row)
            elif kind == "expense":
                unclosed -= _signed(row)

        assets, a_tot = self._build(bs["assets"], by_cn)
        liabilities, l_tot = self._build(bs["liabilities"], by_cn)
        equity, e_tot = self._build(bs["equity"], by_cn, unclosed=unclosed)

        total_assets = a_tot["total_assets"]
        total_right = l_tot["total_liabilities"] + e_tot["total_equity"]
        unmapped = self._unmapped(by_kernel, missing,
                                  bs["assets"] + bs["liabilities"] + bs["equity"],
                                  include_kinds=("asset", "liability", "equity"))
        out = {
            "schema": SCHEMA, "period": key, "chart": self._chart,
            "assets": assets, "liabilities": liabilities, "equity": equity,
            "totals": {
                "assets": _fmt(total_assets),
                "liabilities": _fmt(l_tot["total_liabilities"]),
                "equity": _fmt(e_tot["total_equity"]),
                "liabilitiesAndEquity": _fmt(total_right),
            },
            "balanced": total_assets == total_right,
            "difference": _fmt(total_assets - total_right),
            "unmapped": unmapped,
            "complete": not unmapped,
            "caliberNote": "小企业会计准则格式；资产总计 = 负债和所有者权益总计；"
                           "「未结转损益」是账上仍挂着的损益净额，结转后进留存收益",
        }
        if out["difference"] != "0.00":
            out["error"] = (f"资产负债表不平：资产 {_fmt(total_assets)} ≠ "
                            f"负债+权益 {_fmt(total_right)}（差 {_fmt(total_assets - total_right)}）"
                            "——先看 unmapped 里是否有落不进项目的科目")
        return out

    # ---- 利润表 ----

    def income_statement(self, period: Any) -> dict[str, Any]:
        key = _check_period(period)
        rows = self._rows(key, cumulative=False)
        by_cn, by_kernel, missing = self._by_cn(rows)
        lines, tot = self._build(self._rules["incomeStatement"], by_cn)
        unmapped = self._unmapped(by_kernel, missing, self._rules["incomeStatement"],
                                  include_kinds=("revenue", "expense"))
        out = {
            "schema": SCHEMA, "period": key, "chart": self._chart,
            "lines": lines,
            "totals": {"operatingProfit": _fmt(tot["operating_profit"]),
                       "totalProfit": _fmt(tot["total_profit"]),
                       "netProfit": _fmt(tot["net_profit"])},
            "unmapped": unmapped,
            "complete": not unmapped,
            "caliberNote": "小企业会计准则格式（会小企 02 表）；本期发生额口径，不是累计；"
                           "在制品（4001）与制造费用是成本归集科目，不进利润表",
        }
        return out

    # ---- 增值税申报口径 ----

    def vat_return(self, period: Any, *, declared: dict[str, Any] | None = None,
                   invoices: Any = None) -> dict[str, Any]:
        """账面口径 vs 申报口径。

        `declared` 由申报方/车道给出（销售额、销项税额、进项税额）；不给就按账面视同申报，
        并在口径说明里写明「未提供独立申报数」——不能让读者以为核过了。
        """
        key = _check_period(period)
        rows = self._rows(key, cumulative=False)
        sales = Decimal(0)
        for row in rows:
            code = str(row.get("account_code") or "")
            if CHART.get(code, (None, None))[1] != "revenue":
                continue
            amount = _dec(row.get("amount_base") or row.get("amount"))
            sales += -amount if row.get("direction") == "debit" else amount
        # 销项/进项/转出/上期留抵/应纳**只从 vat_position 取**：报表层另写一遍式子，
        # 今天一致、改一处就漂，而漂了不会报错——只会让「申报口径 vs 计提口径」差一个数。
        pos = vat_position(self._kernel, key)
        ledger = {"sales": sales,
                  "outputVat": Decimal(pos["outputVat"]),
                  "inputVat": Decimal(pos["inputVat"]),
                  "inputTransferOut": Decimal(pos["inputTransferOut"]),
                  "openingCredit": Decimal(pos["openingCredit"]),
                  "payable": Decimal(pos["payable"])}
        if declared is None:
            declared_values = dict(ledger)
            note = "未提供独立申报数：以下「申报口径」即账面口径，勾稽恒成立，不能当作已核对"
        else:
            declared_values = {
                "sales": _dec(declared.get("sales")),
                "outputVat": _dec(declared.get("outputVat")),
                "inputVat": _dec(declared.get("inputVat")),
                "inputTransferOut": _dec(declared.get("inputTransferOut", 0)),
                "openingCredit": _dec(declared.get("openingCredit", 0)),
            }
            declared_values["payable"] = (declared_values["outputVat"]
                                          - declared_values["inputVat"]
                                          + declared_values["inputTransferOut"]
                                          - declared_values["openingCredit"])
            note = "申报口径由调用方提供，与账面逐项对比"
        items = [("sales", "销售额"), ("outputVat", "销项税额"),
                 ("inputVat", "进项税额"), ("inputTransferOut", "进项税额转出"),
                 ("openingCredit", "上期留抵"), ("payable", "应纳税额")]
        reconciliation = [{"item": k, "label": label,
                           "ledger": _fmt(ledger[k]),
                           "declared": _fmt(declared_values[k]),
                           "difference": _fmt(ledger[k] - declared_values[k])}
                          for k, label in items]
        consistent = all(Decimal(r["difference"]) == 0 for r in reconciliation)
        out = {
            "schema": SCHEMA, "period": key,
            "ledger": {k: _fmt(ledger[k]) for k in ledger},
            "declared": {k: _fmt(declared_values[k]) for k in declared_values},
            "reconciliation": reconciliation,
            "consistent": consistent,
            "provided": declared is not None,
            "caliberNote": note,
        }
        # 进项三档来自发票台账（可抵/待认证/不可抵）：账上只有可抵那一档，
        # 另外两档不进账但决定下期能不能抵，申报时要说清楚。
        if invoices is not None:
            tiers = invoices.deduction(key)["tiers"]
            out["inputTiers"] = {
                "deductible": tiers["deductible"]["tax"],
                "pendingCertification": tiers["pendingCertification"]["tax"],
                "nonDeductible": tiers["nonDeductible"]["tax"],
            }
            out["invoiceReconcile"] = invoices.reconcile(key)
        return out

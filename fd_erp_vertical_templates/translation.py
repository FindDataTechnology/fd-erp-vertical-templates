"""外币报表折算（change: add-fx-translation）。

一个有海外子公司的集团，合并报表不能把 USD 主体的数字直接和 CNY 主体相加。折算不是「乘一个
汇率」：三类项目各有各的汇率口径，混用就错，而错的报表照样平。

1. **资产/负债按期末汇率**——存量按报表日重述；
2. **收入/费用按期间平均汇率**——流量在期间内发生，用期末汇率会把汇率波动整块压进损益；
3. **权益按历史汇率**——出资当时的汇率（我们只有期间级汇率注册，故取该分录所属期间的期末汇率，
   并在输出里说明这是代理，不假装是逐笔历史汇率）。

差额不是错，是同一批净资产在两种汇率下的差：它进权益（折算差额 CTA），不进损益。

## 两个「不假装」

- **折算差额只兜折算本身**。若主体账簿自己就不平（未标注科目、分录漏行），差额会把这个错一起
  吞掉，报表就「平」了。所以先算本位币口径的残差 `untranslatedResidual`，它非零直接
  `balanced=false` 并指名——不用 CTA 盖住真实错误。
- **缺汇率、混币分录一律拒**。1:1 混入是编数；用分录的集团本位币金额顶，是拿「交易日汇率」冒充
  「期末汇率」，两条口径同时错（见 design D4）。
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .errors import TemplateError
from .fx_revaluation import RATE_AVERAGE, RATE_CLOSING
from .statements import CHART

Q2 = Decimal("0.01")
SCHEMA = "fd-erp-vertical-templates/fx-translation/v1"

#: 报表类别 → 用什么汇率。资产/负债（存量）用期末；损益（流量）用平均；权益用发生期。
_CLOSING_KINDS = ("asset", "liability")
_AVERAGE_KINDS = ("revenue", "expense")

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except Exception:                                    # noqa: BLE001
        return Decimal(0)


class TranslationBook:
    """按主体把账簿折算到集团本位币（合并前的 pre-pass）。"""

    def __init__(self, tenant_id: str, fx: Any, *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._fx = fx
        self._base = str(base_currency or "CNY").strip().upper()

    # ---- 汇率取数 ----

    def _rate(self, period: str, currency: str, kind: str,
              fallback: list[dict[str, Any]]) -> Decimal:
        """取率；`average` 缺失时退回该期间期末汇率并登记进 `fallback`（不静默）。"""
        found = self._fx.rate_of(period, currency, kind=kind)
        if found is not None:
            return _dec(found["rate"])
        if kind == RATE_AVERAGE:
            closing = self._fx.rate_of(period, currency, kind=RATE_CLOSING)
            if closing is not None:
                entry = {"currency": currency, "period": period,
                         "used": str(closing["rate"]), "kind": RATE_CLOSING}
                if entry not in fallback:
                    fallback.append(entry)
                return _dec(closing["rate"])
            raise TemplateError(
                "NOT_WIRED",
                f"{currency} 在 {period} 既无平均汇率也无期末汇率：折算率是外部事实，"
                "不猜、不按 1:1",
                detail={"currency": currency, "period": period,
                        "missing": [RATE_CLOSING, RATE_AVERAGE]})
        raise TemplateError(
            "NOT_WIRED",
            f"{currency} 在 {period} 缺期末汇率：外币主体无法折算，请在汇率表登记后再合并",
            detail={"currency": currency, "period": period, "missing": kind})

    # ---- 折算 ----

    def translate(self, entity_id: Any, period: Any, rows: list[dict[str, Any]], *,
                  functional_currency: Any) -> dict[str, Any]:
        """把一个主体的分录折成本位币。

        `rows` 是该主体在**本期及以前**的全部分录（资产负债表是累计口径）；每行按所属期间
        取率——损益按各期平均、权益按各期期末，这样累计损益不会用同一个率一刀切。
        """
        who = str(entity_id or "").strip()
        if not who:
            raise TemplateError("INVALID_ARGUMENT", "主体标识不能为空")
        key = _check_period(period)
        func = str(functional_currency or "").strip().upper()
        if not func:
            raise TemplateError("INVALID_ARGUMENT", "主体记账本位币不能为空")

        if func == self._base:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"主体 {who} 记账本位币就是集团本位币 {self._base}：无需折算",
                detail={"entity": who, "functionalCurrency": func})

        # 混币分录：主体本位币 USD，账里却有一笔 EUR。内核只存了 EUR 金额与集团本位币金额，
        # USD 金额没有落库，无法还原——拒，并指向改法。
        mixed: dict[str, int] = {}
        for row in rows:
            cur = str(row.get("currency") or func).strip().upper()
            if cur != func:
                mixed[cur] = mixed.get(cur, 0) + 1
        if mixed:
            raise TemplateError(
                "NOT_WIRED",
                f"主体 {who} 的账簿里有非记账本位币（{func}）的分录："
                + "、".join(f"{c}×{n}" for c, n in sorted(mixed.items()))
                + "——内核没有存这些分录的本位币金额，无法折算；"
                "请先在主体账内把它们调汇到本位币再合并",
                detail={"entity": who, "functionalCurrency": func,
                        "mixedCurrency": mixed})

        unclassified = sorted({str(r.get("account_code") or "")
                               for r in rows
                               if str(r.get("account_code") or "") not in CHART})
        if unclassified:
            raise TemplateError(
                "NOT_WIRED",
                f"主体 {who} 有科目不在折算口径里（{'、'.join(unclassified)}）："
                "不知道它属于资产还是损益，就无法选汇率；先把科目映射进报表科目表",
                detail={"entity": who, "unclassified": unclassified})

        fallback: list[dict[str, Any]] = []
        closing = self._rate(key, func, RATE_CLOSING, fallback)

        totals = {"assets": Decimal(0), "liabilities": Decimal(0),
                  "equity": Decimal(0), "revenue": Decimal(0), "expense": Decimal(0)}
        raw = {"assets": Decimal(0), "liabilities": Decimal(0),
               "equity": Decimal(0), "revenue": Decimal(0), "expense": Decimal(0)}
        translated_rows: list[dict[str, Any]] = []
        by_period: dict[str, dict[str, Decimal]] = {}

        for row in rows:
            code = str(row.get("account_code") or "")
            _line, kind = CHART[code]
            amount = _dec(row.get("amount") if row.get("amount") not in (None, "")
                          else row.get("amount_base"))
            signed = amount if row.get("direction") == "debit" else -amount
            when = str(row.get("booked_on") or "")[:7] or key
            if kind in _CLOSING_KINDS:
                rate = closing
            elif kind in _AVERAGE_KINDS:
                rate = self._rate(when, func, RATE_AVERAGE, fallback)
            else:                                     # equity：历史汇率取发生期期末
                rate = self._rate(when, func, RATE_CLOSING, fallback)
            base_amount = (abs(amount) * rate).quantize(Q2, rounding=ROUND_HALF_UP)

            if kind == "asset":
                totals["assets"] += signed * rate
            elif kind == "liability":
                totals["liabilities"] += -signed * rate
            elif kind == "equity":
                totals["equity"] += -signed * rate
            elif kind == "revenue":
                totals["revenue"] += -signed * rate
            else:
                totals["expense"] += signed * rate
            if kind == "asset":
                raw["assets"] += signed
            elif kind == "liability":
                raw["liabilities"] += -signed
            elif kind == "equity":
                raw["equity"] += -signed
            elif kind == "revenue":
                raw["revenue"] += -signed
            else:
                raw["expense"] += signed

            per = by_period.setdefault(when, {"revenue": Decimal(0),
                                              "expense": Decimal(0)})
            if kind == "revenue":
                per["revenue"] += -signed * rate
            elif kind == "expense":
                per["expense"] += signed * rate

            translated_rows.append({**row,
                                    "amount": str(base_amount),
                                    "amount_base": str(base_amount),
                                    "currency": self._base,
                                    "fxRate": str(rate),
                                    "fxRateKind": (RATE_CLOSING if kind in _CLOSING_KINDS
                                                   or kind == "equity" else RATE_AVERAGE),
                                    "originalAmount": str(amount),
                                    "originalCurrency": func})

        retained = totals["revenue"] - totals["expense"]
        # 折算差额 = 折算后资产 − 负债 − 权益 − 未结转损益（存量与流量用了不同汇率才会非零）
        cta = totals["assets"] - totals["liabilities"] - totals["equity"] - retained
        raw_retained = raw["revenue"] - raw["expense"]
        raw_residual = (raw["assets"] - raw["liabilities"] - raw["equity"] - raw_retained)

        out: dict[str, Any] = {
            "schema": SCHEMA,
            "entity": who,
            "period": key,
            "functionalCurrency": func,
            "baseCurrency": self._base,
            "rates": {"closing": str(closing), "periods": len(by_period)},
            "totals": {
                "assets": _fmt(totals["assets"]),
                "liabilities": _fmt(totals["liabilities"]),
                "equity": _fmt(totals["equity"]),
                "revenue": _fmt(totals["revenue"]),
                "expense": _fmt(totals["expense"]),
                "retained": _fmt(retained),
                "translationDifference": _fmt(cta),
            },
            "untranslated": {
                "assets": _fmt(raw["assets"]), "liabilities": _fmt(raw["liabilities"]),
                "equity": _fmt(raw["equity"]), "retained": _fmt(raw_retained),
                "residual": _fmt(raw_residual),
            },
            "averageFallback": fallback,
            "rows": translated_rows,
            "caliberNote": (
                "资产/负债按期末汇率、损益按各期平均汇率、权益按发生期期末汇率（历史汇率的代理，"
                "我们只有期间级汇率）；折算差额 = 折算后资产 − 负债 − 权益 − 未结转损益，"
                "进权益不落分录"),
        }
        # 本位币口径就不平 ⇒ 折算差额会把真实错误一起吞掉，必须点名而不是让它「看起来平」
        if raw_residual != 0:
            out["balanced"] = False
            out["error"] = (
                f"主体 {who} 账簿本身不平：本位币口径残差 {_fmt(raw_residual)}"
                "（资产 − 负债 − 权益 − 未结转损益）——先查未标注科目或漏行，"
                "折算差额不能用来兜真实差异")
        else:
            out["balanced"] = True
        if fallback:
            out["warning"] = (
                "以下期间缺平均汇率，损益已按期末汇率折算（近似，非精确）："
                + "、".join(f"{f['currency']}@{f['period']}={f['used']}" for f in fallback))
        return out

    # ---- 多主体一次折算 ----

    def translate_all(self, period: Any,
                      entities: list[dict[str, Any]],
                      rows_by_entity: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """按主体清单折算；本位币 == 集团本位币的主体原样返回（`translated=false`）。"""
        key = _check_period(period)
        out: list[dict[str, Any]] = []
        for ent in entities:
            who = str(ent.get("entityId") or "")
            func = str(ent.get("functionalCurrency") or self._base).strip().upper()
            rows = rows_by_entity.get(who, [])
            if func == self._base:
                out.append({"entity": who, "translated": False,
                            "functionalCurrency": func, "rows": rows})
                continue
            out.append({"entity": who, "translated": True,
                        **self.translate(who, key, rows,
                                         functional_currency=func)})
        total_cta = sum((_dec(r["totals"]["translationDifference"])
                         for r in out if r.get("translated")), Decimal(0))
        return {"period": key, "baseCurrency": self._base,
                "entities": out,
                "translationDifference": _fmt(total_cta),
                "balanced": all(r.get("balanced", True) for r in out)}

"""报表期间对比（change: add-report-comparison）。

一期报表回答「现在多少」，对比回答「变好还是变坏」。看着是个减法，其实有三个坑：

1. **零基准算率**：基准为 0 时 `差额/基准` 要么除零、要么被悄悄写成 0%——两个都是谎。
   所以基准为零时变动率置 `null`，差额照常给（差额和变动率是两回事）。
2. **没有基准期间却当零**：账本第一个月做对比时，拿 0 当上期会让每一行都显示「从零增长」。
   所以基准期间没有数据时 `baselineAvailable=false`，基准值与变动率一起置空。
3. **两期各算一遍**：对比若另写一份取数逻辑，迟早与报表本身对不上。所以本期列直接调
   `income_statement` / `balance_sheet` / `cash_flow`——同一引擎，同一口径。

变动率的分母取**绝对值**：基准是 −100、本期 −300 时，差额 −200、变动率 −200%，
不会被显示成「增长 200%」。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_period
from .errors import TemplateError
from .kernel_rows import read_entries
from .statements import CATEGORY_LABELS, StatementBook, is_closing_entry

Q2 = Decimal("0.01")
PREVIOUS = "previous"
LAST_YEAR = "last-year"
YTD = "ytd"
BASES: tuple[str, ...] = (PREVIOUS, LAST_YEAR, YTD)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


def _check_basis(against: Any) -> str:
    basis = str(against or PREVIOUS).strip()
    if basis not in BASES:
        raise TemplateError("INVALID_ARGUMENT", f"对比基准不在册：{basis!r}",
                            detail={"allowed": list(BASES)})
    return basis


def _shift_month(key: str, delta: int) -> str:
    """按日历平移月份（跨年自动回退/前进），不依赖库里有没有那一期。"""
    year, month = int(key[:4]), int(key[5:])
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _range(period: str, basis: str) -> tuple[str, str, str, str]:
    """(本期起, 本期止, 基准起, 基准止)，均为 YYYY-MM 闭区间。

    YTD：本期 = 本年 1 月 → 目标月；基准 = 上年 1 月 → 上年同月。
    其余基准是单月，起止相同。
    """
    year = int(period[:4])
    if basis == YTD:
        return (f"{year:04d}-01", period,
                f"{year - 1:04d}-01", f"{year - 1:04d}-{period[5:]}")
    base = _shift_month(period, -1) if basis == PREVIOUS else _shift_month(period, -12)
    return period, period, base, base


def _months(start: str, end: str) -> list[str]:
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = _shift_month(cur, 1)
    return out


class ReportComparison:
    """三表期间对比：逐行给出本期 / 基准 / 差额 / 变动率。"""

    def __init__(self, tenant_id: str, kernel: Any, *, statements: Any = None,
                 base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._statements = statements or StatementBook(
            tenant_id, kernel, base_currency=base_currency)

    # ---- 基准 ----

    @staticmethod
    def baseline_period(period: Any, against: Any = PREVIOUS) -> str:
        key = _check_period(period)
        basis = _check_basis(against)
        if basis == YTD:
            # 累计口径的「基准期末」是上年同月，区间端点另算（见 _range）。
            return _shift_month(key, -12)
        return _shift_month(key, -1) if basis == PREVIOUS else _shift_month(key, -12)

    # ---- 累计口径（YTD）----

    def _cash_ytd(self, start: str, end: str,
                  entries: list[dict[str, Any]]) -> dict[str, Any]:
        """按月跑同一套直接法再累加：归类是逐笔的，所以逐月相加 == 区间合计。

        这里刻意**不**另写一份「区间归位」：单月与累计若各有一套实现，迟早对不上，
        而两边都「自洽」。期初/期末取区间两端，净增加额用两端相减而不是逐月相加，
        这样它同时给出了「累计是否闭合」的第二个判据。
        """
        parts = [self._statements.cash_flow(month, entries=entries)
                 for month in _months(start, end)]
        categories = {}
        for category in ("operating", "investing", "financing"):
            categories[category] = sum(
                (_dec(p["categories"][category]["net"]) for p in parts), Decimal(0))
        opening = _dec(parts[0]["openingCash"])
        closing = _dec(parts[-1]["closingCash"])
        return {
            "categories": categories,
            "openingCash": opening, "closingCash": closing,
            "netIncrease": closing - opening,
            "inconsistentMonths": [p["period"] for p in parts if not p["consistent"]],
            "unclassified": sorted({c for p in parts
                                    for c in (p.get("unclassified") or [])}),
        }

    def _income_ytd(self, entries: list[dict[str, Any]], start: str,
                    end: str) -> dict[str, Any]:
        window = [e for e in entries
                  if start <= entry_period(e) <= end]
        return self._statements._income_from(window)

    def _compare_ytd(self, period: str) -> dict[str, Any]:
        start, end, bstart, bend = _range(period, YTD)
        entries = read_entries(self._kernel)

        cur_inc = self._income_ytd(entries, start, end)
        base_inc = self._income_ytd(entries, bstart, bend)
        base_lines = {row["line"]: row["amount"] for row in base_inc["lines"]}
        flow_ok = any(bstart <= entry_period(e) <= bend for e in entries
                      if not is_closing_entry(e))
        inc_rows = [self._row(row["line"], row["line"], row["amount"],
                              base_lines.get(row["line"]), flow_ok)
                    for row in cur_inc["lines"]]

        cur_cash = self._cash_ytd(start, end, entries)
        base_cash = self._cash_ytd(bstart, bend, entries)
        cash_rows = [self._row(category, CATEGORY_LABELS[category],
                               cur_cash["categories"][category],
                               base_cash["categories"][category], flow_ok)
                     for category in ("operating", "investing", "financing")]
        cash_rows.append(self._row("netIncrease", "现金净增加额",
                                   cur_cash["netIncrease"],
                                   base_cash["netIncrease"], flow_ok))

        income_table = self._table(inc_rows, flow_ok, period, bend)
        income_table["periodRange"] = [start, end]
        income_table["baselineRange"] = [bstart, bend]
        cash_table = self._table(cash_rows, flow_ok, period, bend)
        cash_table["periodRange"] = [start, end]
        cash_table["baselineRange"] = [bstart, bend]
        cash_table["consistent"] = (not cur_cash["inconsistentMonths"]
                                    and not cur_cash["unclassified"])
        if cur_cash["inconsistentMonths"]:
            cash_table["inconsistentMonths"] = cur_cash["inconsistentMonths"]
            cash_table["note"] = ("累计里这些月份不自证（有未归类科目或缺对手行），"
                                  "累计数照加但不宣称可信：" +
                                  ", ".join(cur_cash["inconsistentMonths"]))
        if cur_cash["unclassified"]:
            cash_table["unclassified"] = cur_cash["unclassified"]

        return {
            "period": period, "against": YTD, "baselinePeriod": bend,
            "periodRange": [start, end], "baselineRange": [bstart, bend],
            "statements": {
                "incomeStatement": income_table,
                "balanceSheet": {
                    "notApplicable": True,
                    "reason": "累计口径对存量表没有意义（余额是时点数）："
                              "要比期末余额请用 last-year",
                },
                "cashFlow": cash_table,
            },
            "caliber_note": "YTD：本期 = 本年 1 月至目标月累计，基准 = 上年 1 月至上年同月累计；"
                            "流量表逐月用同一套单月引擎再累加（不另写区间归位）；"
                            "现金净增加额按区间两端现金相减，同时暴露累计是否闭合",
        }

    def _periods(self) -> list[str]:
        return [entry_period(e) for e in read_entries(self._kernel)
                if not is_closing_entry(e)]

    def _flow_available(self, baseline: str) -> bool:
        """流量表（利润表/现金流量表）的基准可用性：基准期间有分录才算有基准。"""
        return any(p == baseline for p in self._periods())

    def _stock_available(self, baseline: str) -> bool:
        """存量表（资产负债表）：基准期末的累计余额有内容即可比。"""
        return any(p != "unknown" and p <= baseline for p in self._periods())

    # ---- 行 ----

    @staticmethod
    def _row(key: str, label: str, current: Any, baseline: Any,
             available: bool) -> dict[str, Any]:
        cur = _dec(current)
        if not available or baseline is None:
            return {"key": key, "label": label, "current": _fmt(cur),
                    "baseline": None, "change": None, "ratio": None}
        base = _dec(baseline)
        change = cur - base
        ratio = None if base == 0 else \
            f"{(change / abs(base) * 100).quantize(Q2, rounding=ROUND_HALF_UP)}%"
        return {"key": key, "label": label, "current": _fmt(cur),
                "baseline": _fmt(base), "change": _fmt(change), "ratio": ratio}

    def _income(self, period: str, baseline: str, available: bool) -> dict[str, Any]:
        cur = self._statements.income_statement(period)
        base = self._statements.income_statement(baseline)
        base_lines = {row["line"]: row["amount"] for row in base["lines"]}
        rows = [self._row(row["line"], row["line"], row["amount"],
                          base_lines.get(row["line"]), available)
                for row in cur["lines"]]
        return self._table(rows, available, period, baseline)

    def _balance(self, period: str, baseline: str, available: bool) -> dict[str, Any]:
        cur = self._statements.balance_sheet(period)
        base = self._statements.balance_sheet(baseline)
        base_lines: dict[str, Any] = {}
        for group in ("assets", "liabilities", "equity"):
            for line in base[group]["lines"]:
                base_lines[line["accountCode"]] = line["amount"]
        base_lines["__unclosed__"] = base["unclosedProfit"]

        rows: list[dict[str, Any]] = []
        for group, label in (("assets", "资产"), ("liabilities", "负债"),
                             ("equity", "权益")):
            for line in cur[group]["lines"]:
                rows.append(self._row(line["accountCode"],
                                      f"{label}·{line['line'] or line['accountCode']}",
                                      line["amount"],
                                      base_lines.get(line["accountCode"]), available))
            rows.append(self._row(f"__{group}_total__", f"{label}合计",
                                  cur[group]["total"], base[group]["total"], available))
        rows.append(self._row("__unclosed__", "未结转损益",
                              cur["unclosedProfit"],
                              base_lines.get("__unclosed__"), available))
        return self._table(rows, available, period, baseline)

    def _cash(self, period: str, baseline: str, available: bool) -> dict[str, Any]:
        cur = self._statements.cash_flow(period)
        base = self._statements.cash_flow(baseline)
        rows = [self._row(category, CATEGORY_LABELS[category],
                          cur["categories"][category]["net"],
                          base["categories"][category]["net"], available)
                for category in ("operating", "investing", "financing")]
        rows.append(self._row("netIncrease", "现金净增加额",
                              cur["netIncrease"], base["netIncrease"], available))
        return self._table(rows, available, period, baseline)

    @staticmethod
    def _table(rows: list[dict[str, Any]], available: bool,
               period: str, baseline: str) -> dict[str, Any]:
        out: dict[str, Any] = {"period": period, "baselinePeriod": baseline,
                               "baselineAvailable": available, "rows": rows}
        if not available:
            out["note"] = (f"{baseline} 没有可比的数（账本里没有该期间）："
                           f"不拿 0 当基准，基准值与变动率留空")
        elif any(r["ratio"] is None for r in rows):
            out["note"] = "有行的基准为零：差额照常给，变动率不适用（不除零、不写 0%）"
        return out

    # ---- 入口 ----

    def compare(self, period: Any, against: Any = PREVIOUS) -> dict[str, Any]:
        key = _check_period(period)
        basis = _check_basis(against)
        if basis == YTD:
            return self._compare_ytd(key)
        baseline = self.baseline_period(key, basis)
        flow_ok = self._flow_available(baseline)
        stock_ok = self._stock_available(baseline)
        return {
            "period": key, "against": basis, "baselinePeriod": baseline,
            "statements": {
                "incomeStatement": self._income(key, baseline, flow_ok),
                "balanceSheet": self._balance(key, baseline, stock_ok),
                "cashFlow": self._cash(key, baseline, flow_ok),
            },
            "caliber_note": "差额一律「本期 − 基准」；变动率分母取基准绝对值；"
                            "本期列由报表同引擎现算（不另写取数）；"
                            "流量表基准按「基准期间有分录」判可用，存量表按「截至基准期末有累计」",
        }


__all__ = ["BASES", "LAST_YEAR", "PREVIOUS", "ReportComparison"]

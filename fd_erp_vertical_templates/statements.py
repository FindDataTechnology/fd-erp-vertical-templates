"""财务报表引擎（change: add-financial-statements 任务 1.x）。

三张表全部**从分录现算**：不建表、不缓存、不维护第二份余额（与「账本降投影」同一条纪律）。
报表与账本不一致时，唯一要修的是账本——所以这里没有任何「重建报表」的步骤。

## 资产负债表能否平，是账本正确性的最强不变量

单笔借贷平只说明那一笔合法；把**全部**科目按资产/负债/权益/损益归位后仍然平，才说明分类与
过账都自洽。所以 `balance_sheet()` 自带 `balanced` 断言，不平就如实报，绝不四舍五入凑平。

## 科目按封闭表归位，表外科目不猜

通用科目是 14 个的封闭集（`universal_accounts` 预设）。分录里出现表外科目时，本引擎**不猜**它
属于哪一类：单列进 `unclassified` 并把 `balanced` 置 false——猜错会让报表「看起来平」，
那比不平更危险。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_period
from .errors import TemplateError

Q2 = Decimal("0.01")
STATEMENTS_SCHEMA = "fd-erp-vertical-templates/statements/v1"

#: 科目 → (报表行, 类别)。类别只用 asset/liability/equity/revenue/expense 五类，
#: 与内核 `account_type` 封闭集同构；1602 累计折旧是抵减资产，归 asset 但在报表上单列。
CHART: dict[str, tuple[str, str]] = {
    "1000": ("货币资金", "asset"),
    "1100": ("应收账款", "asset"),
    "1200": ("存货", "asset"),
    "1300": ("在途结算款", "asset"),
    "1601": ("固定资产原值", "asset"),
    "1602": ("累计折旧", "asset"),
    "2000": ("应付账款", "liability"),
    "2100": ("预收及暂收款", "liability"),
    "3000": ("业主投入资本", "equity"),
    "3100": ("留存收益", "equity"),
    "4000": ("营业收入", "revenue"),
    "5000": ("营业成本", "expense"),
    "5100": ("平台及支付费用", "expense"),
    "5200": ("其他经营费用", "expense"),
}
#: 抵减资产：报表上从资产里扣，不另起一行「负数资产」误导读者。
CONTRA_ASSETS = ("1602",)

DIMENSION_KEYS = ("department", "project", "channel", "cost_center")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _check_period(period: Any) -> str:
    key = str(period or "").strip()
    if not (_MONTH_RE.match(key) or _WEEK_RE.match(key)):
        raise TemplateError("INVALID_ARGUMENT",
                            f"期间格式须为 YYYY-MM 或 YYYY-Www：{period!r}")
    return key


class StatementBook:
    """试算平衡 / 利润表 / 资产负债表 / 按维度费用表（全部现算）。"""

    def __init__(self, tenant_id: str, kernel: Any, *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency

    # ---- 分录读取 ----

    def _entries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            out = self._kernel.query_entries(offset=offset, limit=200) or {}
            page = [r for r in (out.get("entries") or out.get("items")
                                or out.get("data") or [])
                    if isinstance(r, dict)]
            if not page:
                break
            rows.extend(page)
            offset += len(page)
            if len(page) < 200:
                break
        return rows

    @staticmethod
    def _upto(entries: list[dict[str, Any]], as_of: str | None,
              grain: str = "monthly") -> list[dict[str, Any]]:
        if as_of is None:
            return entries
        # 期间按**业务日期**归属（dates.entry_period 一处定义）：用 posted_at 的话，
        # 「10 月的单据 11 月才确认」会被算进 11 月，同一张报表还会随确认时点变化。
        return [e for e in entries if entry_period(e, grain) <= as_of]

    @staticmethod
    def _in_period(entries: list[dict[str, Any]], period: str,
                   grain: str = "monthly") -> list[dict[str, Any]]:
        return [e for e in entries if entry_period(e, grain) == period]

    # ---- 汇总原语 ----

    @staticmethod
    def _sums(entries: list[dict[str, Any]]) -> dict[str, dict[str, Decimal]]:
        """科目 → {debit, credit, net}（net 用「借 − 贷」的单一符号约定）。"""
        out: dict[str, dict[str, Decimal]] = {}
        for e in entries:
            code = str(e.get("account_code") or "").strip()
            if not code:
                continue
            amount = _dec(e.get("amount_base") or e.get("amount"))
            bucket = out.setdefault(code, {"debit": Decimal(0), "credit": Decimal(0)})
            if e.get("direction") == "debit":
                bucket["debit"] += amount
            else:
                bucket["credit"] += amount
        for bucket in out.values():
            bucket["net"] = bucket["debit"] - bucket["credit"]
        return out

    # ---- 1) 试算平衡表 ----

    def trial_balance(self, period: str | None = None) -> dict[str, Any]:
        """每科目借/贷合计与余额（截至期末的累计），带借贷平衡断言。"""
        key = _check_period(period) if period not in (None, "") else None
        entries = self._upto(self._entries(), key)
        sums = self._sums(entries)
        rows = []
        debit_total = credit_total = Decimal(0)
        unclassified: list[str] = []
        for code in sorted(sums):
            bucket = sums[code]
            line, kind = CHART.get(code, (None, None))
            debit_total += bucket["debit"]
            credit_total += bucket["credit"]
            if kind is None:
                unclassified.append(code)
            rows.append({
                "accountCode": code, "line": line, "kind": kind,
                "debit": _fmt(bucket["debit"]), "credit": _fmt(bucket["credit"]),
                "balance": _fmt(bucket["net"]),
                "direction": "debit" if bucket["net"] >= 0 else "credit",
            })
        balanced = debit_total == credit_total
        out = {"asOf": key, "rows": rows,
               "totals": {"debit": _fmt(debit_total), "credit": _fmt(credit_total),
                          "difference": _fmt(debit_total - credit_total)},
               "balanced": balanced,
               "caliber_note": "由已落账分录现算；余额符号统一为「借 − 贷」"}
        if unclassified:
            out["unclassified"] = sorted(unclassified)
            out["note"] = f"表外科目 {', '.join(sorted(unclassified))} 未归类（不影响借贷平衡）"
        if not balanced:
            out["error"] = (f"借贷合计不等：借 {_fmt(debit_total)} / 贷 "
                            f"{_fmt(credit_total)}——账本本身有问题，不是报表口径问题")
        return out

    # ---- 2) 利润表 ----

    def income_statement(self, period: str) -> dict[str, Any]:
        """按期间给出收入、成本、费用与营业利润。"""
        key = _check_period(period)
        sums = self._sums(self._in_period(self._entries(), key))

        def amount(code: str) -> Decimal:
            bucket = sums.get(code)
            if bucket is None:
                return Decimal(0)
            # 收入在贷方（取反），成本费用在借方（取正）
            return (bucket["credit"] - bucket["debit"]) if code == "4000" \
                else (bucket["debit"] - bucket["credit"])

        revenue = amount("4000")
        cogs = amount("5000")
        platform = amount("5100")
        other = amount("5200")
        opex = platform + other
        profit = revenue - cogs - opex
        return {
            "period": key,
            "revenue": _fmt(revenue), "costOfSales": _fmt(cogs),
            "platformFees": _fmt(platform), "otherOpex": _fmt(other),
            "operatingExpenses": _fmt(opex), "operatingProfit": _fmt(profit),
            "lines": [
                {"line": "营业收入", "amount": _fmt(revenue)},
                {"line": "营业成本", "amount": _fmt(cogs)},
                {"line": "平台及支付费用", "amount": _fmt(platform)},
                {"line": "其他经营费用", "amount": _fmt(other)},
                {"line": "营业利润", "amount": _fmt(profit)},
            ],
            "caliber_note": "收入取贷方净额，成本费用取借方净额；不混入资本类科目；"
                            "本表不含所得税与营业外收支（内核科目表没有这两类）",
        }

    # ---- 3) 资产负债表 ----

    def balance_sheet(self, as_of: str | None = None) -> dict[str, Any]:
        """截至期末的资产负债与未结转损益；自带平衡断言。"""
        key = _check_period(as_of) if as_of not in (None, "") else None
        sums = self._sums(self._upto(self._entries(), key))

        assets: list[dict[str, Any]] = []
        liabilities: list[dict[str, Any]] = []
        equity: list[dict[str, Any]] = []
        unclassified: list[str] = []
        asset_total = liability_total = equity_total = Decimal(0)

        for code in sorted(sums):
            bucket = sums[code]
            line, kind = CHART.get(code, (None, None))
            if kind is None:
                unclassified.append(code)
                continue
            if kind == "asset":
                # `net` 已经是「借 − 贷」，抵减资产的贷方余额天然是负数，
                # 直接加进资产就是减少——**不要再取反一次**（取反会让资产虚增两倍，
                # 而资产负债表恰好会因此不平；本轮就是被这条断言抓到的）。
                value = bucket["net"]
                asset_total += value
                assets.append({"accountCode": code, "line": line,
                               "amount": _fmt(value),
                               "contra": code in CONTRA_ASSETS})
            elif kind == "liability":
                value = -bucket["net"]       # 负债在贷方
                liability_total += value
                liabilities.append({"accountCode": code, "line": line,
                                    "amount": _fmt(value)})
            elif kind == "equity":
                value = -bucket["net"]
                equity_total += value
                equity.append({"accountCode": code, "line": line,
                               "amount": _fmt(value)})

        # 未结转损益 = 收入净额 − 成本费用净额（权益的等价物，等结账后才转 3100）
        revenue = -sums.get("4000", {}).get("net", Decimal(0))
        expense = sum((sums.get(c, {}).get("net", Decimal(0))
                       for c in ("5000", "5100", "5200")), Decimal(0))
        retained = revenue - expense
        rhs = liability_total + equity_total + retained
        balanced = (asset_total == rhs) and not unclassified
        out = {
            "asOf": key,
            "assets": {"lines": assets, "total": _fmt(asset_total)},
            "liabilities": {"lines": liabilities, "total": _fmt(liability_total)},
            "equity": {"lines": equity, "total": _fmt(equity_total)},
            "unclosedProfit": _fmt(retained),
            "liabilitiesPlusEquity": _fmt(rhs),
            "balanced": balanced,
            "caliber_note": "累计截至期末；未结转损益 = 收入净额 − 成本费用净额，"
                            "结账转 3100 后该行归零",
        }
        if unclassified:
            out["unclassified"] = sorted(unclassified)
            out["error"] = (f"表外科目 {', '.join(sorted(unclassified))} 无法归类——"
                            f"不猜类型、不塞进任何一侧，故报表声明为不平")
        elif not balanced:
            out["error"] = (f"资产负债不平：资产 {_fmt(asset_total)} ≠ 负债+权益+未结转损益 "
                            f"{_fmt(rhs)}——差 {_fmt(asset_total - rhs)}，先查账再看报表")
        return out

    # ---- 4) 按维度费用表 ----

    def expenses_by_dimension(self, period: str,
                              dimension: str = "department") -> dict[str, Any]:
        """费用按维度汇总；未标注维度的单列（不均摊、不丢弃）。"""
        key = _check_period(period)
        dim = str(dimension or "").strip()
        if dim not in DIMENSION_KEYS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"维度不在册：{dim!r}",
                                detail={"allowed": list(DIMENSION_KEYS)})
        entries = self._in_period(self._entries(), key)
        buckets: dict[str, Decimal] = {}
        total = Decimal(0)
        for e in entries:
            code = str(e.get("account_code") or "")
            if code not in ("5000", "5100", "5200"):
                continue
            amount = _dec(e.get("amount_base") or e.get("amount"))
            signed = amount if e.get("direction") == "debit" else -amount
            value = str((e.get("dimensions") or {}).get(dim) or "").strip() \
                or "unattributed"
            buckets[value] = buckets.get(value, Decimal(0)) + signed
            total += signed
        rows = [{"dimensionValue": k, "amount": _fmt(v),
                 "attributed": k != "unattributed"}
                for k, v in sorted(buckets.items())]
        return {"period": key, "dimension": dim, "rows": rows,
                "total": _fmt(total),
                "caliber_note": "同一批分录同时汇总成本与期间费用；"
                                "标了别的维度的费用在本表里算作 unattributed"
                                "（换个维度再看，别当成没归属）"}

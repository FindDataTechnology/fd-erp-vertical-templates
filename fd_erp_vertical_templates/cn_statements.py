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
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_date, entry_period
from .errors import TemplateError
from .localization import CnPack
from .cn_tax import vat_position
from .statements import CHART, cash_flow_report, is_closing_entry

Q2 = Decimal("0.01")
SCHEMA = "fd-erp-vertical-templates/cn-statements/v1"
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: 增值税两个明细科目（与 cn_tax 同源；这里只读账，不写账）。
OUTPUT_VAT = "2221.01.01"
INPUT_VAT = "2221.01.02"
INPUT_TRANSFER_OUT = "2221.01.04"   # 进项税额转出（change: add-cn-red-letter-and-input-transfer-out）


def _by_statutory_line(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按报表行名合并（同一行由多个对手科目凑成时，合计才是那一行）。"""
    merged: dict[str, dict[str, Any]] = {}
    for row in lines:
        key = str(row.get("line") or row.get("account") or row.get("accountCode"))
        slot = merged.setdefault(key, {"line": key, "accountCodes": [],
                                       "inflow": Decimal(0), "outflow": Decimal(0),
                                       "net": Decimal(0)})
        slot["accountCodes"].append(str(row.get("accountCode")))
        slot["inflow"] += Decimal(str(row.get("inflow") or "0"))
        slot["outflow"] += Decimal(str(row.get("outflow") or "0"))
        slot["net"] += Decimal(str(row.get("net") or "0"))
    out = []
    for slot in merged.values():
        slot.update({"inflow": _fmt(slot["inflow"]), "outflow": _fmt(slot["outflow"]),
                     "net": _fmt(slot["net"])})
        slot["accountCodes"] = sorted(slot["accountCodes"])
        out.append(slot)
    return out


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

    @staticmethod
    def _year_start(period: str) -> date:
        return date(int(period[:4]), 1, 1)

    @classmethod
    def _period_open_end(cls, period: str) -> tuple[date, date]:
        """期间的半开区间 [起, 止)。"""
        year, month = int(period[:4]), int(period[5:7])
        start = date(year, month, 1)
        return start, date(year + (month == 12), month % 12 + 1, 1)

    @staticmethod
    def _prior_period(period: str) -> str:
        """上年同期＝**同月去年**。取「上一期」（2026-11 → 2026-10）是另一个数，
        而且错得毫无痕迹：报表照样平、利润照样像样，只是对不上税务局那一份。"""
        return f"{int(period[:4]) - 1:04d}-{period[5:7]}"

    @staticmethod
    def _rows_before(rows: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
        """`day` 之前（含无日期——报表口径把无日期算在最早）。"""
        return [r for r in rows if (d := entry_date(r)) is None or d < day]

    @classmethod
    def _rows_in_year(cls, rows: list[dict[str, Any]], period: str
                      ) -> list[dict[str, Any]]:
        """本年度 1 月 1 日起至本期（半开）。流量窗口不收无日期分录：它们没有时点，
        放进哪一期都是猜；资产负债表把它们计入期末是另一回事（余额不需要时点）。"""
        start = cls._year_start(period)
        _start, end = cls._period_open_end(period)
        return [r for r in rows if (d := entry_date(r)) is not None and start <= d < end]

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

    def _balance_from(self, rows: list[dict[str, Any]], *,
                      with_unmapped: bool = True) -> dict[str, Any]:
        """一套行次、只吃给定的一批分录——期末与年初走同一段代码。"""
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
        out: dict[str, Any] = {
            "assets": assets, "liabilities": liabilities, "equity": equity,
            "totals": {
                "assets": _fmt(total_assets),
                "liabilities": _fmt(l_tot["total_liabilities"]),
                "equity": _fmt(e_tot["total_equity"]),
                "liabilitiesAndEquity": _fmt(total_right),
            },
            "balanced": total_assets == total_right,
            "difference": _fmt(total_assets - total_right),
        }
        if with_unmapped:
            unmapped = self._unmapped(
                by_kernel, missing, bs["assets"] + bs["liabilities"] + bs["equity"],
                include_kinds=("asset", "liability", "equity"))
            out["unmapped"] = unmapped
            out["complete"] = not unmapped
        return out

    def balance_sheet(self, period: Any, *, opening: bool = True,
                      rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """资产负债表（期末 + 年初）。

        年初不是另算一份表：同一套行次、同一段代码，只把取数窗口换成「本年 1 月 1 日之前」。
        这样「年初 + 本年变动 = 期末」是构造出来的，不是靠两处各算一遍再对。
        """
        key = _check_period(period)
        pool = self._read_all() if rows is None else rows
        # 结转分录**不排除**：它是真实分录，把损益搬进权益正是它该做的事。
        # 排除它会让「未结转损益」永远不减，余额口径与试算平衡一起失真。
        closing = [r for r in pool
                   if entry_period(r) in ("", "unknown") or entry_period(r) <= key]
        out: dict[str, Any] = {
            "schema": SCHEMA, "period": key, "chart": self._chart,
            **self._balance_from(closing),
            "caliberNote": "小企业会计准则格式；资产总计 = 负债和所有者权益总计；"
                           "「未结转损益」是账上仍挂着的损益净额，结转后进留存收益",
        }
        if out["difference"] != "0.00":
            out["error"] = (f"资产负债表不平：资产 {out['totals']['assets']} ≠ "
                            f"负债+权益 {out['totals']['liabilitiesAndEquity']}"
                            f"（差 {out['difference']}）"
                            "——先看 unmapped 里是否有落不进项目的科目")
        if opening:
            start = self._year_start(key)
            out["opening"] = {
                **self._balance_from(self._rows_before(pool, start),
                                     with_unmapped=False),
                "asOf": f"{int(key[:4]) - 1}-12-31",
                "caliberNote": "年初数＝本期所属年度 1 月 1 日之前的全部余额（含无日期分录）",
            }
        return out

    # ---- 利润表 ----

    def _income_from(self, rows: list[dict[str, Any]], *,
                     with_unmapped: bool = True) -> dict[str, Any]:
        # 结转分录必须排除（判定走 statements.is_closing_entry 那一处实现）。
        # 不排除的话，已结账月份的利润表是「收入 0、费用 0」——那不是这期没赚钱，
        # 是账已经做完了；而报表不会报错，`complete` 也照样是 true。
        rows = [r for r in rows if not is_closing_entry(r)]
        by_cn, by_kernel, missing = self._by_cn(rows)
        lines, tot = self._build(self._rules["incomeStatement"], by_cn)
        out: dict[str, Any] = {
            "lines": lines,
            "totals": {"operatingProfit": _fmt(tot["operating_profit"]),
                       "totalProfit": _fmt(tot["total_profit"]),
                       "netProfit": _fmt(tot["net_profit"])},
        }
        if with_unmapped:
            unmapped = self._unmapped(by_kernel, missing,
                                      self._rules["incomeStatement"],
                                      include_kinds=("revenue", "expense"))
            out["unmapped"] = unmapped
            out["complete"] = not unmapped
        return out

    def income_statement(self, period: Any, *, prior_year: bool = True,
                         cumulative: bool = True,
                         rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """利润表（本期 + 上年同期 + 本年累计）。

        上年同期取**同月去年**；本年累计从 1 月 1 日起算（不是从账本第一天起算——
        那是资产负债表的口径，两张表一个参数兼两种语义正是错源之一）。
        """
        key = _check_period(period)
        pool = self._read_all() if rows is None else rows
        out: dict[str, Any] = {
            "schema": SCHEMA, "period": key, "chart": self._chart,
            **self._income_from([r for r in pool if entry_period(r) == key]),
            "caliberNote": "小企业会计准则格式（会小企 02 表）；本期发生额口径，不是累计；"
                           "在制品（4001）与制造费用是成本归集科目，不进利润表",
        }
        if prior_year:
            prior = self._prior_period(key)
            out["priorYear"] = {
                "period": prior,
                **self._income_from([r for r in pool if entry_period(r) == prior],
                                    with_unmapped=False),
            }
        if cumulative:
            out["cumulative"] = {
                "period": key,
                **self._income_from(self._rows_in_year(pool, key),
                                    with_unmapped=False),
            }
        return out

    # ---- 现金流量表 ----

    def _cash_codes(self) -> tuple[str, ...]:
        """「现金」= 资产负债表「货币资金」这一行覆盖的科目，展开成实际存在的科目码。

        口径不在这里另声明一次：资产负债表说货币资金是哪些码，现金流量表的期初期末就用哪些码，
        所以「期末现金 = 资产负债表货币资金」是同一份定义推出来的，而不是两处各算一遍再对。
        """
        cf = self._rules.get("cashFlow") or {}
        key = str(cf.get("cashLine") or "cash")
        bs = self._rules.get("balanceSheet") or {}
        line = next((l for l in (bs.get("assets") or []) if str(l.get("key")) == key), None)
        if line is None:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"现金流量表指向的资产行 {key!r} 不存在——先修 statements 包",
                detail={"chart": self._chart, "cashLine": key})
        roots = [str(c) for c in (line.get("codes") or [])]
        return tuple(str(a["code"]) for a in self._pack.accounts()
                     if any(str(a["code"]) == r or str(a["code"]).startswith(r + ".")
                            for r in roots))

    def _flow_map(self) -> dict[str, Any]:
        """归位表：行键 → (类别, 行名)，方向分支的按 inflow/outflow 各给一份。"""
        cf = self._rules.get("cashFlow") or {}
        lines = cf.get("lines") or {}
        categories = cf.get("categories") or {}
        out: dict[str, Any] = {}

        def resolve(target: Any) -> tuple[str, str]:
            spec = lines.get(str(target)) or {}
            category = str(spec.get("category") or "")
            if category not in categories:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"现金流量表行 {target!r} 的类别 {category!r} 未声明",
                    detail={"chart": self._chart, "line": target})
            return category, str(spec.get("label") or target)

        for code, spec in (cf.get("map") or {}).items():
            if not isinstance(spec, dict):
                continue
            if spec.get("line"):
                out[str(code)] = resolve(spec["line"])
            else:
                out[str(code)] = {side: resolve(spec[side])
                                  for side in ("inflow", "outflow") if spec.get(side)}
        return out

    def _disposal_line(self) -> tuple[str, str] | None:
        spec = (self._rules.get("cashFlow") or {}).get("disposal") or {}
        line = str(spec.get("line") or "")
        if not line:
            return None
        lines = (self._rules.get("cashFlow") or {}).get("lines") or {}
        target = lines.get(line) or {}
        category = str(target.get("category") or "")
        if not category:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"现金流量表处置行 {line!r} 未在 lines 里声明",
                detail={"chart": self._chart, "line": line})
        return category, str(target.get("label") or line)

    def _cash_from(self, pool: list[dict[str, Any]], period: str, *,
                   window: tuple[date, date] | None = None,
                   with_feet: bool = True) -> dict[str, Any]:
        """一张现金流量表：同一段实现，只换取数窗口。"""
        cf = self._rules.get("cashFlow") or {}
        translated = [{**row, "account_code": self._map.get(
            str(row.get("account_code") or "").strip(),
            str(row.get("account_code") or "").strip())}
            for row in pool]
        out = cash_flow_report(
            translated, period,
            cash_codes=self._cash_codes(),
            flow_map=self._flow_map(),
            label_of=lambda code: (self._pack.account(code) or {}).get("name") or code,
            category_labels={k: str(v) for k, v in (cf.get("categories") or {}).items()},
            caliber_note=(
                "直接法（准则报表项目）。现金口径＝资产负债表「货币资金」这一行覆盖的科目"
                "（含银行待清算/在途结算），故期末现金与货币资金由同一份定义得出；"
                "对手科目按准则码归位，处置与购建、借入与偿还按方向分行；"
                "表外科目进 unclassified 并判不自证，不猜类别"),
            disposal_roots=tuple(str(r) for r in
                                 ((cf.get("disposal") or {}).get("roots") or ())),
            disposal_line=self._disposal_line(),
            window=window,
        )
        # 法定报表按**行**列示，不按科目列示：同一行可能由多个对手科目凑成
        # （处置行的固定资产/累计折旧/处置收益），逐科目一行会让同一行名出现三次、
        # 每次只有一部分金额——看着像三行，加起来才是那一行的数。
        out["categories"] = {
            category: {**bucket, "lines": _by_statutory_line(bucket["lines"])}
            for category, bucket in out["categories"].items()}
        if with_feet:
            out.update(self._cash_feet(period, out, pool))
        return out

    def cash_flow(self, period: Any, *, prior_year: bool = True,
                  cumulative: bool = True,
                  rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """现金流量表（直接法，会小企 03 表 / 会企 03 表项目）。

        账里只有内核科目码，所以先把每行的科目换成准则码再交给共用引擎——归位表因此
        与报表其余部分同一套词汇（准则科目码），校验也才能拿准则科目表去查。

        与通用 `StatementBook.cash_flow` 是同一个实现、同一套判据（三类净额之和 =
        现金增减、缺对手行与未归类都判不自证），差别只在科目码与行名。
        对照列同样是同一个实现：本年累计＝把窗口换成 1 月 1 日至期末。
        """
        key = _check_period(period)
        pool = self._read_all() if rows is None else rows
        out = self._cash_from(pool, key)
        out["schema"] = SCHEMA
        out["chart"] = self._chart
        if prior_year:
            prior = self._prior_period(key)
            out["priorYear"] = {"period": prior,
                                **self._cash_from(pool, prior, with_feet=False)}
        if cumulative:
            _start, end = self._period_open_end(key)
            out["cumulative"] = {
                "period": key,
                **self._cash_from(pool, key, window=(self._year_start(key), end),
                                  with_feet=False),
            }
        return out

    def _cash_feet(self, period: str, flow: dict[str, Any],
                   pool: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """两条脚注互证：三类净额之和 = 净增加额；期末现金 = 资产负债表货币资金。

        第三条（与通用引擎同账本比净增加额）不放在这里：那要拿两套科目码各算一遍，
        属于测试的职责，放进报表返回值只会让人以为运行时真的对过两遍。
        """
        bs = self.balance_sheet(period, opening=False, rows=pool)
        cash_line = next((l for l in bs["assets"]
                          if str(l.get("key")) == str(
                              (self._rules.get("cashFlow") or {}).get("cashLine") or "cash")),
                         None)
        balance_cash = Decimal(str((cash_line or {}).get("amount") or "0"))
        closing = Decimal(str(flow.get("closingCash") or "0"))
        categories_sum = Decimal(0)
        for bucket in (flow.get("categories") or {}).values():
            categories_sum += Decimal(str(bucket.get("net") or "0"))
        net_increase = Decimal(str(flow.get("netIncrease") or "0"))
        return {
            "sumCheck": {
                "categoriesSum": _fmt(categories_sum),
                "netIncrease": _fmt(net_increase),
                "difference": _fmt(categories_sum - net_increase),
                "ok": categories_sum == net_increase,
            },
            "balanceCheck": {
                "closingCash": _fmt(closing),
                "balanceSheetCash": _fmt(balance_cash),
                "difference": _fmt(closing - balance_cash),
                "ok": closing == balance_cash,
            },
        }

    # ---- 增值税申报口径 ----

    def vat_return(self, period: Any, *, declared: dict[str, Any] | None = None,
                   invoices: Any = None) -> dict[str, Any]:
        """账面口径 vs 申报口径。

        `declared` 由申报方/车道给出（销售额、销项税额、进项税额）；不给就按账面视同申报，
        并在口径说明里写明「未提供独立申报数」——不能让读者以为核过了。
        """
        key = _check_period(period)
        # 销售额同样排除结转分录：否则已结账月份的「账面 vs 申报」会显示一个漂亮的 0 对 0。
        rows = [r for r in self._rows(key, cumulative=False)
                if not is_closing_entry(r)]
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

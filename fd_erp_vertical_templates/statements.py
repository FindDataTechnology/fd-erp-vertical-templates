"""财务报表引擎（change: add-financial-statements 任务 1.x）。

四张表（试算平衡、利润表、资产负债表、现金流量表）全部**从分录现算**：不建表、不缓存、
不维护第二份余额（与「账本降投影」同一条纪律）。
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
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_date, entry_period
from .errors import TemplateError
from .kernel_rows import read_entries

Q2 = Decimal("0.01")
STATEMENTS_SCHEMA = "fd-erp-vertical-templates/statements/v1"

#: 科目 → (报表行, 类别)。类别只用 asset/liability/equity/revenue/expense 五类，
#: 与内核 `account_type` 封闭集同构；1602 累计折旧是抵减资产，归 asset 但在报表上单列。
CHART: dict[str, tuple[str, str]] = {
    "1000": ("货币资金", "asset"),
    "1100": ("应收账款", "asset"),
    "1231": ("坏账准备", "asset"),
    "1200": ("存货", "asset"),
    "1300": ("在途结算款", "asset"),
    "1401": ("生产成本（在制品）", "asset"),
    "1601": ("固定资产原值", "asset"),
    "1602": ("累计折旧", "asset"),
    "2000": ("应付账款", "liability"),
    "2100": ("预收及暂收款", "liability"),
    "2221": ("应交税费", "liability"),
    "2221.01": ("应交增值税", "liability"),
    "2221.01.01": ("应交增值税（销项税额）", "liability"),
    "2221.01.02": ("应交增值税（进项税额）", "liability"),
    "2221.01.07": ("应交增值税（转出未交增值税）", "liability"),
    "2221.02": ("未交增值税", "liability"),
    "3000": ("业主投入资本", "equity"),
    "3100": ("留存收益", "equity"),
    "4000": ("营业收入", "revenue"),
    "5000": ("营业成本", "expense"),
    "5100": ("平台及支付费用", "expense"),
    "5200": ("其他经营费用", "expense"),
    "5301": ("营业外收入", "revenue"),
    "5403": ("税金及附加", "expense"),
    "5711": ("营业外支出", "expense"),
    "5801": ("所得税费用", "expense"),
    "6602": ("信用减值损失", "expense"),
    "6603": ("汇兑损益", "expense"),
}
#: 抵减资产：报表上从资产里扣，不另起一行「负数资产」误导读者。
CONTRA_ASSETS = ("1602", "1231")

DIMENSION_KEYS = ("department", "project", "channel", "cost_center")

#: 损益类科目（change: add-profit-closing）。收入在贷方、成本费用在借方；这两组也是
#: 期末结转的结平对象——口径只在这里定义一次，结转车道从这里取。
REVENUE_ACCOUNTS: tuple[str, ...] = ("4000",)
EXPENSE_ACCOUNTS: tuple[str, ...] = ("5000", "5100", "5200", "6602", "6603")
PROFIT_LOSS_ACCOUNTS: tuple[str, ...] = REVENUE_ACCOUNTS + EXPENSE_ACCOUNTS
#: 结转分录的 `source_ref` 前缀。它把损益搬进 3100，所以**不进损益口径**（否则已结转
#: 期间的利润表会显示收入 0、费用 0——那是账做完了，不是这期没赚钱）。
CLOSING_PREFIX = "close:"


#: 期末调汇分录前缀（change: add-fx-revaluation）。间接法里它的影响通过「汇兑损益」加回，
#: 若再算进应收/应付变动就是同一个数既加回又减掉。
FX_REVAL_PREFIX = "fx-reval:"


def is_fx_revaluation_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return str(entry.get("source_ref") or "").startswith(FX_REVAL_PREFIX)


def is_closing_entry(entry: Any) -> bool:
    """结转分录判定（唯一实现：报表排除与结转车道自查都调这里）。"""
    if not isinstance(entry, dict):
        return False
    return str(entry.get("source_ref") or "").startswith(CLOSING_PREFIX)

# ---- 现金流量表（change: add-cash-flow-statement）----

#: 现金口径＝货币资金。只有一个科目，所以内部调拨（现金对现金）在表上净额恒为零：
#: 它 MUST 单列，不能因为「两侧都是 1000 就相消不见了」而假装没发生。
CASH_ACCOUNTS: tuple[str, ...] = ("1000",)

CATEGORIES = ("operating", "investing", "financing")

#: 间接法里的营运资本：应收 / 存货 / 在途结算款 / 应付 / 预收暂收。
#: 不含货币资金（它就是现金本身）、不含 1231（减值准备是估计，走加回项）、
#: 不含固定资产与折旧（投资活动与加回项）。
WORKING_CAPITAL_ACCOUNTS: tuple[str, ...] = ("1100", "1200", "1300", "2000", "2100")
CATEGORY_LABELS = {
    "operating": "经营活动产生的现金流量",
    "investing": "投资活动产生的现金流量",
    "financing": "筹资活动产生的现金流量",
}

#: 对手科目 → (类别, 报表行名)。**封闭集合**：不在这里的科目一律进 `unclassified`，
#: 不猜类别。1200 存货/5000 营业成本成对出现在现购与结转里，都归「购买商品支付的现金」；
#: 1231/1602/6602 是纯估计与摊销科目，正常不会与现金同笔——真出现就是异常，故不登记。
CASH_FLOW_MAP: dict[str, tuple[str, str]] = {
    "4000": ("operating", "销售商品、提供劳务收到的现金"),
    "1100": ("operating", "销售商品、提供劳务收到的现金"),
    "2100": ("operating", "收到的预收款及暂收款"),
    "2000": ("operating", "购买商品、接受劳务支付的现金"),
    "1200": ("operating", "购买商品、接受劳务支付的现金"),
    "5000": ("operating", "购买商品、接受劳务支付的现金"),
    "1300": ("operating", "结算在途款项净变动"),
    "5100": ("operating", "支付其他与经营活动有关的现金"),
    "5200": ("operating", "支付其他与经营活动有关的现金"),
    "1601": ("investing", "购建固定资产支付的现金"),
    "3000": ("financing", "吸收投资收到的现金"),
    "3100": ("financing", "分配利润支付的现金"),
}

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


def _grain(key: str) -> str:
    return "weekly" if "-W" in key else "monthly"


def _period_bounds(key: str) -> tuple[date, date]:
    """期间的半开区间 [start, end)。期初现金要按「早于 start」取，不能拿期间字符串比大小：
    `2026-W45 < 2026-11` 在字典序上成立，但那不是时间关系。"""
    if _grain(key) == "weekly":
        start = date.fromisocalendar(int(key[:4]), int(key[6:]), 1)
        return start, start + timedelta(days=7)
    year, month = int(key[:4]), int(key[5:])
    start = date(year, month, 1)
    return start, date(year + (month == 12), month % 12 + 1, 1)


class StatementBook:
    """试算平衡 / 利润表 / 资产负债表 / 按维度费用表（全部现算）。"""

    def __init__(self, tenant_id: str, kernel: Any, *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency

    # ---- 分录读取 ----

    def _entries(self) -> list[dict[str, Any]]:
        # 分页读完的实现只有一处（kernel_rows）：各车道各写一份分页，迟早有一处
        # 「读到 100 条就停」而没人发现。
        return read_entries(self._kernel)

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
        """按期间给出收入、成本、费用与营业利润。

        结转分录不进本表：它的作用是把本期损益搬进 3100，计入就会让已结转期间的利润表
        变成「收入 0、费用 0」——那是账做完了，不是这期没赚钱。
        """
        key = _check_period(period)
        out = self._income_from(self._in_period(self._entries(), key))
        return {"period": key, **out}

    def _income_from(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """利润表口径的纯计算（不含期间键）：单期与区间累计共用同一份逻辑。"""
        sums = self._sums([e for e in entries if not is_closing_entry(e)])

        def amount(code: str) -> Decimal:
            bucket = sums.get(code)
            if bucket is None:
                return Decimal(0)
            # 收入在贷方（取反），成本费用在借方（取正）
            return -(bucket["net"]) if code in REVENUE_ACCOUNTS else bucket["net"]

        revenue = amount("4000")
        cogs = amount("5000")
        platform = amount("5100")
        other = amount("5200")
        # 信用减值损失是期间费用（与平台费/其他经营费用并列），不是营业成本的调整项：
        # 混进 5000 会让毛利口径失真。
        impairment = amount("6602")
        # 汇兑损益单列：它是财务性损益，混进"其他经营费用"会让经营费用口径失真
        # （change: add-fx-revaluation）。
        fx = amount("6603")
        opex = platform + other + impairment + fx
        profit = revenue - cogs - opex
        return {
            "revenue": _fmt(revenue), "costOfSales": _fmt(cogs),
            "platformFees": _fmt(platform), "otherOpex": _fmt(other),
            "impairmentLoss": _fmt(impairment),
            "fxGainLoss": _fmt(fx),
            "operatingExpenses": _fmt(opex), "operatingProfit": _fmt(profit),
            "lines": [
                {"line": "营业收入", "amount": _fmt(revenue)},
                {"line": "营业成本", "amount": _fmt(cogs)},
                {"line": "平台及支付费用", "amount": _fmt(platform)},
                {"line": "其他经营费用", "amount": _fmt(other)},
                {"line": "信用减值损失", "amount": _fmt(impairment)},
                {"line": "汇兑损益", "amount": _fmt(fx)},
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

        # 未结转损益 = 收入净额 − 成本费用净额（权益的等价物，等结账后才转 3100）。
        # 结转分录已落账时，损益科目的累计净额天然只剩**未结转**的部分，
        # 所以这里不需要额外过滤——余额本身就是答案。
        revenue = -sum((sums.get(c, {}).get("net", Decimal(0))
                        for c in REVENUE_ACCOUNTS), Decimal(0))
        expense = sum((sums.get(c, {}).get("net", Decimal(0))
                       for c in EXPENSE_ACCOUNTS), Decimal(0))
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
            if code not in EXPENSE_ACCOUNTS or is_closing_entry(e):
                # 结转分录把费用贷方抹平：计入会让维度表出现一笔「负费用」，
                # 而那不是费用发生额，是账做完了。
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

    # ---- 5) 现金流量表（change: add-cash-flow-statement）----

    @staticmethod
    def _signed(entry: dict[str, Any]) -> Decimal:
        """分录行的带符号金额：借为正、贷为负（与 `_sums` 的 net 同一约定）。"""
        amount = _dec(entry.get("amount_base") or entry.get("amount"))
        return amount if entry.get("direction") == "debit" else -amount

    def _cash_balance(self, entries: list[dict[str, Any]]) -> Decimal:
        return sum((self._signed(e) for e in entries
                    if str(e.get("account_code") or "").strip() in CASH_ACCOUNTS),
                   Decimal(0))

    def cash_flow(self, period: str, *,
                  entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """直接法现金流量表：按对手科目归入经营/投资/筹资，并自证净增加额。

        归类不给分录加字段、也不从利润表倒推：**对手科目本身就说明了钱的来路与去向**。
        一笔交易内，每个非现金对手行的带符号金额取负，就是它带来的现金流入（正）或流出
        （负）；双分录保证这些取负之和恒等于该笔的现金净额，所以多对手行天然按金额分摊、
        逐笔可核，不需要平均或估值。
        """
        key = _check_period(period)
        # `entries` 允许注入：累计口径要把同一个月跑多次，重复读全量分录既慢又容易不一致。
        pool = self._entries() if entries is None else entries
        start, _end = _period_bounds(key)
        grain = _grain(key)
        # 期间归属复用 dates.entry_period（业务日期优先），与利润表/账龄同一口径。
        window = [e for e in pool if entry_period(e, grain) == key]
        earlier = [e for e in pool
                   if (day := entry_date(e)) is not None and day < start]

        opening = self._cash_balance(earlier)
        closing = opening + self._cash_balance(window)

        # 一笔交易 = 一个 txn_id。没有 txn_id 的行只能各自成组，那样会缺对手行——
        # 这正是下面 `fragments` 要如实点名的情况，而不是当成「内部调拨」混过去。
        groups: dict[str, list[dict[str, Any]]] = {}
        for index, entry in enumerate(window):
            gid = str(entry.get("txn_id") or entry.get("stable_id") or "").strip() \
                or f"row-{index}"
            groups.setdefault(gid, []).append(entry)

        buckets: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in CATEGORIES}
        attributions: list[dict[str, Any]] = []
        internal: list[dict[str, Any]] = []
        fragments: list[dict[str, Any]] = []
        unclassified: set[str] = set()

        for gid, legs in groups.items():
            cash_legs = [e for e in legs
                         if str(e.get("account_code") or "").strip() in CASH_ACCOUNTS]
            if not cash_legs:
                continue
            counters = [e for e in legs
                        if str(e.get("account_code") or "").strip() not in CASH_ACCOUNTS]
            cash_net = sum((self._signed(e) for e in cash_legs), Decimal(0))

            if not counters:
                if cash_net == 0:
                    # 两侧都是货币资金：内部调拨。单列、不进三类、不影响净增加额。
                    inflow = sum((s for s in (self._signed(e) for e in cash_legs)
                                  if s > 0), Decimal(0))
                    outflow = -sum((s for s in (self._signed(e) for e in cash_legs)
                                    if s < 0), Decimal(0))
                    internal.append({"txnId": gid, "inflow": _fmt(inflow),
                                     "outflow": _fmt(outflow), "net": _fmt(cash_net)})
                else:
                    fragments.append({"txnId": gid, "net": _fmt(cash_net),
                                      "reason": "只有现金行、缺对手行——交易的其余分录"
                                                "不在本期间或不在账本里"})
                continue

            for entry in counters:
                code = str(entry.get("account_code") or "").strip()
                flow = -self._signed(entry)
                mapped = CASH_FLOW_MAP.get(code)
                if mapped is None:
                    unclassified.add(code)
                    attributions.append({"txnId": gid, "accountCode": code,
                                         "category": None, "line": None,
                                         "amount": _fmt(flow)})
                    continue
                category, line = mapped
                slot = buckets[category].setdefault(
                    code, {"line": line, "inflow": Decimal(0), "outflow": Decimal(0)})
                if flow >= 0:
                    slot["inflow"] += flow
                else:
                    slot["outflow"] += -flow
                attributions.append({"txnId": gid, "accountCode": code,
                                     "category": category, "line": line,
                                     "amount": _fmt(flow)})

        categories: dict[str, dict[str, Any]] = {}
        categorized = Decimal(0)
        for category in CATEGORIES:
            lines: list[dict[str, Any]] = []
            inflow_total = outflow_total = Decimal(0)
            for code in sorted(buckets[category]):
                slot = buckets[category][code]
                net = slot["inflow"] - slot["outflow"]
                inflow_total += slot["inflow"]
                outflow_total += slot["outflow"]
                lines.append({
                    "accountCode": code,
                    "account": CHART.get(code, (code, ""))[0],
                    "line": slot["line"],
                    "inflow": _fmt(slot["inflow"]), "outflow": _fmt(slot["outflow"]),
                    "net": _fmt(net),
                })
            net = inflow_total - outflow_total
            categorized += net
            categories[category] = {"label": CATEGORY_LABELS[category], "lines": lines,
                                    "inflow": _fmt(inflow_total),
                                    "outflow": _fmt(outflow_total), "net": _fmt(net)}

        net_increase = closing - opening
        # 自证：三类净额之和 == 期末现金 − 期初现金。不等就把差额与未归类科目摆出来，
        # **不调明细去凑平**——凑平会让「有组合没归到位」这件事永远查不出来。
        difference = net_increase - categorized
        consistent = difference == 0 and not unclassified and not fragments
        out: dict[str, Any] = {
            "period": key,
            "openingCash": _fmt(opening), "closingCash": _fmt(closing),
            "netIncrease": _fmt(net_increase),
            "categories": categories,
            "categorizedNet": _fmt(categorized),
            "difference": _fmt(difference),
            "internal": internal,
            "consistent": consistent,
            "attributions": attributions,
            "caliber_note": "直接法：逐笔按对手科目归位（取负即该对手带来的现金流入），"
                            "多对手行按各自金额分摊且逐笔随 txnId 可核；"
                            "现金口径＝货币资金 1000；期间按业务日期",
        }
        if unclassified:
            out["unclassified"] = sorted(unclassified)
            out["note"] = (f"表外科目 {', '.join(sorted(unclassified))} 未进归位表，"
                           f"既不猜类别也不计入三类——报表因此判不自证")
        if fragments:
            out["fragments"] = fragments
            out["note"] = ("有交易的现金行找不到对手行（分录被拆开或不在本期），"
                           "差额已计入 difference")
        if not consistent and "error" not in out:
            out["error"] = (f"三类净额之和 {_fmt(categorized)} ≠ 净增加额 "
                            f"{_fmt(net_increase)}——差 {_fmt(difference)}，"
                            f"先查未归类科目与缺对手行的交易")
        return out

    def cash_flow_indirect(self, period: str) -> dict[str, Any]:
        """补充资料（间接法）：净利润 → 加回非现金 → 减营运资本占用 → 经营净额。

        与直接法互证：两条完全独立的推导路径必须给出同一个经营净额。对不上就是有一处
        算错了——而单看任一张表都「自洽」，所以这里必须报差额而不是调平。
        """
        key = _check_period(period)
        grain = _grain(key)
        window = [e for e in self._entries()
                  if entry_period(e, grain) == key and not is_closing_entry(e)]

        profit = Decimal(self.income_statement(key)["operatingProfit"])
        sums = self._sums(window)
        # 折旧：累计折旧在贷方增加（net 为负），加回额取正；处置转出会让它变负，如实保留。
        depreciation = -sums.get("1602", {}).get("net", Decimal(0))
        impairment = sums.get("6602", {}).get("net", Decimal(0))
        # 汇兑损益：借方是损失（加回），贷方是收益（减掉）。
        fx = sums.get("6603", {}).get("net", Decimal(0))

        adjustments = [
            {"line": "信用减值损失", "amount": _fmt(impairment)},
            {"line": "固定资产折旧", "amount": _fmt(depreciation)},
            {"line": "汇兑损益", "amount": _fmt(fx)},
        ]
        adjust_total = impairment + depreciation + fx

        wc_rows: list[dict[str, Any]] = []
        wc_total = Decimal(0)
        for code in WORKING_CAPITAL_ACCOUNTS:
            change = Decimal(0)
            for entry in window:
                if str(entry.get("account_code") or "").strip() != code:
                    continue
                if is_fx_revaluation_entry(entry):
                    continue          # 调汇的影响在「汇兑损益」加回项里，不重复计
                change += self._signed(entry)
            wc_total += change
            wc_rows.append({"accountCode": code,
                            "account": CHART.get(code, (code, ""))[0],
                            "change": _fmt(change)})

        indirect = profit + adjust_total - wc_total
        cash = self.cash_flow(key)
        direct = Decimal(str(cash["categories"]["operating"]["net"]))
        difference = indirect - direct
        unclassified = list(cash.get("unclassified") or [])
        consistent = difference == 0 and not unclassified and not cash.get("fragments")

        out: dict[str, Any] = {
            "period": key,
            "netProfit": _fmt(profit),
            "adjustments": adjustments,
            "adjustmentsTotal": _fmt(adjust_total),
            "workingCapital": wc_rows,
            "workingCapitalTotal": _fmt(wc_total),
            "indirectOperating": _fmt(indirect),
            "directOperating": _fmt(direct),
            "difference": _fmt(difference),
            "consistent": consistent,
            "caliber_note": "起点=本期营业利润（本版无所得税与营业外收支）；"
                            "营运资本按「借 − 贷」取本期变动并整体减去（资产增加是占用、"
                            "负债增加是来源）；调汇分录不计入营运资本（已通过汇兑损益加回）",
        }
        if unclassified:
            out["unclassified"] = unclassified
            out["note"] = (f"直接法有表外科目 {', '.join(unclassified)} 未归位，"
                           f"两法不可能对上——先补归位表")
        if not consistent and "error" not in out:
            if difference != 0:
                out["error"] = (f"间接法经营净额 {_fmt(indirect)} ≠ 直接法 {_fmt(direct)}"
                                f"（差 {_fmt(difference)}）——两条路径各自自洽但对不上，"
                                f"先查未归类科目与非现金项是否漏列")
            else:
                # 两法都是 0 却仍不自证：现金变了但拿不到对手科目，两法会一起「看不见」它。
                out["error"] = ("两法净额相同，但直接法有未归位科目或缺对手行的交易，"
                                "现金变动无法归类——报表仍判不自证")
        return out

"""往来账龄（change: add-receivable-aging / add-payable-aging）。

「谁欠我」和「我欠谁」是同一道题，只是开放项方向相反：应收的开放项是**借方**（客户欠我），
应付的开放项是**贷方**（我欠供应商）。所以这里只有一个引擎，`side` 说明「哪一侧算未结清」，
应收/应付各给一层薄绑定（科目默认值 + 措辞），核销、分桶、截至日、未标注单列**一份实现**。
镜像复制两份的写法必然在边界上漂移，而那种漂移不报错，只让两侧账龄对不上。

分录本身不含往来对象身份，所以归属走分析维度（默认 `customer`：客户与供应商同属往来对象）；
未标维度的分录**单列**，不摊给任何人（摊出去的数字看起来完整，实际是编的）。

## 口径：FIFO 核销，写在返回里

没有逐票匹配时，行业惯例是「收款先冲最早的未核销应收」（应付侧即「付款先冲最早的未结应付」）。
本引擎就按这个做，并且把这条口径明说在 `caliber_note` 里——调用方不该以为我们做了逐票匹配。

## 负数不是账龄

某对象累计收款大于累计应收时，如实报 `creditBalance`（预收/多收）；应付侧对应的是
`debitBalance`（预付/多付）。**不把负数塞进最小桶**——负数账龄会让「30 天内」这一行
看起来有钱可收，实际是多收了要退（或预付了要冲）。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_date
from .errors import TemplateError

Q2 = Decimal("0.01")
AGING_SCHEMA = "fd-erp-vertical-templates/aging/v2"
#: 应收账款科目 / 应付账款科目（内核通用科目）。
AR_ACCOUNT = "1100"
AP_ACCOUNT = "2000"
#: 开放项方向的可取值：应收按借方开放，应付按贷方开放。
OPEN_SIDES = ("debit", "credit")
DIMENSION_KEYS = ("customer", "department", "project", "channel", "cost_center")

#: 每一侧的说法——措辞与科目名都在这里，引擎代码里不出现「应收专用」的判断。
_SIDE_WORDS = {
    "debit": {"open": "借方", "settle": "credit", "offsetKey": "creditBalance",
              "offsetTotalKey": "totalCreditBalance",
              "offsetNote": "累计收款大于应收：这是预收/多收，不是负账龄",
              "caliber": "收款先冲最早的未核销应收"},
    "credit": {"open": "贷方", "settle": "debit", "offsetKey": "debitBalance",
               "offsetTotalKey": "totalDebitBalance",
               "offsetNote": "累计付款大于应付：这是预付/多付，不是负账龄",
               "caliber": "付款先冲最早的未结应付"},
}


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _date_of(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _bucket_labels(edges: tuple[int, ...]) -> list[str]:
    labels = [f"0-{edges[0]}天"]
    for lo, hi in zip(edges, edges[1:]):
        labels.append(f"{lo + 1}-{hi}天")
    labels.append(f"{edges[-1] + 1}天以上")
    return labels


def _bucket_index(days: int, edges: tuple[int, ...]) -> int:
    for i, edge in enumerate(edges):
        if days <= edge:
            return i
    return len(edges)


class AgingReport:
    """往来账龄引擎：FIFO 核销 + 分桶 + 未标注单列 + 多付/多收如实报。

    `side` 是**开放项**方向（`debit` 应收 / `credit` 应付）。子类只改默认值与措辞，
    不复制逻辑。
    """

    SIDE = "debit"
    ACCOUNT = AR_ACCOUNT

    def __init__(self, tenant_id: str, kernel: Any, *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency

    def _entries(self, account: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            out = self._kernel.query_entries(account_code=account,
                                             offset=offset, limit=200) or {}
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

    def report(self, as_of: Any | None = None, *, account: str | None = None,
               side: str | None = None,
               buckets: tuple[int, ...] = (30, 60, 90),
               dimension: str = "customer") -> dict[str, Any]:
        """按往来对象出账龄表；各桶合计减「多付/多收」必须等于科目净额。"""
        if not buckets or any(b <= 0 for b in buckets) or list(buckets) != sorted(buckets):
            raise TemplateError("INVALID_ARGUMENT",
                                f"账龄分界须为递增正整数：{buckets!r}")
        dim = str(dimension or "").strip()
        if dim not in DIMENSION_KEYS:
            raise TemplateError("INVALID_ARGUMENT", f"维度不在册：{dim!r}",
                                detail={"allowed": list(DIMENSION_KEYS)})
        open_dir = str(side or self.SIDE).strip()
        if open_dir not in OPEN_SIDES:
            raise TemplateError("INVALID_ARGUMENT",
                                f"开放方向只认 {list(OPEN_SIDES)}：{open_dir!r}")
        settle_dir = "credit" if open_dir == "debit" else "debit"
        words = _SIDE_WORDS[open_dir]
        acct = str(account or self.ACCOUNT)
        if as_of not in (None, ""):
            cutoff = _date_of(as_of)
            if cutoff is None:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"截至日不合法：{as_of!r}（须 YYYY-MM-DD）")
        else:
            cutoff = date.today()

        entries = []
        for e in self._entries(acct):
            # 账龄从**业务日期**起算（dates.entry_date）：用落账时刻的话，10 月的单据
            # 11 月才确认就会被算成「刚发生」，逾期看起来凭空变好。
            when = entry_date(e)
            if when is None or when > cutoff:
                continue
            entries.append({**e, "_when": when})
        entries.sort(key=lambda e: (e["_when"], int(e.get("line_no") or 0)))

        labels = _bucket_labels(tuple(buckets))
        # 每个归属：按时间排队的未结清项（FIFO 核销）
        open_items: dict[str, list[dict[str, Any]]] = {}
        offset_balance: dict[str, Decimal] = {}
        for e in entries:
            who = str(((e.get("dimensions") or {}).get(dim)) or "").strip() \
                or "unattributed"
            amount = _dec(e.get("amount_base") or e.get("amount"))
            queue = open_items.setdefault(who, [])
            if e.get("direction") == open_dir:
                queue.append({"_when": e["_when"], "amount": amount})
                continue
            # 核销项（应收的收款 / 应付的付款）：先冲最早未结项，冲不完记成预收/预付
            rest = amount
            while rest > 0 and queue:
                head = queue[0]
                take = min(head["amount"], rest)
                head["amount"] -= take
                rest -= take
                if head["amount"] <= 0:
                    queue.pop(0)
            if rest > 0:
                offset_balance[who] = offset_balance.get(who, Decimal(0)) + rest

        rows: list[dict[str, Any]] = []
        totals = [Decimal(0) for _ in labels]
        for who in sorted(open_items):
            buckets_amt = [Decimal(0) for _ in labels]
            for item in open_items[who]:
                if item["amount"] <= 0:
                    continue
                days = max(0, (cutoff - item["_when"]).days)
                buckets_amt[_bucket_index(days, tuple(buckets))] += item["amount"]
            outstanding = sum(buckets_amt, Decimal(0))
            offset = offset_balance.get(who, Decimal(0))
            if outstanding == 0 and offset == 0:
                continue
            for i, val in enumerate(buckets_amt):
                totals[i] += val
            row = {"dimensionValue": who, "attributed": who != "unattributed",
                   "buckets": [{"label": labels[i], "amount": _fmt(buckets_amt[i])}
                               for i in range(len(labels))],
                   "outstanding": _fmt(outstanding)}
            if offset > 0:
                row[words["offsetKey"]] = _fmt(offset)
                row["note"] = words["offsetNote"]
            rows.append(row)

        bucket_total = sum(totals, Decimal(0))
        offset_total = sum(offset_balance.values(), Decimal(0))
        # 净额取「开放方向为正」的口径：应收 = 借−贷，应付 = 贷−借。两侧因此共用一条
        # 一致性断言，不必各自记一个符号翻转。
        # `start=Decimal(0)` 不能省：空账（一条分录都没有）时 `sum(空生成器)` 返回 **int 0**，
        # 而下面 `_fmt` 调的是 `Decimal.quantize` —— 新客户第一天的账龄表就会 500。
        net = Decimal(0)
        for e in entries:
            amount = _dec(e.get("amount_base") or e.get("amount"))
            net += amount if e.get("direction") == open_dir else -amount
        # 净额 = 未结账龄 − 多收/多付。把多付漏掉会让「有多付的对象」永远不平，
        # 而那种不平看着像账错了。
        consistent = (bucket_total - offset_total) == net
        out = {
            "schema": AGING_SCHEMA,
            "asOf": cutoff.isoformat(), "account": acct, "dimension": dim,
            "side": open_dir,
            "bucketLabels": labels,
            "rows": rows,
            "totals": [{"label": labels[i], "amount": _fmt(totals[i])}
                       for i in range(len(labels))],
            "totalOutstanding": _fmt(bucket_total),
            words["offsetTotalKey"]: _fmt(offset_total),
            "accountNetBalance": _fmt(net),
            "consistent": consistent,
            "caliber_note": f"口径＝FIFO 核销（{words['caliber']}），未做逐票匹配；"
                            f"未标{dim}的分录单列 unattributed，不摊给任何对象",
        }
        if not consistent:
            out["error"] = (f"账龄合计 {_fmt(bucket_total)} − "
                            f"多付/多收 {_fmt(offset_total)} ≠ 科目净余额 "
                            f"{_fmt(net)}——不调桶凑合计数，先查账")
        return out


class ReceivableAging(AgingReport):
    """应收账龄：开放项＝借方，默认 `1100 应收账款`。"""

    SIDE = "debit"
    ACCOUNT = AR_ACCOUNT


class PayableAging(AgingReport):
    """应付账龄：开放项＝贷方，默认 `2000 应付账款`。

    与应收共用同一份核销与分桶实现（`AgingReport.report` 的 `side` 参数化），
    只是开放方向相反。
    """

    SIDE = "credit"
    ACCOUNT = AP_ACCOUNT

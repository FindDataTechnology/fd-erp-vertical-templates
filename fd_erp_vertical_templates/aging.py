"""应收账龄（change: add-receivable-aging 任务 2.1-2.4）。

「谁欠我、欠多久」——账龄是应收的第一张运营表。分录本身不含客户身份，所以客户归属走分析
维度 `customer`；未标客户的分录**单列**，不摊给任何人（摊出去的数字看起来完整，实际是编的）。

## 口径：FIFO 核销，写在返回里

没有逐票匹配时，行业惯例是「收款先冲最早的未核销应收」。本引擎就按这个做，并且把这条口径
明说在 `caliber_note` 里——调用方不该以为我们做了逐票匹配。

## 负数不是账龄

某客户累计收款大于累计应收时，如实报 `creditBalance`（预收/多收），**不把负数塞进最小桶**。
负数账龄会让「30 天内」这一行看起来有钱可收，实际是多收了要退。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .errors import TemplateError

Q2 = Decimal("0.01")
AGING_SCHEMA = "fd-erp-vertical-templates/receivable-aging/v1"
#: 应收账款科目（内核通用科目）。
AR_ACCOUNT = "1100"
DIMENSION_KEYS = ("customer", "department", "project", "channel", "cost_center")


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


class ReceivableAging:
    """应收账龄表（FIFO 核销 + 分桶 + 未标客户单列）。"""

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

    def report(self, as_of: Any | None = None, *, account: str = AR_ACCOUNT,
               buckets: tuple[int, ...] = (30, 60, 90),
               dimension: str = "customer") -> dict[str, Any]:
        """按客户出账龄表；各桶合计必须等于该科目净余额。"""
        if not buckets or any(b <= 0 for b in buckets) or list(buckets) != sorted(buckets):
            raise TemplateError("INVALID_ARGUMENT",
                                f"账龄分界须为递增正整数：{buckets!r}")
        dim = str(dimension or "").strip()
        if dim not in DIMENSION_KEYS:
            raise TemplateError("INVALID_ARGUMENT", f"维度不在册：{dim!r}",
                                detail={"allowed": list(DIMENSION_KEYS)})
        if as_of not in (None, ""):
            cutoff = _date_of(as_of)
            if cutoff is None:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"截至日不合法：{as_of!r}（须 YYYY-MM-DD）")
        else:
            cutoff = date.today()

        entries = []
        for e in self._entries(account):
            when = _date_of(e.get("posted_at"))
            if when is None or when > cutoff:
                continue
            entries.append({**e, "_when": when})
        entries.sort(key=lambda e: (e["_when"], int(e.get("line_no") or 0)))

        labels = _bucket_labels(tuple(buckets))
        # 每个归属：按时间排队的未核销借方（FIFO 核销）
        open_items: dict[str, list[dict[str, Any]]] = {}
        credit_balance: dict[str, Decimal] = {}
        for e in entries:
            who = str(((e.get("dimensions") or {}).get(dim)) or "").strip() \
                or "unattributed"
            amount = _dec(e.get("amount_base") or e.get("amount"))
            queue = open_items.setdefault(who, [])
            if e.get("direction") == "debit":
                queue.append({"_when": e["_when"], "amount": amount})
                continue
            # 贷方（收款/冲减）：先冲最早未核销项，冲不完的记成预收
            rest = amount
            while rest > 0 and queue:
                head = queue[0]
                take = min(head["amount"], rest)
                head["amount"] -= take
                rest -= take
                if head["amount"] <= 0:
                    queue.pop(0)
            if rest > 0:
                credit_balance[who] = credit_balance.get(who, Decimal(0)) + rest

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
            credit = credit_balance.get(who, Decimal(0))
            if outstanding == 0 and credit == 0:
                continue
            for i, val in enumerate(buckets_amt):
                totals[i] += val
            row = {"dimensionValue": who, "attributed": who != "unattributed",
                   "buckets": [{"label": labels[i], "amount": _fmt(buckets_amt[i])}
                               for i in range(len(labels))],
                   "outstanding": _fmt(outstanding)}
            if credit > 0:
                row["creditBalance"] = _fmt(credit)
                row["note"] = "累计收款大于应收：这是预收/多收，不是负账龄"
            rows.append(row)

        bucket_total = sum(totals, Decimal(0))
        credit_total = sum(credit_balance.values(), Decimal(0))
        net = sum((_dec(e.get("amount_base") or e.get("amount"))
                   if e.get("direction") == "debit"
                   else -_dec(e.get("amount_base") or e.get("amount")))
                  for e in entries)
        # 净额 = 未核销账龄 − 预收（多收）。把预收漏掉会让「有多收的客户」永远不平，
        # 而那种不平看着像账错了。
        consistent = (bucket_total - credit_total) == net
        out = {
            "asOf": cutoff.isoformat(), "account": account, "dimension": dim,
            "bucketLabels": labels,
            "rows": rows,
            "totals": [{"label": labels[i], "amount": _fmt(totals[i])}
                       for i in range(len(labels))],
            "totalOutstanding": _fmt(bucket_total),
            "totalCreditBalance": _fmt(credit_total),
            "accountNetBalance": _fmt(net),
            "consistent": consistent,
            "caliber_note": "口径＝FIFO 核销（收款先冲最早的未核销应收），未做逐票匹配；"
                            "未标客户的分录单列 unattributed，不摊给任何客户",
        }
        if not consistent:
            out["error"] = (f"账龄合计 {_fmt(bucket_total)} − 预收 {_fmt(credit_total)} ≠ "
                            f"科目净余额 {_fmt(net)}——不调桶凑合计数，先查账")
        return out

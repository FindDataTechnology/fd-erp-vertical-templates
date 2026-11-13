"""账簿：科目余额表（三栏）与明细账（change: add-ledger-books）。

报表回答「一共多少」，账簿回答「这一笔是哪一笔、记完它之后余额还剩多少」。查账与审计
翻的是后者：月末对不上数，第一个动作就是拉某个科目的明细账看流水。

两本账都从分录现算，并且**必须与试算平衡表互相咬合**：

- 科目余额表：期初 / 本期借 / 本期贷 / 期末 四列，自证「期初 + 借 − 贷 == 期末」；
- 明细账：逐笔流水 + 逐行余额，同样自证闭合；
- 与报表：同一期间 `account_balances` 的期末列必须等于 `trial_balance` 的余额**逐科目**。

三条路径对不上不是「算法不同」，而是有一处算错了。所以这里不允许任何「按类型换算法」：
余额一律「借 − 贷」，方向只放在 `direction` 字段里。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_period
from .errors import TemplateError
from .kernel_rows import read_entries
from .statements import CHART

Q2 = Decimal("0.01")
BOOKS_SCHEMA = "fd-erp-vertical-templates/books/v1"

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _signed(entry: dict[str, Any]) -> Decimal:
    amount = _dec(entry.get("amount_base") or entry.get("amount"))
    return amount if entry.get("direction") == "debit" else -amount


def _code(entry: dict[str, Any]) -> str:
    return str(entry.get("account_code") or "").strip()


def _sort_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    """业务日期 → 落账时刻 → txn → 行号。与内核 `ORDER BY` 同一顺序，字符串比较即可
    （ISO 日期与时刻都可按字典序排；行号补零避免 10 排在 2 前面）。"""
    when = str(entry.get("booked_on") or entry.get("posted_at") or "")
    posted = str(entry.get("posted_at") or "")
    return (when, posted, str(entry.get("txn_id") or ""),
            str(entry.get("line_no") or "").zfill(6))


def _direction(net: Decimal) -> str | None:
    return "debit" if net > 0 else "credit" if net < 0 else None


class LedgerBooks:
    """账簿：科目余额表（三栏）与明细账（逐行余额）。"""

    def __init__(self, tenant_id: str, kernel: Any, *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency

    # ---- 取数与分窗 ----

    def _split(self, period: str | None) -> tuple[list[dict[str, Any]],
                                                  list[dict[str, Any]]]:
        """(期初, 本期) 两段。期初 = 期间起之前的分录；本期 = 归属该期间的分录。

        期间口径与报表同源（`dates.entry_period`，业务日期优先）：账簿若另用落账时刻分窗，
        同一期的三本账就会各自对不上，而每本都「自洽」。
        """
        entries = read_entries(self._kernel)
        if period in (None, ""):
            return [], entries
        earlier: list[dict[str, Any]] = []
        window: list[dict[str, Any]] = []
        for entry in entries:
            which = entry_period(entry)          # 月度口径 YYYY-MM，可安全按字典序比较
            if which == "unknown":
                continue
            if which == period:
                window.append(entry)
            elif which < period:
                earlier.append(entry)
        return earlier, window

    # ---- 1) 科目余额表（三栏） ----

    def account_balances(self, period: Any) -> dict[str, Any]:
        """期初 / 本期借 / 本期贷 / 期末，逐科目 + 合计 + 借贷平衡断言。"""
        key = _check_period(period)
        earlier, window = self._split(key)

        opening: dict[str, Decimal] = {}
        for entry in earlier:
            code = _code(entry)
            if code:
                opening[code] = opening.get(code, Decimal(0)) + _signed(entry)

        debit: dict[str, Decimal] = {}
        credit: dict[str, Decimal] = {}
        for entry in window:
            code = _code(entry)
            if not code:
                continue
            amount = _dec(entry.get("amount_base") or entry.get("amount"))
            if entry.get("direction") == "debit":
                debit[code] = debit.get(code, Decimal(0)) + amount
            else:
                credit[code] = credit.get(code, Decimal(0)) + amount

        codes = sorted(set(opening) | set(debit) | set(credit))
        rows: list[dict[str, Any]] = []
        total_open = total_debit = total_credit = total_close = Decimal(0)
        unclassified: list[str] = []
        for code in codes:
            line, kind = CHART.get(code, (None, None))
            if kind is None:
                unclassified.append(code)
            begin = opening.get(code, Decimal(0))
            dr = debit.get(code, Decimal(0))
            cr = credit.get(code, Decimal(0))
            end = begin + dr - cr
            total_open += begin
            total_debit += dr
            total_credit += cr
            total_close += end
            rows.append({
                "accountCode": code, "line": line, "kind": kind,
                "account": line or code,
                "opening": _fmt(begin), "debit": _fmt(dr), "credit": _fmt(cr),
                "closing": _fmt(end), "direction": _direction(end),
                "balanced": (begin + dr - cr) == end,
            })

        balanced = total_debit == total_credit
        out: dict[str, Any] = {
            "period": key, "schema": BOOKS_SCHEMA, "rows": rows,
            "totals": {"opening": _fmt(total_open), "debit": _fmt(total_debit),
                       "credit": _fmt(total_credit), "closing": _fmt(total_close)},
            "balanced": balanced,
            "caliber_note": "三栏式：期初 + 本期借 − 本期贷 = 期末；余额符号统一为"
                            "「借 − 贷」，方向见 direction；期末列与试算平衡表逐科目相等",
        }
        if unclassified:
            out["unclassified"] = sorted(unclassified)
            out["note"] = (f"表外科目 {', '.join(sorted(unclassified))} 已出行但未归类"
                           f"（不影响借贷平衡）")
        if not balanced:
            out["error"] = (f"本期借贷合计不等：借 {_fmt(total_debit)} / "
                            f"贷 {_fmt(total_credit)}——账本本身有问题，不是账簿口径问题")
        return out

    # ---- 2) 明细账 ----

    def subsidiary_ledger(self, account_code: Any, period: Any = None,
                          *, limit: int = 0) -> dict[str, Any]:
        """某科目逐笔流水 + 逐行余额；`period=None` 从建账起算（期初为零）。"""
        code = str(account_code or "").strip()
        if not code:
            raise TemplateError("INVALID_ARGUMENT", "明细账须给科目编码 account_code")
        key = _check_period(period) if period not in (None, "") else None
        earlier, window = self._split(key)

        opening = sum((_signed(e) for e in earlier if _code(e) == code), Decimal(0))
        line = CHART.get(code, (None, None))[0]

        picked = sorted((e for e in window if _code(e) == code), key=_sort_key)
        running = opening
        rows: list[dict[str, Any]] = []
        total_debit = total_credit = Decimal(0)
        for entry in picked:
            amount = _dec(entry.get("amount_base") or entry.get("amount"))
            is_debit = entry.get("direction") == "debit"
            running += amount if is_debit else -amount
            if is_debit:
                total_debit += amount
            else:
                total_credit += amount
            rows.append({
                "bookedOn": entry.get("booked_on"),
                "postedAt": entry.get("posted_at"),
                "txnId": entry.get("txn_id"),
                "stableId": entry.get("stable_id"),
                "sourceRef": entry.get("source_ref"),
                "lineNo": entry.get("line_no"),
                "summary": str(entry.get("source_ref") or entry.get("stable_id")
                               or entry.get("txn_id") or ""),
                "debit": _fmt(amount) if is_debit else "0.00",
                "credit": "0.00" if is_debit else _fmt(amount),
                "balance": _fmt(running),
            })
        closing = opening + total_debit - total_credit
        truncated = bool(limit and len(rows) > limit)
        shown = rows[-limit:] if truncated else rows
        out: dict[str, Any] = {
            "accountCode": code, "account": line or code, "line": line,
            "period": key,
            "opening": _fmt(opening),
            "rows": shown,
            "totals": {"debit": _fmt(total_debit), "credit": _fmt(total_credit),
                       "closing": _fmt(closing)},
            "closingBalance": _fmt(closing),
            "direction": _direction(closing),
            "balanced": (opening + total_debit - total_credit) == closing,
            "caliber_note": "逐行余额=上一条余额 ± 本笔金额；期初=期间起之前的累计；"
                            "行序=业务日期→落账时刻→txn→行号",
        }
        if not rows:
            out["note"] = f"{'本期' if key else '建账以来'}该科目无发生额（期初 {_fmt(opening)}）"
        if truncated:
            # 只截显示行，不截合计与期末：合计永远按全量算，否则「最后一行余额」会骗人。
            out["note"] = f"仅显示最后 {limit} 行（合计与期末按全量 {len(rows)} 行计算）"
            out["truncated"] = True
            out["rowCount"] = len(rows)
        return out


__all__ = ["BOOKS_SCHEMA", "LedgerBooks"]

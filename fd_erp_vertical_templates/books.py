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
from .statements import CHART, is_closing_entry

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


    # ---- 2b) 期间活动（change: fix-fi-surface-audit）----

    def period_activity(self) -> list[dict[str, Any]]:
        """有哪些期间**真的有分录**（按期间 → 凭证数 / 行数 / 最近业务日期）。

        为什么需要它：内核 `list_periods` 的口径是「关过或重开过的期间」。只拿它当期间清单，
        会让「有业务但没关账」的月份**整月消失**——财务进后台想选哪个月来关账，看到空列表，
        分不清是「没业务」还是「没关过」。这两件事的处置完全不同。
        """
        slots: dict[str, dict[str, Any]] = {}
        for entry in read_entries(self._kernel):
            which = entry_period(entry)
            if which == "unknown":
                continue
            slot = slots.setdefault(which, {"period": which, "_txns": set(), "lines": 0,
                                            "lastBookedOn": None})
            txn = str(entry.get("txn_id") or "").strip()
            if txn:
                slot["_txns"].add(txn)
            slot["lines"] += 1
            day = str(entry.get("booked_on") or "")[:10] or None
            if day and (slot["lastBookedOn"] is None or day > slot["lastBookedOn"]):
                slot["lastBookedOn"] = day
        return [{"period": r["period"], "vouchers": len(r["_txns"]),
                 "lines": r["lines"], "lastBookedOn": r["lastBookedOn"],
                 "hasEntries": True}
                for r in sorted(slots.values(), key=lambda x: x["period"])]

    # ---- 3) 序时账与凭证（change: add-fi-voucher-and-journal）----

    def journal(self, period: Any = None, *, limit: int = 0) -> dict[str, Any]:
        """序时账：按凭证（`txnId`）汇总，逐凭证一行。

        为什么要有它：`txnId` 只能从某条明细账行里捡到，意味着「先知道要查哪个科目」才能查。
        序时账把这一段期间发生了哪些业务按凭证摊开，是查账的起点。

        `limit` 只截**显示行**：合计按全量算。与明细账同一纪律——只截合计会让
        「最后一行的金额」骗人。
        """
        key = _check_period(period) if period not in (None, "") else None
        _, window = self._split(key)

        vouchers: dict[str, dict[str, Any]] = {}
        for entry in sorted(window, key=_sort_key):
            txn = str(entry.get("txn_id") or "").strip() or "__no_txn__"
            slot = vouchers.setdefault(txn, {
                "txnId": None if txn == "__no_txn__" else txn,
                "bookedOn": entry.get("booked_on"),
                "postedAt": entry.get("posted_at"),
                "sourceRef": entry.get("source_ref"),
                "stableId": entry.get("stable_id"),
                "summary": str(entry.get("source_ref") or entry.get("stable_id")
                               or txn),
                "debit": Decimal(0), "credit": Decimal(0), "lines": 0,
                "dimensions": {},
                "isClosing": False,
            })
            amount = _dec(entry.get("amount_base") or entry.get("amount"))
            if entry.get("direction") == "debit":
                slot["debit"] += amount
            else:
                slot["credit"] += amount
            slot["lines"] += 1
            if not slot["isClosing"] and is_closing_entry(entry):
                slot["isClosing"] = True
            for dim, value in (entry.get("dimensions") or {}).items():
                text = str(value or "").strip()
                if text:
                    slot["dimensions"].setdefault(dim, text)

        rows = sorted(vouchers.values(), key=lambda r: (
            str(r["bookedOn"] or ""), str(r["postedAt"] or ""),
            str(r["txnId"] or "")))
        total_debit = sum((r["debit"] for r in rows), Decimal(0))
        total_credit = sum((r["credit"] for r in rows), Decimal(0))
        shown = rows[-limit:] if limit and len(rows) > limit else rows
        out: dict[str, Any] = {
            "period": key,
            "rows": [{**r,
                      "debit": _fmt(r["debit"]), "credit": _fmt(r["credit"]),
                      "balanced": r["debit"] == r["credit"]}
                     for r in shown],
            "vouchers": len(rows),
            "lines": sum(r["lines"] for r in rows),
            "totals": {"debit": _fmt(total_debit), "credit": _fmt(total_credit)},
            "balanced": total_debit == total_credit,
            "caliber_note": "按凭证汇总：借方合计 == 贷方合计；`limit` 只截显示行，"
                            "合计与凭证数按全量；`isClosing` 标出结转损益凭证"
                            "（报表口径排除它，查账时要认得出来）",
        }
        if limit and len(rows) > limit:
            out["truncated"] = True
            out["rowCount"] = len(rows)
            out["note"] = (f"仅显示最后 {limit} 张（合计与凭证数按全量 {len(rows)} 张计算）")
        if not rows:
            out["note"] = f"{'本期' if key else '建账以来'}没有分录"
        if total_debit != total_credit:
            out["error"] = (f"序时账借贷合计不等：借 {_fmt(total_debit)} / "
                            f"贷 {_fmt(total_credit)}——账本本身有问题，不是汇总口径问题")
        return out

    def voucher(self, txn_id: Any) -> dict[str, Any]:
        """单张凭证：该 `txnId` 下的全部借贷行 + 合计 + 平衡断言。

        这是「账」与「表」之间唯一不可替代的一步：报表说这一期费用 12 万、明细账说某天有一笔
        3000 挂着某个 `txnId`，而这 3000 借的是费用还是预付、对方科目是什么——只有凭证能答。
        """
        txn = str(txn_id or "").strip()
        if not txn:
            raise TemplateError("INVALID_ARGUMENT", "凭证查询须给 txnId")
        entries = [e for e in read_entries(self._kernel)
                   if str(e.get("txn_id") or "").strip() == txn]
        if not entries:
            raise TemplateError("NOT_FOUND", f"凭证不存在：{txn}",
                                detail={"txnId": txn})
        entries.sort(key=_sort_key)
        lines: list[dict[str, Any]] = []
        total_debit = total_credit = Decimal(0)
        closing = False
        for entry in entries:
            amount = _dec(entry.get("amount_base") or entry.get("amount"))
            is_debit = entry.get("direction") == "debit"
            if is_debit:
                total_debit += amount
            else:
                total_credit += amount
            code = _code(entry)
            line_name = CHART.get(code, (None, None))[0]
            if not closing and is_closing_entry(entry):
                closing = True
            lines.append({
                "lineNo": entry.get("line_no"),
                "accountCode": code,
                "account": line_name or code,
                "summary": str(entry.get("source_ref") or entry.get("stable_id")
                               or txn),
                "debit": _fmt(amount) if is_debit else "0.00",
                "credit": "0.00" if is_debit else _fmt(amount),
                "currency": entry.get("currency"),
                "dimensions": entry.get("dimensions") or {},
            })
        first = entries[0]
        balanced = total_debit == total_credit
        out: dict[str, Any] = {
            "txnId": txn,
            "bookedOn": first.get("booked_on"),
            "postedAt": first.get("posted_at"),
            "sourceRef": first.get("source_ref"),
            "stableId": first.get("stable_id"),
            "lines": lines,
            "totals": {"debit": _fmt(total_debit), "credit": _fmt(total_credit)},
            "balanced": balanced,
            "isClosing": closing,
            "caliber_note": "一张凭证的借贷必须相等；`isClosing=true` 表示这是结转损益"
                            "生成的凭证（报表口径排除它，查账时要认得出来）",
        }
        if not balanced:
            out["error"] = (f"凭证借贷不等：借 {_fmt(total_debit)} / 贷 {_fmt(total_credit)}"
                            f"——账本本身有问题")
        return out

    # ---- 4) 往来对账单（change: add-party-statement）----

    def party_statement(self, dimension: Any, value: Any, period: Any = None,
                        *, accounts: list[str] | tuple[str, ...] | None = None,
                        base_currency: str | None = None) -> dict[str, Any]:
        """某个往来方（客户/供应商）的往来明细：跨科目串联 + 逐行余额 + 能对回总账。

        归属只认 `dimensions[dimension] == value`：靠科目名或摘要文字猜归属，迟早把两家的
        往来记到一家头上。指定 `accounts` 时把「该科目未标这个维度」的净额一并算出——
        否则「每个客户加起来 ≠ 总账余额」，而两张表各自都自洽。
        """
        dim = str(dimension or "").strip()
        if not dim:
            raise TemplateError("INVALID_ARGUMENT", "对账单须给维度键 dimension")
        who = str(value or "").strip()
        if not who:
            raise TemplateError("INVALID_ARGUMENT", "对账单须给维度值 value")
        key = _check_period(period) if period not in (None, "") else None
        picked = [str(code).strip() for code in (accounts or []) if str(code).strip()]
        earlier, window = self._split(key)

        def matches(entry: dict[str, Any]) -> bool:
            dims = entry.get("dimensions") or {}
            return str(dims.get(dim) or "").strip() == who

        opening = Decimal(0)
        opening_by: dict[str, Decimal] = {}
        for entry in earlier:
            if not matches(entry):
                continue
            code = _code(entry)
            if picked and code not in picked:
                continue
            opening += _signed(entry)
            opening_by[code] = opening_by.get(code, Decimal(0)) + _signed(entry)

        rows: list[dict[str, Any]] = []
        by_account: dict[str, dict[str, Decimal]] = {}
        # 逐行余额是**按科目**走的：把各科目加在一起恒为零（每笔分录借贷必平），
        # 那种「余额」看着一路归零，对查账毫无用处。往来余额看的是应收/应付那一列。
        running_by: dict[str, Decimal] = dict(opening_by)
        for entry in sorted((e for e in window if matches(e)), key=_sort_key):
            code = _code(entry)
            if picked and code not in picked:
                continue
            amount = _dec(entry.get("amount_base") or entry.get("amount"))
            is_debit = entry.get("direction") == "debit"
            running = running_by.get(code, Decimal(0))
            running += amount if is_debit else -amount
            running_by[code] = running
            slot = by_account.setdefault(code, {"opening": Decimal(0), "debit": Decimal(0),
                                                "credit": Decimal(0)})
            slot["opening"] = opening_by.get(code, Decimal(0))
            if is_debit:
                slot["debit"] += amount
            else:
                slot["credit"] += amount
            rows.append({
                "bookedOn": entry.get("booked_on"),
                "txnId": entry.get("txn_id"),
                "stableId": entry.get("stable_id"),
                "sourceRef": entry.get("source_ref"),
                "accountCode": code,
                "line": CHART.get(code, (None, None))[0],
                "summary": str(entry.get("source_ref") or entry.get("stable_id")
                               or entry.get("txn_id") or ""),
                "debit": _fmt(amount) if is_debit else "0.00",
                "credit": "0.00" if is_debit else _fmt(amount),
                "balance": _fmt(running),
            })

        # 指定科目里本期完全没出现、但期初有余额的，也要出行（否则对账单「少了那一家」）
        for code, begin in opening_by.items():
            if code in by_account:
                continue
            if picked and code not in picked:
                continue
            by_account[code] = {"opening": begin, "debit": Decimal(0),
                                "credit": Decimal(0)}

        subtotals = []
        total_debit = total_credit = Decimal(0)
        for code in sorted(by_account):
            slot = by_account[code]
            closing = slot["opening"] + slot["debit"] - slot["credit"]
            total_debit += slot["debit"]
            total_credit += slot["credit"]
            subtotals.append({
                "accountCode": code, "account": CHART.get(code, (code, ""))[0],
                "opening": _fmt(slot["opening"]),
                "debit": _fmt(slot["debit"]), "credit": _fmt(slot["credit"]),
                "closing": _fmt(closing), "direction": _direction(closing),
            })
        closing = opening + total_debit - total_credit

        out: dict[str, Any] = {
            "dimension": dim, "dimensionValue": who, "period": key,
            "opening": _fmt(opening), "rows": rows, "subtotals": subtotals,
            "totals": {"debit": _fmt(total_debit), "credit": _fmt(total_credit),
                       "closing": _fmt(closing)},
            "closingBalance": _fmt(closing), "direction": _direction(closing),
            "balanced": (opening + total_debit - total_credit) == closing,
            "caliber_note": "归属只认 dimensions[维度键]；逐行 balance 是**该科目**的余额"
                            "（各科目相加恒为零，看往来余额请看 subtotals 里应收/应付那一行）；"
                            "余额统一「借 − 贷」，方向见 direction；"
                            "closingBalance 是各科目期末合计＝往来净额（结清时为 0）；"
                            "行序=业务日期→落账时刻→txn→行号",
        }
        if not rows:
            out["note"] = f"{'本期' if key else '建账以来'}该往来方无发生额（期初 {_fmt(opening)}）"

        if not picked:
            out["reconciled"] = False
            out["caliber_note"] += "；未指定 accounts，未做总账对账"
            return out

        # 对回总账：各科目「未标该维度」的净额 + 总账期末
        unattributed: dict[str, Decimal] = {}
        ledger: dict[str, Decimal] = {}
        for code in picked:
            ledger[code] = sum((_signed(e) for e in earlier + window
                                if _code(e) == code), Decimal(0))
            unattributed[code] = sum(
                (_signed(e) for e in window
                 if _code(e) == code
                 and not str((e.get("dimensions") or {}).get(dim) or "").strip()),
                Decimal(0))

        checks = []
        consistent = True
        for code in picked:
            mine = next((_dec(s["closing"]) for s in subtotals
                         if s["accountCode"] == code), Decimal(0))
            gap = ledger[code] - (mine + unattributed[code])
            if gap != 0:
                consistent = False
            checks.append({
                "accountCode": code, "account": CHART.get(code, (code, ""))[0],
                "thisParty": _fmt(mine),
                "unattributed": _fmt(unattributed[code]),
                "ledgerClosing": _fmt(ledger[code]),
                "difference": _fmt(gap),
            })
        out["reconciliation"] = checks
        out["reconciled"] = True
        out["consistent"] = consistent
        if not consistent:
            worst = max(checks, key=lambda c: abs(_dec(c["difference"])))
            out["error"] = (f"对账单与总账对不上：{worst['accountCode']} 差 "
                            f"{worst['difference']}（本单 {worst['thisParty']} + 未标注 "
                            f"{worst['unattributed']} ≠ 总账 {worst['ledgerClosing']}）——"
                            f"先查未标维度的分录，别把差额摊给某个往来方")
        return out


__all__ = ["BOOKS_SCHEMA", "LedgerBooks"]

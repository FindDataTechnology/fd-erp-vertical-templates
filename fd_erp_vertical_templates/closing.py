"""期末损益结转（change: add-profit-closing）。

所有报表都从分录现算，但分录里**损益类科目从不结平**：`4000/5000/5100/5200/6602` 的余额
从开业累积到现在，`3100 留存收益` 永远是 0，资产负债表上的「未结转损益」一年比一年大。
跨年时两年利润混在同一行——看报表分不出哪年是哪年。

结转就是把这期损益搬进权益：逐科目按净额**反方向**结平（收入记借、成本费用记贷），
借贷差额计入 `3100`（盈利记贷、亏损记借）。和账龄、减值、折旧一样，它只**出待确认提案**，
不代任何人过门。

## 结转分录必须可识别

它的作用是把损益搬进权益，所以从损益口径里必须排除（`statements` 按 `source_ref` 前缀
`close:` 过滤）：不排除的话，已结转期间的利润表会显示收入 0、费用 0——那不是「这期没赚钱」，
而是账已经做完了。试算平衡表与资产负债表**照样包含**它，`unclosedProfit` 因此只反映
尚未结转的期间。

## 稳定键就是幂等键

`close:<period>`。同期间重提不重复过账；被退回（rejected）后用 `close:<period>#2` 重提——
键由内核的提案表自己记着（`stable_id`），不需要额外的台账文件：这一条车道的全部事实
都在账本与提案里，没有第二份状态。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import entry_period, period_end
from .errors import TemplateError
from .kernel_rows import read_entries, read_proposals
from .statements import (CHART, CLOSING_PREFIX, EXPENSE_ACCOUNTS,
                         PROFIT_LOSS_ACCOUNTS, REVENUE_ACCOUNTS, is_closing_entry)

Q2 = Decimal("0.01")
#: 期末转入的权益科目（留存收益）。盈利记贷、亏损记借。
RETAINED_ACCOUNT = "3100"
#: 科目名从 `CHART` 派生：另写一份表就会在加科目时漏——0019 加的
#: 5301/5403/5711/5801 就漏过，预览里显示成裸科目码。
ACCOUNT_NAMES = {code: CHART[code][0]
                 for code in (*PROFIT_LOSS_ACCOUNTS, RETAINED_ACCOUNT)}

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


class PeriodClosing:
    """期末损益结转：预览 → 提交待确认结转提案 → 查状态。"""

    def __init__(self, tenant_id: str, kernel: Any, *, periods: Any = None,
                 base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        #: 关账台账（内核 periods 绑定）。注入了就拒绝结转已关期间；未注入只能靠 DB 触发器。
        self._periods = periods
        self._base = base_currency

    # ---- 取数 ----

    def _entries(self) -> list[dict[str, Any]]:
        return read_entries(self._kernel)

    def _proposals(self) -> list[dict[str, Any]]:
        """全量提案（分页）。只取第一页会让「结转已确认」在第 100 条之后被读成未结转。"""
        return read_proposals(self._kernel)

    def _attempts(self, period: str) -> list[dict[str, Any]]:
        head = f"{CLOSING_PREFIX}{period}"
        out = []
        for row in self._proposals():
            key = str(row.get("stable_id") or "")
            if key == head or key.startswith(head + "#"):
                out.append(row)
        return out

    def _period_sums(self, period: str) -> dict[str, Decimal]:
        """本期损益类科目净额（借 − 贷），**排除结转分录本身**：结转不能成为下一次结转的基数。"""
        sums = {code: Decimal(0) for code in PROFIT_LOSS_ACCOUNTS}
        for entry in self._entries():
            if is_closing_entry(entry) or entry_period(entry) != period:
                continue
            code = str(entry.get("account_code") or "").strip()
            if code not in sums:
                continue
            amount = _dec(entry.get("amount_base") or entry.get("amount"))
            sums[code] += amount if entry.get("direction") == "debit" else -amount
        return sums

    @staticmethod
    def _plan(sums: dict[str, Decimal]) -> tuple[list[dict[str, Any]], Decimal]:
        """把净额翻成结平分录行；返回 (损益结平行, 净损益)。

        收入类在贷方（净额为负），结平记借；成本费用在借方（净额为正），结平记贷。
        金额一律取绝对值——「反方向同额」才是结平，不是再记一笔负的。
        """
        lines: list[dict[str, Any]] = []
        profit = Decimal(0)
        for code in PROFIT_LOSS_ACCOUNTS:
            net = sums.get(code, Decimal(0))
            if net == 0:
                continue
            direction = "credit" if net > 0 else "debit"
            lines.append({"account_code": code, "direction": direction,
                          "amount": _fmt(abs(net))})
            # 净损益 = −Σ(损益类科目净额)：收入贷方（net 为负）贡献正利润，
            # 费用借方（net 为正）贡献负利润。两者都是取反——别按科目类型分叉。
            profit -= net
        return lines, profit

    def _closed_check(self, period: str) -> dict[str, Any] | None:
        if self._periods is None:
            return None
        try:
            st = self._periods.status(period) or {}
        except Exception:  # noqa: BLE001 查不到就不假装已关，交给 DB 触发器兜底
            return None
        return st if st.get("closed") else None

    # ---- 预览（只读） ----

    def preview(self, period: Any) -> dict[str, Any]:
        """本期要结什么、结多少、能不能结。不产生提案，不改账本。"""
        key = _check_period(period)
        sums = self._period_sums(key)
        lines, profit = self._plan(sums)
        status = self.status(key)
        balances = [{"accountCode": code,
                     "account": ACCOUNT_NAMES.get(code, code),
                     "net": _fmt(sums[code]),
                     "closingDirection": ("credit" if sums[code] > 0 else
                                          "debit" if sums[code] < 0 else None),
                     "closingAmount": _fmt(abs(sums[code]))}
                    for code in PROFIT_LOSS_ACCOUNTS]
        return {
            "period": key, "balances": balances,
            "closingLines": [dict(line, account=ACCOUNT_NAMES.get(line["account_code"]))
                             for line in lines],
            "netProfit": _fmt(profit),
            "retainedDirection": "credit" if profit > 0 else
                                 "debit" if profit < 0 else None,
            "retainedAmount": _fmt(abs(profit)),
            "bookedOn": period_end(key),
            "status": status,
            "caliber_note": "结平金额取本期净额绝对值；差额入 3100（盈利贷、亏损借）；"
                            "结转分录带 close: 前缀，不进利润表口径",
        }

    # ---- 结转（出待确认提案） ----

    def transfer(self, period: Any, *, submitted_by: str = "period-closing") -> dict[str, Any]:
        """提交一张结转 proposal（不代确认）；同期间幂等。"""
        key = _check_period(period)
        status = self.status(key)
        if status["state"] == "posted":
            return {"period": key, "noop": True, "posted": True, "duplicate": True,
                    "proposal": status.get("proposal"), "status": status,
                    "caliber_note": "本期已结转，重复调用不重复过账"}
        if status["state"] == "pending":
            return {"period": key, "noop": True, "posted": False, "duplicate": True,
                    "proposal": status.get("proposal"), "status": status,
                    "caliber_note": "本期已有待确认的结转提案：先过门或作废，不重复提交"}

        closed = self._closed_check(key)
        if closed is not None:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"{key} 已关账：结转分录不能穿进已关期间——先重开（带理由），"
                f"或在开放期间冲正",
                detail={"period": key, "closedAt": closed.get("closedAt")})

        sums = self._period_sums(key)
        lines, profit = self._plan(sums)
        if not lines:
            return {"period": key, "noop": True, "proposal": None, "status": status,
                    "netProfit": "0.00",
                    "caliber_note": "本期损益类科目净额全为零：不生成 0 元分录，也不占用稳定键"}

        debit = sum((_dec(line["amount"]) for line in lines
                     if line["direction"] == "debit"), Decimal(0))
        credit = sum((_dec(line["amount"]) for line in lines
                      if line["direction"] == "credit"), Decimal(0))
        diff = debit - credit
        if diff != 0:
            # 差额进留存收益：借方多了（盈利）就贷记 3100，借方少了就借记。
            lines.append({"account_code": RETAINED_ACCOUNT,
                          "direction": "credit" if diff > 0 else "debit",
                          "amount": _fmt(abs(diff))})
        lines = [dict(line, currency=self._base) for line in lines]

        attempts = self._attempts(key)
        # 内核同 stable_id 永远返回首次结果（哪怕被退回），退回后重提必须换键。
        stable_id = f"{CLOSING_PREFIX}{key}" if not attempts \
            else f"{CLOSING_PREFIX}{key}#{len(attempts) + 1}"
        proposal = self._kernel.submit_proposal(
            stable_id, lines, source_ref=f"{CLOSING_PREFIX}{key}", kind="journal",
            lane_title=f"{key} 结转损益", submitted_by=submitted_by,
            booked_on=period_end(key))
        return {
            "period": key, "noop": False, "proposal": proposal, "stableId": stable_id,
            "bookedOn": period_end(key), "lines": lines,
            "netProfit": _fmt(profit),
            "retainedEntry": {"accountCode": RETAINED_ACCOUNT,
                              "direction": "credit" if diff > 0 else "debit",
                              "amount": _fmt(abs(diff))},
            "status": self.status(key),
            "caliber_note": "结转只生成待确认 proposal；过门后由内核落账并保持借贷必平",
        }

    # ---- 状态 ----

    def status(self, period: Any) -> dict[str, Any]:
        """本期是否已结转：只认稳定键（不看金额——金额是结果，不是身份）。"""
        key = _check_period(period)
        rows = self._attempts(key)
        posted = [r for r in rows if str(r.get("status") or "") == "confirmed"]
        pending = [r for r in rows if str(r.get("status") or "") == "pending"]
        rejected = [r for r in rows if str(r.get("status") or "") == "rejected"]
        last = posted[-1] if posted else (pending[-1] if pending else
                                         (rejected[-1] if rejected else None))
        state = "posted" if posted else "pending" if pending else \
            "rejected" if rejected else "not_closed"
        return {"period": key, "state": state, "closed": bool(posted),
                "stableId": (last or {}).get("stable_id"),
                "proposal": last if last else None,
                "attempts": len(rows),
                "caliber_note": "状态只认稳定键 close:<period>；金额由所引提案自身记录"}


__all__ = ["CLOSING_PREFIX", "EXPENSE_ACCOUNTS", "PROFIT_LOSS_ACCOUNTS",
           "PeriodClosing", "RETAINED_ACCOUNT", "REVENUE_ACCOUNTS",
           "is_closing_entry", "period_end"]

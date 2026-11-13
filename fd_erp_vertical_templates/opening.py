"""建账期初余额（change: add-opening-balances）。

真实客户不会从零开始：他们带着应收、应付、存货、固定资产进系统。期初余额就是把那些余额
一次性搬进账本的**建账事实**。

三条规则都很硬，每一条都对准一种真实事故：

1. **借贷必平**——允许不平就一定会有人拿某个科目去凑，而凑出来的账本从此失去意义；
   所以不平就拒，差额如实报出来，绝不自动塞进留存收益。
2. **只此一次**——第二张期初单会把余额翻倍，而它自己是平的，报表也不会报错；
   所以稳定键 `opening:<tenant>` 幂等。
3. **只能在空账上录**——期初定义的是「账本开始之前」的余额；事后插入会把所有历史报表
   一起改写。真有需要（历史迁移）必须显式 `allow_with_entries=True` 加上理由，理由回显，
   受权偏离留痕而不是变成默认行为。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .dates import period_end
from .errors import TemplateError
from .kernel_rows import read_entries, read_proposals
from .statements import CHART

Q2 = Decimal("0.01")
OPENING_KEY = "opening:{tenant}"

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


class OpeningBalances:
    """建账期初余额单：借贷必平、只此一次、仅限空账。"""

    def __init__(self, tenant_id: str, kernel: Any, *, base_currency: str = "CNY",
                 today: Any = None):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency
        #: 建账日缺省值（可注入，便于测试与「按模板时区取今天」）。
        self._today = today

    # ---- 键与状态 ----

    @property
    def stable_id(self) -> str:
        return OPENING_KEY.format(tenant=self._tenant)

    def _proposals(self) -> list[dict[str, Any]]:
        return read_proposals(self._kernel)

    def _mine(self) -> list[dict[str, Any]]:
        head = self.stable_id
        return [r for r in self._proposals()
                if str(r.get("stable_id") or "") == head
                or str(r.get("stable_id") or "").startswith(head + "#")]

    def status(self) -> dict[str, Any]:
        rows = self._mine()
        posted = [r for r in rows if str(r.get("status") or "") == "confirmed"]
        pending = [r for r in rows if str(r.get("status") or "") == "pending"]
        last = (posted or pending or rows or [None])[-1]
        state = "posted" if posted else "pending" if pending else \
            "rejected" if rows else "not_opened"
        return {"tenant": self._tenant, "stableId": self.stable_id, "state": state,
                "opened": bool(posted), "proposal": last,
                "bookedOn": (last or {}).get("booked_on"),
                "attempts": len(rows),
                "caliber_note": "建账事实只认稳定键；金额由所引提案自身记录"}

    # ---- 预览 ----

    def preview(self) -> dict[str, Any]:
        """已落账期初单投影成「各科目期初余额」（借 − 贷）——账簿期初列必须与它相等。"""
        rows = self._mine()
        posted = [r for r in rows if str(r.get("status") or "") == "confirmed"]
        balances: dict[str, Decimal] = {}
        source: dict[str, Any] | None = None
        if posted:
            source = posted[-1]
            # 期初单的分录靠稳定键落账，这里直接读该 stable_id 的分录最容易核。
            for entry in read_entries(self._kernel):
                if str(entry.get("stable_id") or "") != str(source.get("stable_id")):
                    continue
                code = str(entry.get("account_code") or "").strip()
                if code:
                    balances[code] = balances.get(code, Decimal(0)) + _signed(entry)
        out_rows = [{"accountCode": code,
                     "account": (CHART.get(code, (code, None))[0] or code),
                     "line": CHART.get(code, (None, None))[0],
                     "balance": _fmt(balances[code])}
                    for code in sorted(balances)]
        return {"tenant": self._tenant, "status": self.status()["state"],
                "bookedOn": (source or {}).get("booked_on"),
                "rows": out_rows,
                "total": _fmt(sum(balances.values(), Decimal(0))),
                "caliber_note": "期初余额按「借 − 贷」；借贷必平 ⇒ 合计恒为零，"
                                "所以看的是逐科目分布而不是合计"}

    # ---- 登记 ----

    def register(self, lines: list[dict[str, Any]], *, as_of: Any = None,
                 submitted_by: str = "opening-balances",
                 allow_with_entries: bool = False,
                 reason: str | None = None) -> dict[str, Any]:
        """提交一张借贷必平的期初余额单（待确认 proposal）。"""
        prior = self.status()
        if prior["state"] in ("pending", "posted"):
            return {"tenant": self._tenant, "duplicate": True,
                    "proposal": prior["proposal"], "status": prior,
                    "caliber_note": "已建账：重复提交不重复过账（同一 stable_id 幂等）"}

        entries = read_entries(self._kernel)
        note = str(reason or "").strip()
        if entries and not allow_with_entries:
            first = min((str(e.get("booked_on") or e.get("posted_at") or "")
                         for e in entries if e.get("booked_on")
                         or e.get("posted_at")), default="")
            raise TemplateError(
                "INVALID_ARGUMENT",
                "账本已有分录：期初余额只能在空账上录（事后插入会改写全部历史报表）——"
                "先关账/冲正，或显式 allow_with_entries=True 并给出理由",
                detail={"entries": len(entries), "earliest": first,
                        "fix": "allow_with_entries=True + reason"})
        if entries and not note:
            raise TemplateError(
                "INVALID_ARGUMENT",
                "allow_with_entries 必须同时给出理由（受权偏离要留痕）",
                detail={"entries": len(entries)})

        normalized = self._normalize(lines)
        debit = sum((_dec(l["amount"]) for l in normalized if l["direction"] == "debit"),
                    Decimal(0))
        credit = sum((_dec(l["amount"]) for l in normalized if l["direction"] == "credit"),
                     Decimal(0))
        if debit != credit:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"期初余额借贷不平：借 {_fmt(debit)} / 贷 {_fmt(credit)} / "
                f"差额 {_fmt(debit - credit)}——不平就是有科目漏了或多记了，"
                f"不能拿某个科目去补差",
                detail={"debit": _fmt(debit), "credit": _fmt(credit),
                        "difference": _fmt(debit - credit)})

        day = self._resolve_day(as_of)
        attempts = self._mine()
        stable_id = self.stable_id if not attempts \
            else f"{self.stable_id}#{len(attempts) + 1}"
        proposal = self._kernel.submit_proposal(
            stable_id, normalized, source_ref="opening", kind="journal",
            lane_title=f"{self._tenant} 建账期初余额", submitted_by=submitted_by,
            booked_on=day)
        out: dict[str, Any] = {
            "tenant": self._tenant, "duplicate": False, "proposal": proposal,
            "stableId": stable_id, "bookedOn": day,
            "lines": normalized, "debit": _fmt(debit), "credit": _fmt(credit),
            "allowWithEntries": bool(entries and allow_with_entries),
            "status": self.status(),
            "caliber_note": "期初单只出待确认 proposal；过门后由内核落账，"
                            "账簿期初列与 preview 必须相等",
        }
        if entries and allow_with_entries:
            out["reason"] = note
        return out

    # ---- 校验 ----

    def _normalize(self, lines: Any) -> list[dict[str, Any]]:
        if not isinstance(lines, list) or not lines:
            raise TemplateError("INVALID_ARGUMENT", "期初余额单不能为空")
        seen: dict[str, str] = {}
        out: list[dict[str, Any]] = []
        for raw in lines:
            if not isinstance(raw, dict):
                raise TemplateError("INVALID_ARGUMENT", f"期初行格式非法：{raw!r}")
            code = str(raw.get("account_code") or raw.get("accountCode") or "").strip()
            if not code:
                raise TemplateError("INVALID_ARGUMENT", "期初行缺少科目编码")
            if code not in CHART:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"科目 {code} 不在册（通用科目封闭集）",
                    detail={"allowed": sorted(CHART), "account": code})
            direction = str(raw.get("direction") or "").strip()
            if direction not in ("debit", "credit"):
                raise TemplateError("INVALID_ARGUMENT",
                                    f"科目 {code} 方向须为 debit/credit：{direction!r}")
            if code in seen:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"科目 {code} 在期初单里出现了两次：同一科目只留一行净额"
                    f"（一正一负会被抵成零，看着平其实什么都没录）",
                    detail={"account": code})
            seen[code] = direction
            amount = _dec(raw.get("amount"))
            if amount <= 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"科目 {code} 期初金额须大于零：{raw.get('amount')!r}")
            out.append({"account_code": code, "direction": direction,
                        "amount": _fmt(amount), "currency": self._base})
        return out

    def _resolve_day(self, as_of: Any) -> str:
        raw = str(as_of or "").strip()
        if raw:
            if _DATE_RE.match(raw):
                return raw
            if _PERIOD_RE.match(raw):
                return period_end(raw)
            raise TemplateError("INVALID_ARGUMENT",
                                f"建账日须为 YYYY-MM-DD 或 YYYY-MM：{as_of!r}")
        if self._today is not None:
            value = self._today() if callable(self._today) else self._today
            return str(value)
        from datetime import date
        return date.today().isoformat()


__all__ = ["OPENING_KEY", "OpeningBalances"]

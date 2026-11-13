"""月结清单（change: add-month-end-checklist）。

月结是**一串有顺序、有前置条件**的动作：结转出库成本 → 计提折旧 → 计提坏账 →
结转损益 → 关账。
散着做的话，漏跑一步没人知道：漏折旧时账本一切正常（只是这期少了一笔费用），漏结转时
营业成本永远是 0——两个错在报表上都「看起来合理」，通常等到跨期才发现。

本模块把那套凭记忆的顺序变成代码，但**守住确认门**：

- 准备步骤只**生成待确认 proposal**，绝不代任何人确认；
- 某一步失败就停，不跳过（跳过会让「少了一笔费用」看起来像「这期本来没有」）；
- 关账只在「所有步骤（含结转损益）都成功 + 期间内没有任何待确认提案」时发生，
  且关账入口由调用方注入（模板层不实现关账，那是内核/API 的职责）。

## 每一步都是幂等的

每条车道各自按期间幂等（稳定键带期间）。所以「过门后再跑一次」不会重复计提，只会看到
「无需处理」——那正是关账那一刻该有的样子。
"""
from __future__ import annotations

import re
from typing import Any

from .errors import TemplateError

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


def _pending_in_period(kernel: Any, period: str) -> list[dict[str, Any]]:
    """期间内的待确认提案（按业务日期归期）。读不到就不假装「没有」。"""
    out = kernel.list_proposals(status="pending") or {}
    rows = out.get("proposals") or out.get("items") or out.get("data") or []
    pending: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        booked = str(row.get("booked_on") or "")
        if booked.startswith(period):
            pending.append(row)
    return pending


class MonthEndChecklist:
    """月结清单：前置检查 → 三步准备（只出待确认）→ 结转损益 → 无待办才关账。"""

    #: 准备步骤（顺序即依赖：成本影响费用，费用影响结转）。
    STEPS = ("cogs", "depreciation", "bad_debt")
    #: 第四步：结转损益。它的基数只认**已落账**分录，所以前序刚出的待确认提案没落账时
    #: 必须推迟——少结转一笔费用在报表上看起来完全正常，这才是最危险的形态。
    CLOSING_STEP = "closing"

    def __init__(self, tenant_id: str, kernel: Any, *, cost: Any = None,
                 assets: Any = None, bad_debts: Any = None,
                 closing: Any = None, closer: Any = None):
        self._tenant = tenant_id
        self._kernel = kernel
        self._cost = cost
        self._assets = assets
        self._bad_debts = bad_debts
        self._closing = closing
        #: 关账入口（内核 periods 的绑定）。模板层不实现关账，未注入就如实 NOT_WIRED。
        self._closer = closer

    # ---- 前置检查（只读） ----

    def precheck(self, period: Any) -> dict[str, Any]:
        """不产生任何提案，只回答「现在能不能开跑、还差什么」。"""
        key = _check_period(period)
        items: list[dict[str, Any]] = []

        # 1) 期间是否已关账
        if self._closer is None:
            items.append({"item": "period_status", "state": "warning",
                          "detail": "关账入口未接线（closer 未注入）：清单可准备但无法关账",
                          "fix": "由 API 层注入内核 periods 绑定"})
            closed = None
        else:
            st = self._closer.status(key)
            closed = bool(st.get("closed"))
            items.append({
                "item": "period_status",
                "state": "blocker" if closed else "ok",
                "detail": (f"{key} 已关账（{st.get('actor')}）" if closed else f"{key} 未关账"),
                "fix": "要补该期须先重开（带理由），或在开放期间做冲正" if closed else None,
            })

        # 2) 期间内待确认提案
        pending = _pending_in_period(self._kernel, key)
        items.append({
            "item": "pending_proposals",
            "state": "blocker" if pending else "ok",
            "detail": (f"期间内有 {len(pending)} 张待确认提案"
                       f"（{', '.join(str(p.get('stable_id')) for p in pending[:5])}）"
                       if pending else "期间内没有待确认提案"),
            "fix": "先过确认门（或作废）再跑月结；关账不会在有待办时发生" if pending else None,
        })

        # 3) 成本未知的出库（结转会被整期拒）
        if self._cost is not None:
            try:
                snap = self._cost.replay()
                grain = "monthly"
                uncosted = [r for r in (snap.get("issues") or [])
                            if str(r.get("occurredAt") or "").startswith(key)
                            and r.get("cost") is None]
            except Exception as exc:  # noqa: BLE001 读不到不是「没问题」，是「查不了」
                uncosted = []
                items.append({"item": "cost_coverage", "state": "warning",
                              "detail": f"成本重放失败，无法判断未计价出库：{exc}",
                              "fix": "先看成本车道日志；不要带着未知状态关账"})
            else:
                items.append({
                    "item": "cost_coverage",
                    "state": "blocker" if uncosted else "ok",
                    "detail": (f"有 {len(uncosted)} 笔出库成本未知（结转会整期拒）"
                               if uncosted else "本期出库成本均已计价"),
                    "fix": "补成本凭据（或按口径允许部分结转 partial=True 并自报跳过项）"
                           if uncosted else None,
                })

        # 4) 坏账分档费率是否登记（未登记不能计提）
        if self._bad_debts is not None:
            sched = self._bad_debts.schedule()
            items.append({
                "item": "bad_debt_schedule",
                "state": "ok" if sched.get("registered") else "blocker",
                "detail": ("坏账分档费率已登记" if sched.get("registered")
                           else "未登记坏账分档费率（计提会被拒）"),
                "fix": "人工登记费率（set_schedule）：这是会计估计，程序不得自设"
                       if not sched.get("registered") else None,
            })

        blockers = [i for i in items if i["state"] == "blocker"]
        warnings = [i for i in items if i["state"] == "warning"]
        return {"period": key, "items": items, "blockers": blockers,
                "warnings": warnings, "readyToRun": not blockers,
                "closed": closed}

    # ---- 三步准备 ----

    def _step(self, name: str, period: str, actor: str) -> dict[str, Any]:
        if name == "cogs":
            if self._cost is None:
                return {"step": name, "state": "not_wired",
                        "detail": "成本车道未装配"}
            out = self._cost.post_cogs(period, submitted_by=actor)
            return {"step": name, "state": "prepared", "result": out,
                    "proposal": out.get("proposal"), "amount": out.get("total")}
        if name == "depreciation":
            if self._assets is None:
                return {"step": name, "state": "not_wired",
                        "detail": "资产车道未装配"}
            out = self._assets.depreciate(period, submitted_by=actor)
            return {"step": name, "state": "prepared", "result": out,
                    "proposal": out.get("proposal"), "amount": out.get("total")}
        if name == "bad_debt":
            if self._bad_debts is None:
                return {"step": name, "state": "not_wired",
                        "detail": "减值车道未装配"}
            out = self._bad_debts.provision(period, submitted_by=actor)
            return {"step": name, "state": "noop" if out.get("noop") else "prepared",
                    "result": out, "proposal": out.get("proposal"),
                    "amount": out.get("delta")}
        if name == self.CLOSING_STEP:
            if self._closing is None:
                return {"step": name, "state": "not_wired",
                        "detail": "结转车道未装配"}
            out = self._closing.transfer(period, submitted_by=actor)
            return {"step": name, "state": "noop" if out.get("noop") else "prepared",
                    "result": out, "proposal": out.get("proposal"),
                    "amount": out.get("netProfit")}
        raise TemplateError("INVALID_ARGUMENT", f"未知月结步骤：{name}")

    def run(self, period: Any, *, actor: str = "month-end",
            close: bool = False) -> dict[str, Any]:
        """跑三步准备；全部成功且无待办时才（可选）关账。"""
        key = _check_period(period)
        pre = self.precheck(key)
        if pre["closed"]:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"{key} 已关账：要补该期须先重开（带理由），或在开放期间做冲正",
                detail={"period": key, "closed": True})
        blockers = [b for b in pre["blockers"] if b["item"] != "pending_proposals"]
        if blockers:
            raise TemplateError(
                "INVALID_ARGUMENT",
                "月结前置未通过：" + "；".join(f"{b['item']}（{b['fix']}）" for b in blockers),
                detail={"period": key, "blockers": blockers})
        pending_before = [b for b in pre["blockers"] if b["item"] == "pending_proposals"]
        if pending_before:
            # 还有没过的门：不重复准备，先让人过门（重复准备也只会幂等返回首次结果）
            return {"period": key, "steps": [], "closed": False,
                    "pending": _pending_in_period(self._kernel, key),
                    "nextActions": [pending_before[0]["fix"]],
                    "caliber_note": "期间内已有待办：先过确认门或作废，再重跑月结"}

        steps: list[dict[str, Any]] = []
        for name in self.STEPS:
            try:
                steps.append(self._step(name, key, actor))
            except TemplateError as exc:
                steps.append({"step": name, "state": "failed",
                              "error": {"code": exc.code, "message": exc.message,
                                        "detail": exc.detail}})
                break                     # 失败即停：跳过去会让报表少一笔而看着正常

        prep_pending = _pending_in_period(self._kernel, key)
        failed = [s for s in steps if s["state"] == "failed"]

        # 第四步：结转损益。基数只认已落账分录——前三步刚出的待确认提案还没落账时，
        # 现在结转就会少结转一笔费用（报表上看起来完全正常），所以宁可推迟。
        if not failed:
            if prep_pending:
                steps.append({"step": self.CLOSING_STEP, "state": "deferred",
                              "detail": "前序步骤刚生成待确认提案，结转须等它们落账后再算",
                              "blockedBy": sorted({str(p.get("stable_id") or "")
                                                   for p in prep_pending
                                                   if p.get("stable_id")})})
            else:
                try:
                    steps.append(self._step(self.CLOSING_STEP, key, actor))
                except TemplateError as exc:
                    steps.append({"step": self.CLOSING_STEP, "state": "failed",
                                  "error": {"code": exc.code, "message": exc.message,
                                            "detail": exc.detail}})
                    failed = [s for s in steps if s["state"] == "failed"]

        pending_after = _pending_in_period(self._kernel, key)
        next_actions: list[str] = []
        if failed:
            next_actions.append(f"修好 {failed[0]['step']} 后重跑（{failed[0]['error']['message']}）")
        deferred = [s for s in steps if s["state"] == "deferred"]
        if deferred:
            next_actions.append("人工过确认门后重跑：结转损益必须在费用类提案落账之后再算")
        if pending_after:
            next_actions.append(
                f"人工过确认门：{len(pending_after)} 张待确认提案全部落账后才能关账")

        closed = False
        close_error: dict[str, Any] | None = None
        if close and not failed and not pending_after:
            if self._closer is None:
                close_error = {"code": "NOT_WIRED",
                               "message": "关账入口未接线（closer 未注入）：清单已准备完，但无法关账"}
            else:
                try:
                    self._closer.close(key, actor=actor, note="月结清单")
                    closed = True
                except TemplateError as exc:
                    close_error = {"code": exc.code, "message": exc.message,
                                   "detail": exc.detail}
                    next_actions.append(f"关账失败：{exc.message}")

        return {
            "period": key,
            "steps": steps,
            "pending": pending_after,
            "closed": closed,
            "closeError": close_error,
            "blocked": bool(failed) or bool(pending_after) or close_error is not None,
            "nextActions": next_actions,
            "caliber_note": "各步只生成待确认 proposal，不代任何人过门；结转损益在"
                            "前序提案落账后才算；关账前置=期间内没有任何待确认提案",
        }

    # ---- 一览 ----

    def checklist(self, period: Any) -> dict[str, Any]:
        """一次给出：已准备什么 / 还差什么 / 下一步由谁做。"""
        key = _check_period(period)
        pre = self.precheck(key)
        pending = _pending_in_period(self._kernel, key)
        who = []
        if pending:
            who.append("decider：过确认门（确认或作废待确认提案）")
        for b in pre["blockers"]:
            if b["item"] == "pending_proposals":
                continue
            who.append(f"ops：{b['fix']}")
        if not who:
            who.append("decider：确认无误后执行关账（带理由）" if pre["closed"] is False
                       else "无：本期已关账")
        return {"period": key, "precheck": pre, "pending": pending,
                "nextOwners": who,
                "caliber_note": "清单只读；准备与关账分别走 run(period) 与 run(period, close=True)"}


__all__ = ["MonthEndChecklist"]

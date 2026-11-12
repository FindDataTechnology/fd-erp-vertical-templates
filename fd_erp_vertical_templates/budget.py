"""预算控制车道（change: add-budget-control-lane 任务 1.x）。

预算不是一张表，是**「钱的计划」与「钱的占用」的比对**，外加一句「这笔还能不能花」。

## 为什么承诺由本层记、实际从内核读

内核分录只有科目、**没有维度**，所以「按部门的实际」无法从分录里拆出来。硬给账本核心加维度列
（proposals / journal_entries / 过账 / 余额与分录查询 / 契约表 / 工具面）是为了一个还没有真实
客户的引擎去动地基，风险与收益不成比例（design D1）。

于是本车道把两件事分开署名：

- **承诺**（这一笔已经占了多少钱）：本层 append-only 事件档记录，带维度与单据号；
- **实际**（这一科目这一期真花了多少）：内核已落账分录按期间只读汇总，**只到科目层**。

查询里两者分开列，并明说实际不区分维度——不拿承诺冒充实际，也不假装能按部门拆实际。

## 口径是唯一出处

- `budget_dimension`：维度键（`department` / `project` / `channel` / `cost-center`）。
  口径说部门，承诺就必须带部门；带别的维度键即拒——错维度比对等于比错对象（design D2）。
- `budget_period`：期间粒度（`monthly` → `YYYY-MM`，`quarterly` → `YYYY-Qn`）。登记与承诺
  用同一个「业务日期 → 期间键」函数，不会出现按季登记、按月扣减（design D3）。
- `budget_control`：超预算时的行为。`hard-block` 拒且**不写承诺**（写了再拒会让额度账不与实际
  一致）；`warn-only` 写并标超支；`post-audit` 写不标，超支只在查询里可见（design D4）。

## 纪律

- 预算登记与修订须人工拍板（程序不得自设）；修订追加式，旧记录留档（谁把额度从 8000 改成
  12000，本身是审计对象）。
- 承诺按 `ref`（单据号）幂等；释放按 `ref` 幂等且须带原因。
- 没登记预算不等于「预算充足」：没预算按 0 算，`hard-block` 下会被拒——那正是硬拦已知的代价。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog
from .policy import PolicyBook

Q6 = Decimal("0.000001")
BUDGET_SCHEMA = "fd-erp-vertical-templates/budget/v1"

#: 口径值 → 内核维度键。两套词表**不靠字符串巧合对齐**：口径册按 kebab-case 命名
#: （`cost-center`），内核维度键按 snake_case 且是封闭 schema（`cost_center`）。
#: 显式映射写在这里，错位就在这一处、看得见；如果直接当字符串传，实际值会永远为 0
#: 而报表上只显示「这期没花钱」——最容易被当成业务事实的假绿。
DIMENSION_KEY_MAP = {
    "department": "department",
    "project": "project",
    "channel": "channel",
    "cost-center": "cost_center",
}

CONTROL_MODES = ("hard-block", "warn-only", "post-audit")
GRAINS = ("monthly", "quarterly")
#: 程序身份：预算额度是「打算花多少」的政策数字，程序不得自设。
PROGRAM_ACTORS = ("system", "budget-engine", "asset-engine", "billing-engine",
                  "posting-engine")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_QUARTER_RE = re.compile(r"^\d{4}-Q[1-4]$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q6, rounding=ROUND_HALF_UP))


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: Any, what: str = "业务日期") -> date:
    raw = str(value or "").strip()
    if not raw:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise TemplateError("INVALID_ARGUMENT",
                            f"{what}不是合法日期：{value!r}（须 YYYY-MM-DD）") from exc


def period_key(when: date, grain: str) -> str:
    """业务日期 → 期间键（月 `YYYY-MM` / 季 `YYYY-Qn`）。"""
    if grain == "quarterly":
        return f"{when.year:04d}-Q{(when.month - 1) // 3 + 1}"
    return f"{when.year:04d}-{when.month:02d}"


def period_range(period: str, grain: str) -> tuple[date, date]:
    """期间键 → [起, 止) 日期区间（含头不含尾），供分录按期间汇总。"""
    if grain == "quarterly":
        year = int(period[:4])
        quarter = int(period[-1])
        start_month = (quarter - 1) * 3 + 1
        start = date(year, start_month, 1)
        end = date(year + 1, 1, 1) if quarter == 4 else date(year, start_month + 3, 1)
        return start, end
    year, month = int(period[:4]), int(period[5:7])
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def kernel_dimension_key(policy_key: str) -> str:
    """口径里的维度值 → 内核维度键（未登记即拒，不猜）。"""
    key = DIMENSION_KEY_MAP.get(str(policy_key or "").strip())
    if key is None:
        raise TemplateError(
            "INVALID_ARGUMENT",
            f"口径维度 {policy_key!r} 没有对应的内核维度键",
            detail={"allowed": list(DIMENSION_KEY_MAP),
                    "policy_key": policy_key})
    return key


def _check_period(period: Any, grain: str) -> str:
    key = str(period or "").strip()
    pattern, hint = ((_QUARTER_RE, "YYYY-Qn") if grain == "quarterly"
                     else (_MONTH_RE, "YYYY-MM"))
    if not pattern.match(key):
        raise TemplateError("INVALID_ARGUMENT",
                            f"期间格式须为 {hint}（口径 budget_period={grain}）：{period!r}",
                            detail={"period": period, "grain": grain, "expected": hint})
    return key


class BudgetBook:
    """预算登记 + 承诺占用 + 执行查询（口径唯一出处：PolicyBook）。"""

    def __init__(self, tenant_id: str, data_dir: str | Path,
                 kernel: Any, policy: PolicyBook):
        self._tenant = tenant_id
        self._kernel = kernel
        self._policy = policy
        self._log = EventLog(Path(data_dir) / f"budget-{tenant_id}.jsonl")

    # ---- 口径 ----

    def _cfg(self) -> dict[str, Any]:
        return self._policy.current()["values"]

    # ---- 读面 ----

    def _records(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self._log.events()
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def _budget_rows(self) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        """(维度键, 维度值, 科目, 期间) → 生效预算（同键后者覆盖，历史仍在档内）。"""
        out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for rec in self._records("budget"):
            out[(rec["dimension"], rec["dimensionValue"], rec["accountCode"],
                 rec["period"])] = rec
        return out

    def _commit_state(self) -> dict[str, dict[str, Any]]:
        """按档序折叠：释放只抵消它之前的承诺，释放后重提的照常生效。"""
        active: dict[str, dict[str, Any]] = {}
        released: dict[str, dict[str, Any]] = {}
        for rec in self._log.events():
            kind = rec.get("kind")
            if kind == "commit":
                active[rec["ref"]] = rec
            elif kind == "release":
                hit = active.pop(rec["ref"], None)
                if hit is not None:
                    released[rec["ref"]] = {**hit, **rec}
        return {"active": active, "released": released}

    # ---- 预算登记 ----

    def set_budget(self, dimension_value: str, account_code: str, period: Any,
                   amount: Any, *, currency: str = "CNY",
                   approved_by: str = "owner") -> dict[str, Any]:
        """登记/修订一条预算（人工拍板；同键修订追加式）。"""
        cfg = self._cfg()
        key = cfg["budget_dimension"]
        grain = cfg["budget_period"]
        actor = str(approved_by or "").strip()
        if not actor or actor in PROGRAM_ACTORS:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"预算额度须人工拍板：{actor or '（空）'} 不得自设预算",
                detail={"approved_by": actor, "program_actors": list(PROGRAM_ACTORS)})
        dim_value = str(dimension_value or "").strip()
        if not dim_value:
            raise TemplateError("INVALID_ARGUMENT",
                                f"维度值不能为空（口径 budget_dimension={key}）",
                                detail={"dimension": key})
        account = str(account_code or "").strip()
        if not account:
            raise TemplateError("INVALID_ARGUMENT", "预算科目不能为空")
        pkey = _check_period(period, grain)
        amount_v = _dec(amount, "预算金额")
        if amount_v < 0:
            raise TemplateError("INVALID_ARGUMENT", f"预算金额不能为负：{amount_v}")
        cur = str(currency or "").strip().upper() or "CNY"

        rec = {"kind": "budget", "dimension": key, "dimensionValue": dim_value,
               "accountCode": account, "period": pkey, "amount": _fmt(amount_v),
               "currency": cur, "approved_by": actor, "recorded_at": _now()}
        previous = self._budget_rows().get((key, dim_value, account, pkey))
        self._log.append(rec)
        return {"budget": {"dimension": key, "dimensionValue": dim_value,
                           "accountCode": account, "period": pkey,
                           "amount": _fmt(amount_v), "currency": cur,
                           "approvedBy": actor},
                "revised": previous is not None,
                "previousAmount": None if previous is None else previous["amount"],
                "grain": grain}

    def _budget(self, dimension_value: str, account_code: str,
                period: str) -> dict[str, Any] | None:
        cfg = self._cfg()
        return self._budget_rows().get(
            (cfg["budget_dimension"], dimension_value, account_code, period))

    def _committed(self, dimension_value: str, account_code: str,
                   period: str, *, exclude_ref: str | None = None) -> Decimal:
        cfg = self._cfg()
        total = Decimal(0)
        for ref, rec in self._commit_state()["active"].items():
            if ref == exclude_ref:
                continue
            if (rec["dimension"] == cfg["budget_dimension"]
                    and rec["dimensionValue"] == dimension_value
                    and rec["accountCode"] == account_code
                    and rec["period"] == period):
                total += _dec(rec["amount"], "承诺金额")
        return total

    # ---- 承诺占用 ----

    def commit(self, dimension_value: str, account_code: str, amount: Any, *,
               ref: str, occurred_on: Any, currency: str = "CNY",
               committed_by: str = "ops") -> dict[str, Any]:
        """占用预算：按口径 `budget_control` 决定超预算时拒/标/不标。"""
        cfg = self._cfg()
        key = cfg["budget_dimension"]
        grain = cfg["budget_period"]
        control = cfg["budget_control"]
        if control not in CONTROL_MODES:  # 册内封闭集，走到这里说明册坏了
            raise TemplateError("INVALID_ARGUMENT",
                                f"未知的预算控制方式：{control!r}",
                                detail={"allowed": list(CONTROL_MODES)})
        tag = str(ref or "").strip()
        if not tag:
            raise TemplateError("INVALID_ARGUMENT", "承诺须带单据号 ref（幂等键）")
        state = self._commit_state()
        if tag in state["active"]:
            first = state["active"][tag]
            # 幂等返回也要说清额度现在什么样：重复提交时调用方最关心的就是「还剩多少」。
            budget = self._budget(first["dimensionValue"], first["accountCode"],
                                  first["period"])
            budget_v = Decimal(0) if budget is None else _dec(budget["amount"], "预算金额")
            committed = self._committed(first["dimensionValue"], first["accountCode"],
                                        first["period"])
            return {"ref": tag, "duplicate": True, "period": first["period"],
                    "dimension": first["dimension"],
                    "dimensionValue": first["dimensionValue"],
                    "accountCode": first["accountCode"],
                    "amount": first["amount"], "overBudget": first.get("overBudget", False),
                    "budget": _fmt(budget_v), "committed": _fmt(committed),
                    "remaining": _fmt(budget_v - committed),
                    "note": "同一单据号已承诺过，返回首次结果（额度不重复占用）"}
        if tag in state["released"]:
            first = state["released"][tag]
            return {"ref": tag, "duplicate": True, "period": first["period"],
                    "dimension": first["dimension"],
                    "dimensionValue": first["dimensionValue"],
                    "accountCode": first["accountCode"],
                    "amount": first["amount"], "overBudget": False,
                    "note": "该单据号的承诺已释放；要重新占用请换一个单据号"}

        dim_value = str(dimension_value or "").strip()
        if not dim_value:
            raise TemplateError("INVALID_ARGUMENT",
                                f"维度值不能为空（口径 budget_dimension={key}）",
                                detail={"dimension": key})
        account = str(account_code or "").strip()
        if not account:
            raise TemplateError("INVALID_ARGUMENT", "承诺须给科目")
        amount_v = _dec(amount, "承诺金额")
        if amount_v <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"承诺金额必须大于 0：{amount_v}")
        when = _parse_date(occurred_on, "业务日期")
        pkey = _check_period(period_key(when, grain), grain)
        cur = str(currency or "").strip().upper() or "CNY"

        budget = self._budget(dim_value, account, pkey)
        budget_v = Decimal(0) if budget is None else _dec(budget["amount"], "预算金额")
        committed = self._committed(dim_value, account, pkey)
        remaining = budget_v - committed
        over = amount_v > remaining

        if over and control == "hard-block":
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"超出预算：{dim_value}/{account}/{pkey} 预算 {_fmt(budget_v)}、"
                f"已承诺 {_fmt(committed)}、剩余 {_fmt(remaining)}，本次 "
                f"{_fmt(amount_v)}——口径 budget_control=hard-block，拒绝",
                detail={"dimension": key, "dimensionValue": dim_value,
                        "accountCode": account, "period": pkey,
                        "budget": _fmt(budget_v), "committed": _fmt(committed),
                        "remaining": _fmt(remaining), "requested": _fmt(amount_v),
                        "control": control,
                        "budgetRegistered": budget is not None})

        rec = {"kind": "commit", "ref": tag, "dimension": key,
               "dimensionValue": dim_value, "accountCode": account,
               "period": pkey, "amount": _fmt(amount_v), "currency": cur,
               "occurredOn": when.isoformat(), "control": control,
               "overBudget": bool(over) and control == "warn-only",
               "overAmount": _fmt(amount_v - remaining) if over else "0",
               "committed_by": committed_by, "recorded_at": _now()}
        self._log.append(rec)
        out = {"ref": tag, "duplicate": False, "dimension": key,
               "dimensionValue": dim_value, "accountCode": account,
               "period": pkey, "amount": _fmt(amount_v), "currency": cur,
               "budget": _fmt(budget_v), "committed": _fmt(committed + amount_v),
               "remaining": _fmt(remaining - amount_v), "control": control,
               "overBudget": False,
               "budgetRegistered": budget is not None}
        if over and control == "warn-only":
            out["overBudget"] = True
            out["overAmount"] = _fmt(amount_v - remaining)
            out["warning"] = (f"超出预算 {out['overAmount']}——口径 warn-only，已放行并标记"
                              f"（剩余原为 {_fmt(remaining)}）")
        elif over:
            out["overBudget"] = False
            out["note"] = "口径 post-audit：不拦不标，超支只在执行查询里可见"
        return out

    def release(self, ref: str, *, reason: str, released_by: str = "owner") -> dict[str, Any]:
        """释放承诺（退回/作废时）；须带原因，同单据号幂等。"""
        tag = str(ref or "").strip()
        if not tag:
            raise TemplateError("INVALID_ARGUMENT", "释放须给单据号 ref")
        note = str(reason or "").strip()
        if not note:
            raise TemplateError("INVALID_ARGUMENT", "释放预算须说明原因（退回归属留档）")
        state = self._commit_state()
        if tag in state["released"]:
            return {"ref": tag, "duplicate": True,
                    "note": "该单据号已释放过"}
        hit = state["active"].get(tag)
        if hit is None:
            raise TemplateError("NOT_FOUND", f"没有该单据号的承诺：{tag}",
                                detail={"ref": tag})
        self._log.append({"kind": "release", "ref": tag, "reason": note,
                          "released_by": released_by, "recorded_at": _now()})
        return {"ref": tag, "duplicate": False, "reason": note,
                "period": hit["period"], "amount": hit["amount"],
                "dimensionValue": hit["dimensionValue"],
                "accountCode": hit["accountCode"]}

    # ---- 执行查询 ----

    def status(self, dimension_value: str, period: Any | None = None
               ) -> dict[str, Any]:
        """按维度值与期间看执行：预算 / 承诺 / 剩余 / 执行率 + 科目层实际。"""
        cfg = self._cfg()
        key = cfg["budget_dimension"]
        grain = cfg["budget_period"]
        pkey = (_check_period(period, grain) if period not in (None, "")
                else period_key(date.today(), grain))
        dim_value = str(dimension_value or "").strip()
        if not dim_value:
            raise TemplateError("INVALID_ARGUMENT", f"维度值不能为空（口径 {key}）")

        accounts: set[str] = set()
        for (dim, dv, account, per) in self._budget_rows():
            if dim == key and dv == dim_value and per == pkey:
                accounts.add(account)
        for rec in self._commit_state()["active"].values():
            if (rec["dimension"] == key and rec["dimensionValue"] == dim_value
                    and rec["period"] == pkey):
                accounts.add(rec["accountCode"])

        rows: list[dict[str, Any]] = []
        totals = {"budget": Decimal(0), "committed": Decimal(0),
                  "remaining": Decimal(0), "actual": Decimal(0),
                  "unattributedActual": Decimal(0)}
        for account in sorted(accounts):
            budget = self._budget(dim_value, account, pkey)
            budget_v = Decimal(0) if budget is None else _dec(budget["amount"], "预算金额")
            committed = self._committed(dim_value, account, pkey)
            remaining = budget_v - committed
            actual, count, unattributed = self._actual(
                account, pkey, grain,
                dimensions={kernel_dimension_key(key): dim_value})
            rows.append({
                "accountCode": account,
                "budget": _fmt(budget_v) if budget is not None else None,
                "committed": _fmt(committed),
                "remaining": _fmt(remaining),
                "utilization": ("0" if budget_v == 0
                                else str((committed / budget_v).quantize(
                                    Decimal("0.0001"), rounding=ROUND_HALF_UP))),
                "actual": _fmt(actual), "actualEntries": count,
                "unattributedActual": _fmt(unattributed),
                "overBudget": remaining < 0,
                "budgetRegistered": budget is not None,
            })
            totals["budget"] += budget_v
            totals["committed"] += committed
            totals["remaining"] += remaining
            totals["actual"] += actual
            totals["unattributedActual"] += unattributed
        return {
            "dimension": key, "kernelDimensionKey": kernel_dimension_key(key),
            "dimensionValue": dim_value, "period": pkey,
            "grain": grain, "control": cfg["budget_control"], "rows": rows,
            "totals": {k: _fmt(v) for k, v in totals.items()},
            "caliber_note": "承诺由本车道按维度记；实际来自内核已落账分录，**按同一维度汇总**"
                            "（内核分录自 add-ledger-dimensions 起带维度）。"
                            "`unattributedActual` 是同科目同期**未标注维度**的净额——"
                            "它不属于任何维度，别把它悄悄漏掉",
        }

    def _actual(self, account_code: str, period: str, grain: str,
                *, dimensions: dict[str, str] | None = None
                ) -> tuple[Decimal, int, Decimal]:
        """该科目该期间已落账净额（借 − 贷，按本位币），只读内核分录。

        给 `dimensions` 时按维度过滤（内核 jsonb 包含语义）；同时把**同科目同期未标注
        维度**的净额一并算出来——否则「这个部门花了多少」会把没标维度的部分静默漏掉，
        而漏掉的数字在报表上看起来只是「偏小」。
        """
        start, end = period_range(period, grain)
        total = Decimal(0)
        seen = 0
        unattributed = Decimal(0)
        offset = 0
        while True:
            out = self._kernel.query_entries(account_code=account_code,
                                             dimensions=dimensions,
                                             offset=offset, limit=200) or {}
            rows = [r for r in (out.get("entries") or out.get("data") or [])
                    if isinstance(r, dict)]
            if not rows:
                break
            for row in rows:
                when = _entry_date(row.get("posted_at"))
                if when is None or not (start <= when < end):
                    continue
                amount = _dec(row.get("amount_base") or row.get("amount"), "分录金额")
                signed = amount if row.get("direction") == "debit" else -amount
                total += signed
                seen += 1
            offset += len(rows)
            if len(rows) < 200:
                break
        if dimensions:
            unattributed = self._unattributed(account_code, start, end)
        return total, seen, unattributed

    def _unattributed(self, account_code: str, start: date, end: date) -> Decimal:
        """同科目同期「没有标注维度」的净额（独立一次读取，不与维度过滤混在一起）。"""
        total = Decimal(0)
        offset = 0
        while True:
            out = self._kernel.query_entries(account_code=account_code,
                                             offset=offset, limit=200) or {}
            rows = [r for r in (out.get("entries") or out.get("data") or [])
                    if isinstance(r, dict)]
            if not rows:
                break
            for row in rows:
                if row.get("dimensions"):
                    continue                     # 有维度的归它自己，不重复计
                when = _entry_date(row.get("posted_at"))
                if when is None or not (start <= when < end):
                    continue
                amount = _dec(row.get("amount_base") or row.get("amount"), "分录金额")
                total += amount if row.get("direction") == "debit" else -amount
            offset += len(rows)
            if len(rows) < 200:
                break
        return total

    def budgets(self, period: Any | None = None) -> list[dict[str, Any]]:
        """已登记的预算清单（生效版本）。"""
        cfg = self._cfg()
        grain = cfg["budget_period"]
        pkey = (_check_period(period, grain) if period not in (None, "")
                else None)
        rows = []
        for (dim, dv, account, per), rec in self._budget_rows().items():
            if pkey is not None and per != pkey:
                continue
            rows.append({"dimension": dim, "dimensionValue": dv,
                         "accountCode": account, "period": per,
                         "amount": rec["amount"], "currency": rec["currency"],
                         "approvedBy": rec.get("approved_by"),
                         "committed": _fmt(self._committed(dv, account, per))})
        rows.sort(key=lambda r: (r["period"], r["dimensionValue"], r["accountCode"]))
        return rows


def _entry_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


__all__ = ["BudgetBook", "CONTROL_MODES", "GRAINS", "period_key", "period_range",
           "BUDGET_SCHEMA"]

"""固定资产车道（change: add-fixed-assets-lane 任务 2.x）。

**不新增表**：资产台账与折旧记录都落在模板层事件档（append-only JSONL），账务事实仍只在
内核——期末计提生成**待确认的 proposal**（借费用 / 贷 1602 累计折旧），过门后才落账。

## 为什么计划是算出来的

折旧计划 = `(原值, 残值, 月数, 方法, 已计提期数, 产量)` 的纯函数结果（design D2）。
不建「每期折旧表」：那张表本来就能算出来，存一份只会引入「表与事实不一致」的第二份真相。

## 三条口径都在册，引擎都认

- `depreciation_method`：`straight-line` / `double-declining` / `units-of-production`，
  资产可单独指定方法（不指定就随口径）；
- `asset_ledger_owner`：`platform` 自建台账；`customer-readonly` / `customer-writeback`
  一律 `NOT_WIRED`——没有客户资产系统可接，不假装接了（design D6）；
- 资本化门槛：人工登记、程序不得自设；币种不同**不换汇**，直接拒绝（宁可停下也不猜）。

## 几条不许松的纪律

- 折旧**当月增加次月起计提**（常规做法）；期间键为 `YYYY-MM`。
- 任何方法都不许把净值压到残值以下；**末期精确收口**——逐期按分四舍五入的残渣全由末期吸收
  （design D7），否则 12 期累加会差几分钱，看得见但说不清。
- 工作量法缺当期产量**整期拒绝并点名资产**（design D5）：「这期没生产」与「没填产量」在账上
  长得一模一样，前者合法、后者是缺数据。
- 同期间重复计提**幂等**（`stable_id = depr:<期间>`，内核侧也幂等）。
- 已提交但被人退回的计提，用 `void(period, reason=...)`作废后重提；已过门落账的拒绝直接作废，
  要求先在内核冲正——不然台账会比账本少一笔折旧。
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

#: 科目金额口径：分。
Q2 = Decimal("0.01")

METHODS = ("straight-line", "double-declining", "units-of-production")
#: 折旧贷方科目固定为累计折旧。
ACCUMULATED = "1602"
#: 折旧借方可进的内核费用科目（行业归集如制造费用/销售费用走映射包，不进内核科目表）。
EXPENSE_ACCOUNTS = ("5000", "5200")
#: 程序身份：不得自设资本化门槛（门槛由人工登记）。
PROGRAM_ACTORS = ("system", "asset-engine", "billing-engine", "posting-engine")

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: Any) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise TemplateError("INVALID_ARGUMENT", "取得日期不能为空")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise TemplateError("INVALID_ARGUMENT",
                            f"取得日期不是合法日期：{value!r}（须 YYYY-MM-DD）") from exc


def _period_of(d: date, offset: int) -> str:
    """取得月 + offset 个月 → `YYYY-MM`（offset=1 即次月）。"""
    total = d.year * 12 + (d.month - 1) + offset
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _first_period(acquired_on: date) -> str:
    return _period_of(acquired_on, 1)


def _index_in(period: str, acquired_on: date) -> int | None:
    """期间 → 第几期（次月起算 1）；早于起提期返回 None。"""
    year, month = int(period[:4]), int(period[5:7])
    n = (year * 12 + month) - (acquired_on.year * 12 + acquired_on.month)
    return n if n >= 1 else None


def _check_period(period: Any) -> str:
    key = str(period or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT",
                            f"期间格式须为 YYYY-MM：{period!r}")
    return key


class FixedAssetLane:
    """固定资产台账 + 折旧车道（登记、计划、计提、净值）。"""

    def __init__(self, tenant_id: str, data_dir: str | Path,
                 kernel: Any, policy: PolicyBook):
        self._tenant = tenant_id
        self._kernel = kernel
        self._policy = policy
        self._log = EventLog(Path(data_dir) / f"assets-{tenant_id}.jsonl")

    # ---- 读面（档内记录）----

    def _records(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self._log.events()
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def threshold(self) -> dict[str, Any] | None:
        hits = self._records("threshold")
        if not hits:
            return None
        last = hits[-1]
        if last.get("amount") in (None, ""):
            return None  # 显式清除（不设门槛）
        return {"amount": str(last["amount"]), "currency": last["currency"],
                "registeredBy": last.get("registered_by"),
                "registeredAt": last.get("recorded_at")}

    def _assets(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for rec in self._records("asset"):
            out.setdefault(rec["assetId"], rec)  # 首次登记为真，重登不覆盖
        return out

    def _runs(self) -> list[dict[str, Any]]:
        return self._records("depreciation")

    def _depreciation_state(self) -> dict[str, Any]:
        """按档案顺序折叠：作废只抵消它**之前**的计提，作废后重提的照常生效。

        早先按「期间在作废集里就跳过」判，导致作废后重提的这一期永远算作未提——
        账上少一笔折旧，而且看不出来（这正是重提用例抓到的缺陷）。
        """
        effective: dict[str, dict[str, Any]] = {}
        voids: dict[str, int] = {}
        for rec in self._log.events():
            kind = rec.get("kind")
            if kind == "depreciation":
                effective[rec["period"]] = rec
            elif kind == "void":
                effective.pop(rec["period"], None)
                voids[rec["period"]] = voids.get(rec["period"], 0) + 1
        return {"effective": effective, "voids": voids}

    def _booked(self, asset_id: str) -> dict[str, Decimal]:
        """已计提：期间 → 金额（作废期间不计）。"""
        out: dict[str, Decimal] = {}
        for period, run in self._depreciation_state()["effective"].items():
            for line in run.get("assets") or []:
                if line["assetId"] == asset_id:
                    out[period] = _dec(line["amount"], "计提金额")
        return out

    # ---- 资本化门槛（人工登记）----

    def set_threshold(self, amount: Any, *, currency: str = "CNY",
                      approved_by: str = "owner") -> dict[str, Any]:
        """登记资本化门槛；`amount=None` 表示显式取消门槛。

        程序不得自设门槛：门槛是「多小就不该进固定资产」这条判断，属于人的政策决定。
        """
        actor = str(approved_by or "").strip()
        if not actor or actor in PROGRAM_ACTORS:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"资本化门槛须人工登记：{actor or '（空）'} 不得自设门槛",
                detail={"approved_by": actor, "program_actors": list(PROGRAM_ACTORS)})
        cur = str(currency or "").strip().upper()
        if not cur:
            raise TemplateError("INVALID_ARGUMENT", "门槛币种不能为空")
        if amount in (None, ""):
            rec = {"kind": "threshold", "amount": None, "currency": cur,
                   "registered_by": actor, "recorded_at": _now()}
        else:
            value = _dec(amount, "资本化门槛")
            if value < 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"资本化门槛不能为负：{value}")
            rec = {"kind": "threshold", "amount": _fmt(value), "currency": cur,
                   "registered_by": actor, "recorded_at": _now()}
        self._log.append(rec)
        return {"threshold": self.threshold(), "cleared": rec["amount"] is None}

    # ---- 登记 ----

    def register(self, asset_id: str, name: str, *, cost: Any, acquired_on: Any,
                 useful_life_months: Any, salvage: Any = 0,
                 currency: str = "CNY", category: str | None = None,
                 expense_account: str = "5200", method: str | None = None,
                 units_total: Any = None,
                 registered_by: str = "ops") -> dict[str, Any]:
        """登记一项固定资产（受资本化门槛约束；同标识幂等）。"""
        key = str(asset_id or "").strip()
        if not key:
            raise TemplateError("INVALID_ARGUMENT", "资产标识不能为空")
        title = str(name or "").strip()
        if not title:
            raise TemplateError("INVALID_ARGUMENT", "资产名称不能为空")

        cost_v = _dec(cost, "取得成本")
        if cost_v <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"取得成本必须大于 0：{cost_v}")
        salvage_v = _dec(salvage, "残值") if salvage not in (None, "") else Decimal(0)
        if salvage_v < 0:
            raise TemplateError("INVALID_ARGUMENT", f"残值不能为负：{salvage_v}")
        if salvage_v >= cost_v:
            raise TemplateError("INVALID_ARGUMENT",
                                f"残值必须小于取得成本：残值 {salvage_v} ≥ 成本 {cost_v}")
        try:
            months = int(useful_life_months)
        except (TypeError, ValueError) as exc:
            raise TemplateError("INVALID_ARGUMENT",
                                f"使用月数须为正整数：{useful_life_months!r}") from exc
        if months <= 0 or str(months) != str(useful_life_months).strip():
            raise TemplateError("INVALID_ARGUMENT",
                                f"使用月数须为正整数：{useful_life_months!r}")
        acquired = _parse_date(acquired_on)

        cur = str(currency or "").strip().upper() or "CNY"
        account = str(expense_account or "").strip()
        if account not in EXPENSE_ACCOUNTS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"折旧借方科目须在内核费用科目内：{account!r}",
                                detail={"allowed": list(EXPENSE_ACCOUNTS)})
        if method is not None and method not in METHODS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"折旧方法不在册：{method!r}",
                                detail={"allowed": list(METHODS)})

        units_total_v: Decimal | None = None
        if units_total not in (None, ""):
            units_total_v = _dec(units_total, "预计总产量")
            if units_total_v <= 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"预计总产量必须大于 0：{units_total_v}")

        # 门槛：币种不同不换汇——换算需要汇率出处，这里没有，宁可不比也不比错。
        gate = self.threshold()
        if gate is not None:
            if gate["currency"] != cur:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"资本化门槛币种 {gate['currency']} 与资产币种 {cur} 不同——"
                    f"不做汇率换算，请按同一币种登记门槛",
                    detail={"threshold_currency": gate["currency"],
                            "asset_currency": cur})
            if cost_v < _dec(gate["amount"], "资本化门槛"):
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"取得成本 {_fmt(cost_v)} {cur} 低于资本化门槛 "
                    f"{gate['amount']} {gate['currency']}——应费用化，不入固定资产",
                    detail={"cost": _fmt(cost_v), "threshold": gate["amount"],
                            "currency": cur, "treatment": "expense"})

        existing = self._assets().get(key)
        if existing is not None:
            return {"asset": self._view(existing), "duplicate": True}

        rec = {
            "kind": "asset", "assetId": key, "name": title,
            "category": str(category or "").strip() or None,
            "acquiredOn": acquired.isoformat(), "cost": _fmt(cost_v),
            "currency": cur, "salvage": _fmt(salvage_v),
            "usefulLifeMonths": months, "expenseAccount": account,
            "method": method, "unitsTotal": None if units_total_v is None
            else _fmt(units_total_v),
            "firstPeriod": _first_period(acquired),
            "registered_by": registered_by, "recorded_at": _now(),
            "status": "registered",
        }
        self._log.append(rec)
        return {"asset": self._view(rec), "duplicate": False}

    # ---- 口径与计划 ----

    def _method_of(self, asset: dict[str, Any]) -> str:
        return asset.get("method") or self._policy.current()["values"]["depreciation_method"]

    def _plan(self, asset: dict[str, Any]) -> list[dict[str, Any]]:
        """完整折旧计划（纯函数）：每期金额、累计、期末净值。"""
        cost = _dec(asset["cost"], "取得成本")
        salvage = _dec(asset["salvage"], "残值")
        months = int(asset["usefulLifeMonths"])
        method = self._method_of(asset)
        total = cost - salvage
        acquired = _parse_date(asset["acquiredOn"])
        rows: list[dict[str, Any]] = []

        if method == "units-of-production":
            units_total = asset.get("unitsTotal")
            unit_rate = (None if units_total in (None, "")
                         else (total / _dec(units_total, "预计总产量")))
            for k in range(1, months + 1):
                rows.append({
                    "period": _period_of(acquired, k), "index": k,
                    "amount": None, "unitRate": None if unit_rate is None
                    else _fmt(unit_rate),
                    "cumulative": None, "netBookValue": None, "method": method,
                    "note": "工作量法：逐期按实际产量计提，未给产量前不预算金额",
                })
            return rows

        cum = Decimal(0)
        amounts = self._plan_amounts(cost, salvage, months, method)
        for k in range(1, months + 1):
            amount = amounts[k - 1]
            cum += amount
            rows.append({
                "period": _period_of(acquired, k), "index": k,
                "amount": _fmt(amount), "cumulative": _fmt(cum),
                "netBookValue": _fmt(cost - cum), "method": method,
                "closing": k == months,
            })
        return rows

    @staticmethod
    def _plan_amounts(cost: Decimal, salvage: Decimal, months: int,
                      method: str) -> list[Decimal]:
        """整期折旧额序列（计划与计提共用同一份，两者因此必然一致）。

        - 直线法：可折旧额按期均摊，末期吸收分位残渣；
        - 双倍余额递减：期初净值 × 2/月数；一旦「剩余月数内直线」更优就**锁定**该等额
          继续提（design D4）——不锁的话每期重算 `(净值−残值)/剩余月数` 会因分位残差
          上下跳一分，账单上看着像错；
        - 末期一律取「剩余可折旧额」，保证累计折旧精确等于 原值 − 残值（design D7）。
        """
        total = cost - salvage
        out: list[Decimal] = []
        cum = Decimal(0)
        if method == "straight-line":
            per = (total / Decimal(months)).quantize(Q2, rounding=ROUND_HALF_UP)
            for k in range(1, months + 1):
                amount = total - cum if k == months else min(per, total - cum)
                amount = max(amount, Decimal(0))
                out.append(amount)
                cum += amount
            return out
        fixed: Decimal | None = None
        for k in range(1, months + 1):
            if k == months:
                amount = total - cum
            elif fixed is not None:
                amount = min(fixed, total - cum)
            else:
                opening = cost - cum
                ddb = (opening * Decimal(2) / Decimal(months)).quantize(
                    Q2, rounding=ROUND_HALF_UP)
                straight = ((opening - salvage) / Decimal(months - k + 1)).quantize(
                    Q2, rounding=ROUND_HALF_UP)
                if straight >= ddb:
                    fixed = straight  # 切换点：此后按等额提完
                    amount = min(fixed, total - cum)
                else:
                    amount = min(ddb, total - cum)
            amount = max(amount, Decimal(0))
            out.append(amount)
            cum += amount
        return out

    def plan(self, asset_id: str) -> dict[str, Any]:
        """折旧计划（含已计提标记）；无该资产即 NOT_FOUND。"""
        asset = self._assets().get(str(asset_id or "").strip())
        if asset is None:
            raise TemplateError("NOT_FOUND", f"资产不存在：{asset_id!r}",
                                detail={"assetId": asset_id})
        booked = self._booked(asset["assetId"])
        rows = []
        for row in self._plan(asset):
            hit = booked.get(row["period"])
            rows.append({**row, "booked": hit is not None,
                         "bookedAmount": None if hit is None else _fmt(hit)})
        return {"assetId": asset["assetId"], "method": self._method_of(asset),
                "policySource": ("asset" if asset.get("method") else "policy-book"),
                "periods": rows,
                "bookedAmount": _fmt(sum(booked.values(), Decimal(0))),
                "caliber_note": "计划是纯函数结果，可由原值/残值/月数/方法复算；"
                                "booked 只反映本层已记录的计提"}

    def book_value(self, asset_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        """账面净值 = 原值 − 累计折旧（按已记录计提；作废期间不计）。"""
        asset = self._assets().get(str(asset_id or "").strip())
        if asset is None:
            raise TemplateError("NOT_FOUND", f"资产不存在：{asset_id!r}",
                                detail={"assetId": asset_id})
        limit = (None if as_of in (None, "")
                 else _check_period(str(as_of).strip()[:7]))
        booked = self._booked(asset["assetId"])
        accum = sum((v for p, v in booked.items() if limit is None or p <= limit),
                    Decimal(0))
        cost = _dec(asset["cost"], "取得成本")
        salvage = _dec(asset["salvage"], "残值")
        return {
            "assetId": asset["assetId"], "cost": asset["cost"],
            "accumulated": _fmt(accum), "netBookValue": _fmt(cost - accum),
            "salvage": asset["salvage"],
            "fullyDepreciated": (cost - accum) <= salvage,
            "asOf": limit, "method": self._method_of(asset),
            "currency": asset["currency"],
        }

    # ---- 期末计提 ----

    def depreciate(self, period: Any, *, units: dict[str, Any] | None = None,
                   submitted_by: str = "asset-engine") -> dict[str, Any]:
        """按期间计提折旧：生成一张**待确认** proposal（借费用 / 贷 1602）。"""
        key = _check_period(period)
        cfg = self._policy.current()["values"]
        owner = cfg["asset_ledger_owner"]
        if owner != "platform":
            raise TemplateError(
                "NOT_WIRED",
                f"资产台账归属口径为 {owner}：需客户资产系统可接，本车道未接线——"
                f"不按平台台账继续计提",
                detail={"asset_ledger_owner": owner})

        state = self._depreciation_state()
        existing = state["effective"].get(key)
        if existing is not None:
            return {"period": key, "duplicate": True,
                    "proposal": existing.get("proposal"),
                    "total": existing.get("total"),
                    "assets": existing.get("assets") or [],
                    "note": "该期间已计提过，返回首次结果（不重复计提）"}

        assets = self._assets()
        units = {str(k): v for k, v in (units or {}).items()}
        lines_out: list[dict[str, Any]] = []
        missing_units: list[str] = []
        missing_units_total: list[str] = []

        for asset_id in sorted(assets):
            asset = assets[asset_id]
            idx = _index_in(key, _parse_date(asset["acquiredOn"]))
            if idx is None or idx > int(asset["usefulLifeMonths"]):
                continue  # 尚未起提 / 已过折旧期
            booked = self._booked(asset_id)
            if key in booked:
                continue
            method = self._method_of(asset)
            cost = _dec(asset["cost"], "取得成本")
            salvage = _dec(asset["salvage"], "残值")
            total = cost - salvage
            accum = sum(booked.values(), Decimal(0))
            remaining = total - accum
            if remaining <= 0:
                continue  # 已提足（净值 = 残值）
            if method == "units-of-production":
                units_total = asset.get("unitsTotal")
                if units_total in (None, ""):
                    missing_units_total.append(asset_id)
                    continue
                if asset_id not in units:
                    missing_units.append(asset_id)
                    continue
                used = _dec(units[asset_id], f"资产 {asset_id} 当期产量")
                if used < 0:
                    raise TemplateError("INVALID_ARGUMENT",
                                        f"资产 {asset_id} 当期产量不能为负：{used}")
                amount = (used * total / _dec(units_total, "预计总产量")).quantize(
                    Q2, rounding=ROUND_HALF_UP)
                amount = min(amount, remaining)
            else:
                # 与计划同一份金额序列：正常路径下逐期金额与计划逐项相等，可复算；
                # 前期被作废留缺口时按「剩余可折旧额」封顶，末期仍精确收口。
                amounts = self._plan_amounts(cost, salvage,
                                             int(asset["usefulLifeMonths"]), method)
                amount = min(amounts[idx - 1], remaining)
            if amount <= 0:
                continue
            lines_out.append({
                "assetId": asset_id, "amount": _fmt(amount),
                "expenseAccount": asset["expenseAccount"], "method": method,
                "currency": asset["currency"],
            })

        if missing_units_total or missing_units:
            detail = {"period": key, "method": "units-of-production"}
            parts = []
            if missing_units_total:
                detail["missing_units_total"] = missing_units_total
                parts.append(f"未登记预计总产量：{', '.join(missing_units_total)}")
            if missing_units:
                detail["missing_units"] = missing_units
                parts.append(f"缺少当期产量：{', '.join(missing_units)}")
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"工作量法计提被拒（{key}）——不猜产量、不用时间折旧顶替。"
                + "；".join(parts),
                detail=detail)

        if not lines_out:
            return {"period": key, "duplicate": False, "proposal": None,
                    "total": "0.00", "assets": [],
                    "note": "本期无可计提资产（尚未起提、已提足或已过折旧期）"}

        lines: list[dict[str, Any]] = []
        for item in lines_out:
            lines.append({"account_code": item["expenseAccount"], "direction": "debit",
                          "amount": item["amount"], "currency": item["currency"]})
            lines.append({"account_code": ACCUMULATED, "direction": "credit",
                          "amount": item["amount"], "currency": item["currency"]})
        total = sum((_dec(i["amount"], "计提金额") for i in lines_out), Decimal(0))

        # 作废后重提要换稳定键：内核同 stable_id 永远返回首次结果（哪怕那次被退回），
        # 用同一个键重提等于「什么都没发生」。故按本期间作废次数编号。
        attempt = state["voids"].get(key, 0)
        stable_id = f"depr:{key}" if attempt == 0 else f"depr:{key}#{attempt + 1}"

        proposal = self._kernel.submit_proposal(
            stable_id, lines, source_ref=f"depreciation:{key}", kind="journal",
            lane_title=f"{key} 固定资产折旧", submitted_by=submitted_by)
        run = {"kind": "depreciation", "period": key, "assets": lines_out,
               "total": _fmt(total), "stableId": stable_id,
               "proposal": proposal,
               "policy": {"depreciation_method": cfg["depreciation_method"]},
               "recorded_at": _now(), "submitted_by": submitted_by}
        self._log.append(run)
        return {"period": key, "duplicate": bool(proposal and proposal.get("duplicate")),
                "proposal": proposal, "total": _fmt(total), "assets": lines_out,
                "method": cfg["depreciation_method"],
                "caliber_note": "计提只生成待确认 proposal；过门后由内核落账"}

    def void(self, period: Any, *, reason: str, voided_by: str = "owner") -> dict[str, Any]:
        """作废某期计提（被退回或提错时）；已过门落账的须先在内核冲正。"""
        key = _check_period(period)
        note = str(reason or "").strip()
        if not note:
            raise TemplateError("INVALID_ARGUMENT", "作废计提须说明原因（退回理由即审计线索）")
        state = self._depreciation_state()
        run = state["effective"].get(key)
        if run is None:
            if state["voids"].get(key):
                return {"period": key, "duplicate": True,
                        "note": "该期间已作废过（要重提直接调 depreciate）"}
            raise TemplateError("NOT_FOUND", f"没有该期间的计提记录：{key}",
                                detail={"period": key})
        status = str((run.get("proposal") or {}).get("status") or "").lower()
        if status in ("confirmed", "posted"):
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"{key} 计提已过门落账，不能直接作废——先在内核冲正该分录再作废",
                detail={"period": key, "proposal_status": status})
        self._log.append({"kind": "void", "period": key, "reason": note,
                          "voided_by": voided_by, "recorded_at": _now()})
        return {"period": key, "duplicate": False, "reason": note}

    # ---- 视图 ----

    def _view(self, asset: dict[str, Any]) -> dict[str, Any]:
        booked = self._booked(asset["assetId"])
        cost = _dec(asset["cost"], "取得成本")
        accum = sum(booked.values(), Decimal(0))
        return {
            "assetId": asset["assetId"], "name": asset["name"],
            "category": asset.get("category"),
            "acquiredOn": asset["acquiredOn"], "cost": asset["cost"],
            "currency": asset["currency"], "salvage": asset["salvage"],
            "usefulLifeMonths": asset["usefulLifeMonths"],
            "expenseAccount": asset["expenseAccount"],
            "method": self._method_of(asset),
            "methodSource": "asset" if asset.get("method") else "policy-book",
            "firstPeriod": asset["firstPeriod"],
            "accumulated": _fmt(accum),
            "netBookValue": _fmt(cost - accum),
            "remainingMonths": self._remaining_months(asset, booked),
            "status": asset.get("status"),
        }

    @staticmethod
    def _remaining_months(asset: dict[str, Any], booked: dict[str, Decimal]) -> int:
        months = int(asset["usefulLifeMonths"])
        return max(0, months - len(booked))

    def assets(self) -> list[dict[str, Any]]:
        return [self._view(a) for a in self._assets().values()]

    def summary(self) -> dict[str, Any]:
        """台账摘要：本层口径戳 + 规模。"""
        cfg = self._policy.current()["values"]
        rows = self.assets()
        return {
            "assetCount": len(rows),
            "totalCost": _fmt(sum((_dec(r["cost"], "取得成本") for r in rows),
                                  Decimal(0))),
            "totalAccumulated": _fmt(sum((_dec(r["accumulated"], "累计折旧")
                                          for r in rows), Decimal(0))),
            "totalNetBookValue": _fmt(sum((_dec(r["netBookValue"], "净值")
                                          for r in rows), Decimal(0))),
            "threshold": self.threshold(),
            "policy": {"depreciation_method": cfg["depreciation_method"],
                       "asset_ledger_owner": cfg["asset_ledger_owner"]},
            "caliber_note": "台账在本层（口径 platform）；占比与税务口径走映射包",
        }


__all__ = ["FixedAssetLane", "METHODS", "ACCUMULATED", "EXPENSE_ACCOUNTS"]

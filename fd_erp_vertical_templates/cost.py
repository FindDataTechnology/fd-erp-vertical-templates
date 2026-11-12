"""成本计价引擎（change: add-valuation-policy-book 任务 2.x）。

**不建表、不迁移**：成本是「对已接受事件的确定性重放」——与数量面同一哲学（账本降投影）。
数量面答「多少」，成本面答「值多少」，两者都由同一批事实推出；重放一遍即校验一遍，
不存在「成本表与事件不一致」这种要再对一次账的形态。

三种计价方法**各自实现**，由口径册 `cost_method` 选择：

- `weighted-average`：每个维度只有一个加权池，入库即时重算单位成本，出库按当时均价；
- `fifo`：逐层消耗最早的入库层；
- `batch-actual`：同样逐层，但批次不可知即拒绝（用均价顶替就变成「假装是批次法」）。

**缺成本默认拒绝，绝不补零**：零成本会一路污染结存金额与毛利，而且从报表上看不出来。
缺成本处理由口径 `provisional_basis` 决定（`reject` / `last-cost` / `standard`），
用估计值必须带 `basis` 标记，结账报告单列。

截止用**事件 id** 而非时间戳：`occurred_at` 可补录（业务时点 ≠ 写入时点），按时间戳截断会把
补录的旧事漏算或多算；报告仍按 `occurred_at` 归期间。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog
from .policy import PolicyBook

Q6 = Decimal("0.000001")
UNKNOWN = "__unknown__"
STD_SCHEMA = "fd-erp-vertical-templates/standard-cost/v1"

TREATMENT = {
    "to-cogs": "差异按口径进营业成本（当月损益）",
    "capitalize": "差异按口径按出库量占比资本化进存货",
    "track-only": "按口径只跟踪不结转（差异留在分析层，不改报表金额）",
}

GAP_MESSAGES = {
    "insufficient_layers": "成本层不足（出库多于已入库）——不按 0 计价",
    "cost_unknown": "消耗到成本未知的入库层——不按 0 计价",
    "batch_unknown": "口径为批次实际，但出库批次未知——不按均价顶替",
    "cost_missing": "入库无成本且口径为 reject——该批标记成本未知，消耗它的出库会被拒",
    "last_cost_unavailable": "口径为 last-cost 但该商品此前没有已知成本",
    "standard_cost_missing": "口径为 standard 但该商品未登记标准成本——拒绝而非猜",
}


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}") from exc


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q6, rounding=ROUND_HALF_UP))


def _period_of(occurred_at: str, grain: str) -> str:
    """业务时点 → 期间键（月=YYYY-MM，周=ISO 周 YYYY-Www）。"""
    raw = str(occurred_at or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if grain == "weekly":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return dt.strftime("%Y-%m")


class _Layer:
    __slots__ = ("qty", "unit_cost", "basis", "event_id", "currency", "occurred_at")

    def __init__(self, qty: Decimal, unit_cost: Decimal | None, basis: str,
                 event_id: int, currency: str, occurred_at: str):
        self.qty = qty
        self.unit_cost = unit_cost
        self.basis = basis
        self.event_id = event_id
        self.currency = currency
        self.occurred_at = occurred_at

    def as_dict(self) -> dict[str, Any]:
        return {"qty": _fmt(self.qty),
                "unitCost": None if self.unit_cost is None else _fmt(self.unit_cost),
                "basis": self.basis, "eventId": self.event_id,
                "currency": self.currency, "occurredAt": self.occurred_at}


class CostEngine:
    """成本层重放 + 出库计价 + 期末结账。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any,
                 policy: PolicyBook, *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._policy = policy
        self._base = base_currency
        self._std_log = EventLog(Path(data_dir) / f"standard-costs-{tenant_id}.jsonl")

    # ---- 标准成本登记（provisional_basis=standard 时用）----

    def set_standard_cost(self, sku: str, unit_cost: Any, *, currency: str | None = None,
                          approved_by: str = "owner") -> dict[str, Any]:
        actor = str(approved_by or "").strip()
        if not actor or actor == "system":
            raise TemplateError("INVALID_ARGUMENT",
                                "标准成本须人工登记：approved_by 不得为空或 system")
        code = str(sku or "").strip()
        if not code:
            raise TemplateError("INVALID_ARGUMENT", "标准成本须给商品编码")
        cost = _dec(unit_cost, "标准成本")
        if cost < 0:
            raise TemplateError("INVALID_ARGUMENT", f"标准成本不得为负：{cost}")
        rec = {"schema": STD_SCHEMA, "tenant_id": self._tenant, "sku": code,
               "unit_cost": _fmt(cost), "currency": currency or self._base,
               "approved_by": actor,
               "registered_at": datetime.now(timezone.utc).isoformat()}
        self._std_log.append(rec)
        return {"sku": code, "unit_cost": _fmt(cost), "currency": rec["currency"],
                "note": "已登记（append-only；后登记覆盖先前，历史可回放）"}

    def _standard_cost(self, sku: str) -> Decimal | None:
        hit = None
        for rec in self._std_log.events():
            if rec.get("sku") == sku:
                hit = rec
        return None if hit is None else _dec(hit["unit_cost"], "标准成本")

    # ---- 重放 ----

    def replay(self, *, until_event_id: int | None = None) -> dict[str, Any]:
        """重放已接受事件，得到成本层、结存、出库成本与缺口（确定性）。"""
        cfg = self._policy.current()["values"]
        method = cfg["cost_method"]
        provisional = cfg["provisional_basis"]
        pools: dict[tuple[str, str, str], list[_Layer]] = {}
        receipts: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        last_cost: dict[str, Decimal] = {}

        for ev in self._events(until_event_id=until_event_id):
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            sku = str(payload.get("sku") or "").strip()
            if not sku:
                continue
            wh = str(payload.get("warehouse") or UNKNOWN)
            lot = str(payload.get("lot") or UNKNOWN)
            qty = _dec(payload.get("quantity"), f"事件 {ev.get('id')} 数量")
            when = str(ev.get("occurred_at") or "")
            key = (sku, wh, lot)

            if ev.get("event_type") == "goods_received":
                unit_cost, basis = self._receipt_cost(
                    ev, payload, sku, provisional, last_cost, gaps)
                if unit_cost is not None:
                    last_cost[sku] = unit_cost
                receipts.append({"eventId": ev.get("id"), "sku": sku, "warehouse": wh,
                                 "lot": lot, "qty": _fmt(qty),
                                 "unitCost": None if unit_cost is None else _fmt(unit_cost),
                                 "value": None if unit_cost is None
                                 else _fmt(qty * unit_cost),
                                 "basis": basis, "occurredAt": when})
                self._add_receipt(pools.setdefault(key, []), qty, unit_cost, basis,
                                  int(ev["id"]), when, method, ev, sku, gaps)
                continue

            if ev.get("event_type") != "goods_issued":
                continue
            if method == "batch-actual" and lot == UNKNOWN:
                gaps.append({"eventId": ev.get("id"), "sku": sku, "warehouse": wh,
                             "reason": "batch_unknown"})
                issues.append({"eventId": ev.get("id"), "sku": sku, "warehouse": wh,
                               "lot": lot, "qty": _fmt(qty), "occurredAt": when,
                               "cost": None, "unitCost": None, "layers": [],
                               "reason": "batch_unknown"})
                continue
            cost, consumed, gap = self._consume(pools.setdefault(key, []), qty, method)
            issues.append({"eventId": ev.get("id"), "sku": sku, "warehouse": wh,
                           "lot": lot, "qty": _fmt(qty), "occurredAt": when,
                           "cost": None if cost is None else _fmt(cost),
                           "unitCost": None if cost is None else _fmt(cost / qty),
                           "layers": consumed, "reason": gap})
            if gap:
                gaps.append({"eventId": ev.get("id"), "sku": sku, "warehouse": wh,
                             "lot": lot, "reason": gap})

        closing = []
        for (sku, wh, lot), layers in pools.items():
            qty_left = sum((l.qty for l in layers), Decimal(0))
            if qty_left == 0:
                continue
            if any(l.unit_cost is None for l in layers):
                closing.append({"sku": sku, "warehouse": wh, "lot": lot,
                                "qty": _fmt(qty_left), "value": None,
                                "unitCost": None, "basis": "unknown"})
                continue
            value = sum((l.qty * l.unit_cost for l in layers), Decimal(0))
            basis = "actual" if all(l.basis == "actual" for l in layers) else "estimated"
            closing.append({"sku": sku, "warehouse": wh, "lot": lot,
                            "qty": _fmt(qty_left), "value": _fmt(value),
                            "unitCost": _fmt(value / qty_left), "basis": basis})
        closing.sort(key=lambda r: (r["sku"], r["warehouse"], r["lot"]))
        return {"method": method, "provisionalBasis": provisional,
                "untilEventId": until_event_id, "receipts": receipts,
                "issues": issues, "closing": closing,
                "gaps": [{**g, "message": GAP_MESSAGES.get(str(g["reason"]), "")}
                         for g in gaps],
                "pools": {f"{k[0]}|{k[1]}|{k[2]}": [l.as_dict() for l in v]
                          for k, v in pools.items()}}

    def _add_receipt(self, pool: list[_Layer], qty: Decimal,
                     unit_cost: Decimal | None, basis: str, event_id: int,
                     when: str, method: str, ev: dict[str, Any], sku: str,
                     gaps: list[dict[str, Any]]) -> None:
        currency = self._base
        if method == "weighted-average":
            if not pool:
                pool.append(_Layer(qty, unit_cost, basis, event_id, currency, when))
                return
            cur = pool[0]
            total_qty = cur.qty + qty
            if cur.unit_cost is None or unit_cost is None:
                # 池里任何一层成本未知 → 整池未知：不拿已知部分假装均价
                cur.qty = total_qty
                cur.unit_cost = None
                cur.basis = "unknown"
                gaps.append({"eventId": event_id, "sku": sku,
                             "reason": "cost_unknown"})
                return
            total_value = cur.qty * cur.unit_cost + qty * unit_cost
            cur.qty = total_qty
            cur.unit_cost = total_value / total_qty
            cur.basis = ("actual" if cur.basis == "actual" and basis == "actual"
                         else "estimated")
            return
        pool.append(_Layer(qty, unit_cost, basis, event_id, currency, when))

    def _events(self, *, until_event_id: int | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_type in ("goods_received", "goods_issued"):
            after = 0
            seen = 0
            total: int | None = None
            while True:
                out = self._kernel.query_events(
                    event_type=event_type, status="accepted", limit=500,
                    after_event_id=after) or {}
                page = [r for r in (out.get("events") or out.get("data") or [])
                        if isinstance(r, dict)]
                declared = out.get("declared_total")
                if isinstance(declared, int):
                    total = declared
                if not page:
                    break
                rows.extend(page)
                seen += len(page)
                nxt = max(int(r.get("id") or 0) for r in page)
                if nxt <= after:
                    break
                after = nxt
                if total is not None and seen >= total:
                    break
        if until_event_id is not None:
            rows = [r for r in rows if int(r.get("id") or 0) <= int(until_event_id)]
        rows.sort(key=lambda r: int(r.get("id") or 0))
        return rows

    def _receipt_cost(self, ev: dict[str, Any], payload: dict[str, Any], sku: str,
                      provisional: str, last_cost: dict[str, Decimal],
                      gaps: list[dict[str, Any]]) -> tuple[Decimal | None, str]:
        raw = payload.get("cost")
        if raw not in (None, ""):
            return _dec(raw, f"事件 {ev.get('id')} 成本"), "actual"
        if provisional == "reject":
            gaps.append({"eventId": ev.get("id"), "sku": sku, "reason": "cost_missing"})
            return None, "unknown"
        if provisional == "last-cost":
            hit = last_cost.get(sku)
            if hit is None:
                gaps.append({"eventId": ev.get("id"), "sku": sku,
                             "reason": "last_cost_unavailable"})
                return None, "unknown"
            return hit, "last-cost"
        hit = self._standard_cost(sku)
        if hit is None:
            gaps.append({"eventId": ev.get("id"), "sku": sku,
                         "reason": "standard_cost_missing"})
            return None, "unknown"
        return hit, "standard"

    @staticmethod
    def _consume(layers: list[_Layer], qty: Decimal, method: str
                 ) -> tuple[Decimal | None, list[dict[str, Any]], str | None]:
        """逐层消耗（加权池只有一层，同样适用），成本未知即整笔不给价。"""
        need = qty
        cost = Decimal(0)
        consumed: list[dict[str, Any]] = []
        unknown = False
        for layer in list(layers):
            if need <= 0:
                break
            take = min(layer.qty, need)
            if take <= 0:
                continue
            if layer.unit_cost is None:
                unknown = True
            else:
                cost += take * layer.unit_cost
            consumed.append({"eventId": layer.event_id, "qty": _fmt(take),
                             "unitCost": None if layer.unit_cost is None
                             else _fmt(layer.unit_cost), "basis": layer.basis})
            layer.qty -= take
            need -= take
        layers[:] = [l for l in layers if l.qty > 0]
        if need > 0:
            return None, consumed, "insufficient_layers"
        if unknown:
            return None, consumed, "cost_unknown"
        return cost, consumed, None

    # ---- 出库计价（供库存作业车道调用）----

    def issue_cost(self, sku: str, qty: Any, *, warehouse: str | None = None,
                   lot: str | None = None) -> dict[str, Any]:
        """按当前口径为一次出库定价；取不到成本即拒——不返回 0、不返回 None 混过去。"""
        code = str(sku or "").strip()
        if not code:
            raise TemplateError("INVALID_ARGUMENT", "出库计价须给商品编码")
        need = _dec(qty, "出库数量")
        if need <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"出库数量必须大于 0：{need}")
        wh = str(warehouse or UNKNOWN)
        lot_v = str(lot or UNKNOWN)
        cfg = self._policy.current()["values"]
        snap = self.replay()
        pool = snap["pools"].get(f"{code}|{wh}|{lot_v}")
        if pool is None:
            raise TemplateError("INVALID_ARGUMENT",
                                f"没有成本层：{code}@{wh}/{lot_v}（先入库再出库）",
                                detail={"sku": code, "warehouse": wh, "lot": lot_v})
        layers = [_Layer(_dec(l["qty"], "层数量"),
                         None if l["unitCost"] is None else _dec(l["unitCost"], "层单价"),
                         l["basis"], int(l["eventId"]), l["currency"], l["occurredAt"])
                  for l in pool]
        if cfg["cost_method"] == "batch-actual" and lot_v == UNKNOWN:
            raise TemplateError("INVALID_ARGUMENT",
                                "口径为批次实际，但该出库批次未知——不按均价顶替",
                                detail={"sku": code, "lot": lot_v})
        cost, consumed, gap = self._consume(layers, need, cfg["cost_method"])
        if gap == "insufficient_layers":
            have = sum((l.qty for l in layers), Decimal(0))
            raise TemplateError("INVALID_ARGUMENT",
                                f"成本层不足：{code}@{wh} 可用 {_fmt(have)}，"
                                f"需要 {_fmt(need)}",
                                detail={"sku": code, "warehouse": wh, "lot": lot_v,
                                        "available": _fmt(have), "requested": _fmt(need)})
        if gap == "cost_unknown":
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"出库计价被拒：{code}@{wh} 消耗到成本未知的入库层（口径 "
                f"provisional_basis={cfg['provisional_basis']}）——不按 0 计价",
                detail={"sku": code, "warehouse": wh, "lot": lot_v,
                        "provisional_basis": cfg["provisional_basis"]})
        assert cost is not None
        return {"sku": code, "warehouse": wh, "lot": lot_v, "qty": _fmt(need),
                "method": cfg["cost_method"], "unitCost": _fmt(cost / need),
                "amount": _fmt(cost), "layers": consumed}

    # ---- 期末结账 ----

    def close(self, period: str | None = None) -> dict[str, Any]:
        """结账报告：结存、期间入库/出库、暂估差异分类。报告自带口径戳。"""
        cfg = self._policy.current()["values"]
        grain = cfg["close_grain"]
        snap = self.replay()
        target = str(period or "").strip() or None

        def hit(when: str) -> bool:
            return target is None or _period_of(when, grain) == target

        r_count = 0
        r_qty = r_value = r_est_qty = r_est_value = Decimal(0)
        for rec in snap["receipts"]:
            if not hit(rec["occurredAt"]):
                continue
            r_count += 1
            r_qty += _dec(rec["qty"], "入库数量")
            if rec["value"] is not None:  # 成本未知的层不进金额（也不补零）
                value = _dec(rec["value"], "入库金额")
                r_value += value
                if rec["basis"] in ("last-cost", "standard"):
                    r_est_qty += _dec(rec["qty"], "入库数量")
                    r_est_value += value

        i_count = 0
        i_qty = i_cost = Decimal(0)
        uncosted = 0
        for rec in snap["issues"]:
            if not hit(rec["occurredAt"]):
                continue
            i_count += 1
            i_qty += _dec(rec["qty"], "出库数量")
            if rec["cost"] is None:
                uncosted += 1
            else:
                i_cost += _dec(rec["cost"], "出库成本")

        at_risk = r_est_value
        return {
            "period": target, "grain": grain,
            "policy": {"cost_method": cfg["cost_method"],
                       "provisional_basis": cfg["provisional_basis"],
                       "variance_policy": cfg["variance_policy"],
                       "close_grain": grain},
            "receipts": {"count": r_count, "qty": _fmt(r_qty), "value": _fmt(r_value),
                         "estimatedQty": _fmt(r_est_qty),
                         "estimatedValue": _fmt(r_est_value)},
            "issues": {"count": i_count, "qty": _fmt(i_qty), "cost": _fmt(i_cost),
                       "uncosted": uncosted},
            "closing": snap["closing"],
            "variance": {"policy": cfg["variance_policy"],
                         "amountAtRisk": _fmt(at_risk),
                         "treatment": TREATMENT[cfg["variance_policy"]],
                         "note": ("这些入库层用暂估口径计价，真实成本到达后才能确定差异；"
                                  "现在给的金额是「可能被高估/低估的总量」，不是已实现差异"
                                  if at_risk > 0 else "无暂估层，无差异")},
            "gaps": snap["gaps"],
            "caliber_note": "报告绑定生成时点的口径；换口径重跑会得到另一份可解释的报告"
                            f"（method={cfg['cost_method']}, grain={grain}, "
                            f"provisional={cfg['provisional_basis']}）",
        }

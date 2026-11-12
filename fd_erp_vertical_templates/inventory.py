"""库存作业车道（change: add-inventory-operations-lane tasks 1.x）。

四类仓库作业各自落成内核数量面事实，**不新增事件类型、不改迁移**：

- 入库单 → `goods_received`（不要求挂采购单）
- 出库单 → `goods_issued`
- 调拨单 → 源仓 `goods_issued` + 目的仓 `goods_received`（两笔事实，数量守恒）
- 盘点差异 → `goods_adjusted`（`reason` 必填、数量带正负；无差异不出事实）

## 状态机：提交与投影为什么分两步

事实走**拟稿车道**：`submit_event` 落 `pending`，而 `project_object_event` 只认 `accepted`
——所以提交时不可能、也不应该投影。拍板由拍板席经内核 `decide_event` 完成；内核工具面契约把
`decide_event` 列为模板层的「故意不覆盖」（模板不代任何一方过门），因此本模块**不提供过门**，
只提供 `project()`：事实已被拍板为 accepted 之后，按单据里存的维度做投影与过账。

这样调拨的原子性落在**过门**而不是写入：两笔都只是 pending 时投影为零，账不会脏；某笔过门
失败留下的是**可见的半边**（单据读面逐笔给状态），对侧重试幂等收敛。

## 纪律

- 幂等键 = `<单据ref>:<行号>:<动作>`（`in`/`out`/`adjust`）：同单两行同商品是两笔事实，
  用行内容当键会把它们并成一条。
- 库位/批次/单位不可知时显式写 `__unknown__`：空串会被内核读成「没有」并拒收。
- 出库/调拨先按维度核对可用量，不足整单拒绝并点名缺口——**不允许用出库单把账做成负的**。
- 过账权在内核；缺规则如实记 `posted=False` + 原因，不补零金额。
- 本模块对任何外部 WMS 只有读面（由 `WmsSnapshotAdapter` 接口保证），不回写外部台账。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog

INV_SCHEMA = "fd-erp-vertical-templates/inventory/v1"
#: 内核「确实不可知」哨兵（与 events.UNKNOWN / orchestration.UNKNOWN 同串）。
UNKNOWN = "__unknown__"
Q6 = Decimal("0.000001")

KINDS = ("receipt", "issue", "transfer", "stocktake")
_KIND_TITLES = {"receipt": "入库单", "issue": "出库单",
                "transfer": "调拨单", "stocktake": "盘点单"}
#: 盘点差异原因（内核 schema 要求 reason 非空）
REASON_GAIN = "stocktake_gain"
REASON_LOSS = "stocktake_loss"


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q6, rounding=ROUND_HALF_UP))


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}",
                            detail={"value": str(value)}) from exc


def _ref(prefix: str, seed: str) -> str:
    return f"{prefix}-{hashlib.sha1(seed.encode()).hexdigest()[:10].upper()}"


class InventoryLane:
    """入库/出库/调拨/盘点：提交落拟稿事实，拍板后投影与过账。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any):
        self._tenant = tenant_id
        self._kernel = kernel
        self._log = EventLog(Path(data_dir) / f"inventory-{tenant_id}.jsonl")

    # ---- 事件档 ----

    def _docs(self) -> list[dict[str, Any]]:
        return [e for e in self._log.events()
                if e.get("record") == "inventory_document"]

    def _find(self, ref: str) -> dict[str, Any]:
        hit = next((d for d in self._docs() if d.get("request_id") == ref), None)
        if hit is None:
            raise TemplateError("NOT_FOUND", f"库存作业单据不存在：{ref}",
                                detail={"ref": ref})
        return hit

    # ---- 1) 提交（拟稿事实）----

    def receive(self, *, warehouse: str, lines: list[dict[str, Any]],
                ref: str | None = None, occurred_at: str | None = None,
                requested_by: str = "wh-ops", note: str | None = None
                ) -> dict[str, Any]:
        """入库单：逐行写 `goods_received` 拟稿事实（可不挂采购单）。"""
        return self._submit("receipt", lines=lines, ref=ref,
                            warehouse=warehouse, occurred_at=occurred_at,
                            requested_by=requested_by, note=note)

    def issue(self, *, warehouse: str, lines: list[dict[str, Any]],
              ref: str | None = None, occurred_at: str | None = None,
              requested_by: str = "wh-ops", note: str | None = None
              ) -> dict[str, Any]:
        """出库单：先按维度核对可用量，不足整单拒绝；足量才逐行写 `goods_issued`。"""
        return self._submit("issue", lines=lines, ref=ref,
                            warehouse=warehouse, occurred_at=occurred_at,
                            requested_by=requested_by, note=note)

    def transfer(self, *, from_warehouse: str, to_warehouse: str,
                 lines: list[dict[str, Any]], ref: str | None = None,
                 occurred_at: str | None = None, requested_by: str = "wh-ops",
                 note: str | None = None) -> dict[str, Any]:
        """调拨单：源仓 `goods_issued` + 目的仓 `goods_received` 两笔事实。"""
        src = str(from_warehouse or "").strip()
        dst = str(to_warehouse or "").strip()
        if not src or not dst:
            raise TemplateError("INVALID_ARGUMENT", "调拨必须同时给调出仓与调入仓")
        if src == dst:
            raise TemplateError("INVALID_ARGUMENT",
                                f"调出仓与调入仓相同（{src}），这不是调拨",
                                detail={"warehouse": src})
        return self._submit("transfer", lines=lines, ref=ref,
                            warehouse=src, to_warehouse=dst,
                            occurred_at=occurred_at,
                            requested_by=requested_by, note=note)

    def stocktake(self, *, warehouse: str, counts: list[dict[str, Any]],
                  ref: str | None = None, occurred_at: str | None = None,
                  requested_by: str = "wh-ops", note: str | None = None
                  ) -> dict[str, Any]:
        """盘点单：只对**有差异**的行写 `goods_adjusted`（盘盈正、盘亏负）。

        账面数量一律读本板块数量面（`query_object_balance`），不接受调用方递来的账面数：
        拿外部 WMS 的账面数当自己的账面数，做出来的是别人的盘盈亏。
        """
        ref = str(ref or _ref("ST", f"{self._tenant}|{warehouse}|{uuid.uuid4().hex}"))
        existing = next((d for d in self._docs() if d.get("request_id") == ref), None)
        if existing is not None:  # 同单重放：幂等返回首次结果，不重复落事实
            return self._summary(existing, duplicate=True)
        business_at = str(occurred_at or datetime.now(timezone.utc).isoformat())
        records: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for idx, raw in enumerate(counts or [], start=1):
            line = dict(raw or {})
            try:
                sku = self._sku(line, idx)
                wh = str(line.get("warehouse") or warehouse).strip()
                lot = str(line.get("lot") or "").strip() or UNKNOWN
                unit = str(line.get("unit") or "").strip() or UNKNOWN
                counted = _dec(line.get("counted_qty", line.get("countedQty")),
                               f"盘点实盘数量（第 {idx} 行）")
                book = self._available(sku, wh, lot)
            except TemplateError as exc:
                errors.append({"line": idx, "ok": False, "code": exc.code,
                               "message": exc.message})
                continue
            diff = counted - book
            if diff == 0:  # 无差异不出事实（不写零差异）
                continue
            stable_id = f"{ref}:{idx}:adjust"
            payload = {"sku": sku, "quantity": _fmt(diff), "unit": unit,
                       "warehouse": wh, "lot": lot,
                       "reason": REASON_GAIN if diff > 0 else REASON_LOSS}
            try:
                out = self._kernel.submit_event(
                    "goods_adjusted", stable_id, business_at, payload,
                    source_ref=ref)
            except Exception as exc:  # noqa: BLE001 — 协议边界
                errors.append(self._kernel_error(idx, exc))
                continue
            records.append({"line": idx, "side": "", "sku": sku,
                            "quantity": _fmt(counted), "delta": _fmt(diff),
                            "unit": unit, "warehouse": wh, "lot": lot,
                            "booked_qty": _fmt(book),
                            "reason": payload["reason"],
                            "stable_id": stable_id,
                            "event_id": out.get("id"),
                            "event_status": out.get("status", "pending"),
                            "duplicate": bool(out.get("duplicate"))})
        return self._record("stocktake", ref, records, errors, business_at,
                            warehouse=warehouse, requested_by=requested_by,
                            note=note)

    # ---- 2) 拍板后的投影与过账 ----

    def project(self, ref: str, *, line: int | None = None,
                side: str | None = None, settled_by: str = "gate",
                finalize: bool = True) -> dict[str, Any]:
        """对**已过确认门**（accepted）的事实做投影与过账（逐行，可单行重试）。

        `line`/`side` 只做筛选：单行失败时调用方可以只重试那一行，其余行不重算。
        `finalize=True` 且全部行成功时把单据状态推进为 confirmed。
        """
        doc = self._find(ref)
        if str(doc.get("status")) == "rejected":
            raise TemplateError("WORKFLOW_STATE",
                                f"库存作业单据已被驳回（{ref}），不可再投影")
        results: list[dict[str, Any]] = []
        for item in doc["payload"]["lines"]:
            if line is not None and int(item["line"]) != int(line):
                continue
            if side is not None and str(item.get("side") or "") != side:
                continue
            results.append(self._project_one(ref, item))
        if finalize and results and all(r.get("ok") for r in results) \
                and not doc["payload"].get("errors"):
            self._log.set_status(ref, "confirmed", decided_by=settled_by)
        return {"ref": ref, "kind": doc.get("kind"), "lines": results,
                "ok": all(r.get("ok") for r in results) if results else False}

    def reject(self, ref: str, *, reason: str, decided_by: str = "human"
               ) -> dict[str, Any]:
        """驳回单据：只改本地单据状态，不碰事件（事件由拍板席在内核侧拒绝）。"""
        note = str(reason or "").strip()
        if not note:
            raise TemplateError("INVALID_ARGUMENT",
                                "驳回必须写明原因（reason）——后续要据此查为什么没入库")
        doc = self._find(ref)
        if str(doc.get("status")) != "pending":
            raise TemplateError("WORKFLOW_STATE",
                                f"单据已裁决（{doc.get('status')}）：{ref}")
        self._log.set_status(ref, "rejected", decided_by=decided_by, reason=note)
        return {"ref": ref, "status": "rejected", "reason": note}

    # ---- 3) 读面 ----

    def documents(self, *, kind: str | None = None, status: str | None = None
                  ) -> dict[str, Any]:
        rows = []
        for doc in self._docs():
            if kind and doc.get("kind") != kind:
                continue
            if status and doc.get("status") != status:
                continue
            rows.append(self._summary(doc))
        rows.sort(key=lambda r: (str(r["occurredAt"]), str(r["ref"])))
        return {"rows": rows, "total": len(rows)}

    def document(self, ref: str) -> dict[str, Any]:
        doc = self._find(ref)
        out = self._summary(doc)
        out["lines"] = [dict(x) for x in doc["payload"]["lines"]]
        out["errors"] = [dict(x) for x in doc["payload"].get("errors") or []]
        out["caliber_note"] = ("数量面来源＝本板块已确认事实（不是外部 WMS 实时库存）；"
                               "未过门的事实不参与任何余额")
        return out

    # ---- 内部 ----

    def _submit(self, kind: str, *, lines: list[dict[str, Any]], ref: str | None,
                warehouse: str, to_warehouse: str | None = None,
                occurred_at: str | None = None, requested_by: str = "wh-ops",
                note: str | None = None) -> dict[str, Any]:
        wh = str(warehouse or "").strip()
        if not wh:
            raise TemplateError("INVALID_ARGUMENT", f"{_KIND_TITLES[kind]}缺仓库")
        if not lines:
            raise TemplateError("INVALID_ARGUMENT",
                                f"{_KIND_TITLES[kind]}至少需要一行")
        ref = str(ref or _ref({"receipt": "IR", "issue": "IO",
                               "transfer": "IT"}[kind],
                              f"{self._tenant}|{wh}|{uuid.uuid4().hex}"))
        existing = next((d for d in self._docs() if d.get("request_id") == ref), None)
        if existing is not None:  # 同单重放：幂等
            return self._summary(existing, duplicate=True)
        business_at = str(occurred_at or datetime.now(timezone.utc).isoformat())

        # 出库/调拨先整单核量（不足就一行都不写：避免半张单落了事实）
        if kind in ("issue", "transfer"):
            self._assert_enough(lines, wh, kind, idx_base=1)

        records: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for idx, raw in enumerate(lines, start=1):
            line = dict(raw or {})
            try:
                sku = self._sku(line, idx)
                qty = _dec(line.get("qty", line.get("quantity")),
                           f"数量（第 {idx} 行）")
                if qty <= 0:
                    raise TemplateError("INVALID_ARGUMENT",
                                        f"数量必须大于 0（第 {idx} 行）：{qty}")
                unit = str(line.get("unit") or "").strip() or UNKNOWN
                lot = str(line.get("lot") or "").strip() or UNKNOWN
            except TemplateError as exc:
                errors.append({"line": idx, "ok": False, "code": exc.code,
                               "message": exc.message})
                continue
            sides = (("out", wh, -qty, "goods_issued"),
                     ("in", str(to_warehouse), qty, "goods_received")) \
                if kind == "transfer" else \
                (("", wh, qty, "goods_received"),) if kind == "receipt" else \
                (("", wh, -qty, "goods_issued"),)
            for side, dim_wh, delta, event_type in sides:
                stable_id = f"{ref}:{idx}:{side or ('in' if delta > 0 else 'out')}"
                payload = {"sku": sku, "quantity": _fmt(abs(qty)), "unit": unit,
                           "warehouse": dim_wh or UNKNOWN, "lot": lot}
                if kind == "receipt":
                    # 可选字段只在真有值时写：成本随事实走（不另存成本表），
                    # 没带就是「不知道成本」——由计价口径决定该批能否暂估，
                    # 绝不在这里补 0（补零会一路污染结存金额与毛利）。
                    for opt in ("cost", "currency", "po_ref", "serial"):
                        if line.get(opt) not in (None, ""):
                            payload[opt] = str(line[opt])
                try:
                    out = self._kernel.submit_event(
                        event_type, stable_id, business_at, payload,
                        source_ref=ref)
                except Exception as exc:  # noqa: BLE001 — 协议边界
                    errors.append({**self._kernel_error(idx, exc), "side": side})
                    continue
                records.append({"line": idx, "side": side, "sku": sku,
                                "quantity": _fmt(abs(qty)),
                                "delta": _fmt(delta), "unit": unit,
                                "warehouse": dim_wh or UNKNOWN, "lot": lot,
                                "event_type": event_type,
                                "stable_id": stable_id,
                                "event_id": out.get("id"),
                                "event_status": out.get("status", "pending"),
                                "duplicate": bool(out.get("duplicate"))})
        return self._record(kind, ref, records, errors, business_at,
                            warehouse=wh, to_warehouse=to_warehouse,
                            requested_by=requested_by, note=note)

    def _record(self, kind: str, ref: str, lines: list[dict[str, Any]],
                errors: list[dict[str, Any]], occurred_at: str, *,
                warehouse: str, to_warehouse: str | None = None,
                requested_by: str = "wh-ops",
                note: str | None = None) -> dict[str, Any]:
        doc = {"schema": INV_SCHEMA, "tenant_id": self._tenant,
               "record": "inventory_document", "request_id": ref, "ref": ref,
               "kind": kind, "event": "submitted", "status": "pending",
               "warehouse": warehouse, "to_warehouse": to_warehouse,
               "occurred_at": occurred_at, "requested_by": requested_by,
               "payload": {"lines": lines, "errors": errors},
               "note": note or ("入库/出库/调拨/盘点落内核数量面拟稿事实；"
                                "拍板后由 project 投影与过账")}
        self._log.append(doc)
        out = self._summary(doc)
        return out

    def _summary(self, doc: dict[str, Any], *, duplicate: bool = False
                 ) -> dict[str, Any]:
        lines = doc["payload"]["lines"]
        errors = doc["payload"].get("errors") or []
        return {"ref": doc["request_id"], "kind": doc.get("kind"),
                "kindTitle": _KIND_TITLES.get(str(doc.get("kind")), str(doc.get("kind"))),
                "status": doc.get("status"), "warehouse": doc.get("warehouse"),
                "toWarehouse": doc.get("to_warehouse"),
                "occurredAt": doc.get("occurred_at"),
                "requestedBy": doc.get("requested_by"),
                "lineCount": len(lines), "errorCount": len(errors),
                "pendingLines": sum(1 for x in lines
                                    if str(x.get("event_status")) == "pending"),
                "duplicate": duplicate,
                "note": doc.get("note")}

    def _project_one(self, ref: str, item: dict[str, Any]) -> dict[str, Any]:
        base = {"line": item["line"], "side": item.get("side") or "",
                "stable_id": item["stable_id"], "sku": item["sku"]}
        try:
            balance = self._kernel.project_event(
                item["event_id"], "sku", item["sku"], item["delta"],
                item["unit"], {"warehouse": item["warehouse"],
                               "lot": item["lot"]})
        except Exception as exc:  # noqa: BLE001 — 内核 EVENT_STATE 等如实上抛
            err = self._kernel_error(int(item["line"]), exc)
            return {**base, "ok": False, **{k: err[k] for k in ("code", "message")},
                    "note": "投影失败：事实可能尚未过确认门（只有 accepted 才是事实）"}
        posting: dict[str, Any]
        try:
            posting = dict(self._kernel.post_event(item["event_id"]))
        except Exception as exc:  # noqa: BLE001
            posting = {"posted": False, "reason": "rule_missing_or_incomplete",
                       **self._kernel_error(int(item["line"]), exc)}
        return {**base, "ok": True,
                "delta": item["delta"],
                "dimension": {"warehouse": item["warehouse"], "lot": item["lot"]},
                "balance_after": (balance or {}).get("quantity"),
                "posted": bool(posting.get("posted")),
                "txn_id": posting.get("txn_id"),
                "posting_reason": posting.get("reason"),
                "posting_code": posting.get("code")}

    # ---- 小工具 ----

    @staticmethod
    def _sku(line: dict[str, Any], idx: int) -> str:
        sku = str(line.get("product_code") or line.get("product")
                  or line.get("productId") or "").strip()
        if not sku:
            raise TemplateError("INVALID_ARGUMENT",
                                f"第 {idx} 行缺商品编码（product_code/product/productId）")
        return sku

    def _available(self, sku: str, warehouse: str, lot: str) -> Decimal:
        try:
            out = self._kernel.query_object_balance(
                "sku", sku, {"warehouse": warehouse, "lot": lot}) or {}
        except Exception as exc:  # noqa: BLE001
            raise TemplateError("NOT_WIRED",
                                f"读不到数量面余额，无法核对可用量：{sku}",
                                detail={"sku": sku, "warehouse": warehouse,
                                        "lot": lot}) from exc
        return _dec(out.get("quantity") if out.get("quantity") is not None else "0",
                    "可用数量")

    def _assert_enough(self, lines: list[dict[str, Any]], warehouse: str,
                       kind: str, *, idx_base: int) -> None:
        """整单核量：按（sku, 仓库, 批次）汇总请求量，任一组超出即可整单拒绝。"""
        need: dict[tuple[str, str, str], tuple[Decimal, int]] = {}
        for idx, raw in enumerate(lines, start=idx_base):
            line = dict(raw or {})
            sku = self._sku(line, idx)
            qty = _dec(line.get("qty", line.get("quantity")),
                       f"数量（第 {idx} 行）")
            if qty <= 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"数量必须大于 0（第 {idx} 行）：{qty}")
            wh = str(line.get("warehouse") or warehouse).strip()
            lot = str(line.get("lot") or "").strip() or UNKNOWN
            key = (sku, wh, lot)
            prev, first = need.get(key, (Decimal(0), idx))
            need[key] = (prev + qty, first)
        for (sku, wh, lot), (qty, first) in need.items():
            have = self._available(sku, wh, lot)
            if qty > have:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"可用库存不足（第 {first} 行起）：{sku}@{wh} 可用 {_fmt(have)}，"
                    f"{_KIND_TITLES[kind]}需要 {_fmt(qty)}，缺口 {_fmt(qty - have)}",
                    detail={"sku": sku, "warehouse": wh, "lot": lot,
                            "available": _fmt(have), "requested": _fmt(qty),
                            "shortage": _fmt(qty - have), "line": first})

    @staticmethod
    def _kernel_error(idx: int, exc: Exception) -> dict[str, Any]:
        return {"line": idx, "ok": False,
                "code": str(getattr(exc, "code", type(exc).__name__)),
                "message": str(getattr(exc, "message", exc))}

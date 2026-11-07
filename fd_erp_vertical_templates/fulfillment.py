"""履约路径编排（add-supply-chain-vertical 任务 5.2 / spec「仓配与交付编排」）。

按商品行判断履约路径：
- 越库直发：供应商主档直发支持标识（payload.direct_ship + 可选直发商品清单）
  命中 → 生成直发指令记录（推送供应商门户由连接器执行，模板层仅留痕）；
- 否则仓内履约：多仓货源分配（按只读 WMS 快照分配）→ 仓级出库指令记录
  （执行归外部 WMS）。货源不足如实报短缺（fail-closed，不伪造可履约）。

运单轨迹 / 电子回单 / 签收回流：只读物流适配器接口 + DemoLogisticsAdapter
合成实现；回流 = 读适配器 + 模板层留痕记录，全单物流视图按订单聚合。

编排层零写 WMS、零写外部系统；与订单编排的移交点为 fulfillment_handoff 记录。
"""
from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog
from .masterdata import MasterData
from .wms_sync import WmsSnapshotAdapter

FULFILL_SCHEMA = "erp-supply-fulfillment/v1"


class LogisticsAdapter(ABC):
    """物流执行回流适配器接口：只读（轨迹/电子回单/签收）。

    真实承运商门户对接经萬星连接器/页面集（任务 5.3），本仓只消费读契约。
    """

    @abstractmethod
    def read_track(self, waybill_no: str) -> dict[str, Any]:
        """读运单轨迹（事件列表）。只读。"""
        ...

    @abstractmethod
    def read_pod(self, waybill_no: str) -> dict[str, Any]:
        """读电子回单/签收回执。只读。"""
        ...

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        ...


class DemoLogisticsAdapter(LogisticsAdapter):
    """合成物流适配器（测试/demo）：注入轨迹与回单数据，只读。"""

    def __init__(self, tracks: dict[str, list[dict[str, Any]]] | None = None,
                 pods: dict[str, dict[str, Any]] | None = None):
        self._tracks = tracks or {}
        self._pods = pods or {}

    def read_track(self, waybill_no: str) -> dict[str, Any]:
        events = self._tracks.get(str(waybill_no))
        if events is None:
            raise TemplateError("NOT_FOUND", f"运单轨迹不存在：{waybill_no}")
        return {"waybill_no": str(waybill_no), "events": [dict(e) for e in events]}

    def read_pod(self, waybill_no: str) -> dict[str, Any]:
        pod = self._pods.get(str(waybill_no))
        if pod is None:
            raise TemplateError("NOT_FOUND", f"电子回单不存在：{waybill_no}")
        return {"waybill_no": str(waybill_no), **dict(pod)}

    def describe(self) -> dict[str, Any]:
        return {"adapter": "logistics", "vendor": "demo-synthetic",
                "writes_external": False,
                "note": "合成物流适配器（测试/demo），只读轨迹/回单面；"
                        "真实承运商回流经页面集与连接器"}


def _ref(prefix: str, seed: str) -> str:
    return f"{prefix}-{hashlib.sha1(seed.encode()).hexdigest()[:10].upper()}"


class FulfillmentOrchestrator:
    """交付编排：直发/仓内两路径 + 运单回流留痕 + 全单物流视图。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, master: MasterData,
                 wms: WmsSnapshotAdapter, logistics: LogisticsAdapter):
        self._tenant = tenant_id
        self._master = master
        self._wms = wms
        self._logistics = logistics
        self._log = EventLog(Path(data_dir) / f"fulfillment-{tenant_id}.jsonl")
        # 履约移交记录由订单编排写入编排档（移交点跨模块只读回查）
        self._handoff_log = EventLog(Path(data_dir)
                                     / f"orchestration-{tenant_id}.jsonl")

    # ---- 事件档 ----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def _append(self, record: dict[str, Any]) -> None:
        self._log.append({"schema": FULFILL_SCHEMA, "tenant_id": self._tenant,
                          **record})

    def _get(self, ref: str) -> dict[str, Any]:
        hit = next((e for e in self._events() if e.get("ref") == ref), None)
        if hit is None:
            raise TemplateError("NOT_FOUND", f"履约记录不存在：{ref}")
        return hit

    def _latest(self, ref: str) -> dict[str, Any]:
        hits = [e for e in self._events() if e.get("ref") == ref]
        if not hits:
            raise TemplateError("NOT_FOUND", f"履约记录不存在：{ref}")
        return hits[-1]

    # ---- 1) 履约路径编排 ----

    def plan_fulfillment(self, handoff_ref: str, order_ref: str, customer_id: str,
                         lines: list[dict[str, Any]], *,
                         requested_by: str = "agent") -> dict[str, Any]:
        """按商品行编排履约路径（直发 / 多仓分配），生成指令记录（留痕）。

        - 直发判定来自供应商主档：payload.direct_ship=True 且（direct_ship_products
          为空=全量支持，或命中商品清单）；
        - 仓内：按 WMS 快照可用数量贪心分配（仓编码序确定性），货缺如实 shortage。
        """
        handoff = next((e for e in self._handoff_log.events()
                        if e.get("ref") == handoff_ref
                        and e.get("record") == "fulfillment_handoff"), None)
        if handoff is None:
            raise TemplateError("NOT_FOUND",
                                f"履约移交记录不存在：{handoff_ref}"
                                "（须先经订单编排 confirm_order 移交）")
        ref = _ref("FP", f"{self._tenant}|{handoff_ref}|{uuid.uuid4().hex}")
        snapshot = self._wms.read_inventory_snapshot()
        available: dict[tuple[str, str], Decimal] = {}
        for p in snapshot:
            if str(p.get("status") or "available") != "available":
                continue  # 冻结/锁定库位不参与分配
            key = (str(p.get("product_code")), str(p.get("warehouse")))
            available[key] = available.get(key, Decimal(0)) + Decimal(str(p.get("qty") or "0"))

        plan_lines: list[dict[str, Any]] = []
        for ln in lines:
            # 行键名兼容（2026-11-08 产上首现）：订单编排 confirm_order 交出的
            # handoff 载荷带的是**取价后的 `product` 键**，本函数此前只认
            # `product_code`，于是「确认 → 履约编排」这条标准流会拿空编码去查主档，
            # 报「商品未建档：（空）」——看着像数据缺失，实为两模块键名不一致。
            # 三种键都收（product_code 优先），仍缺则如实报 INVALID_ARGUMENT。
            code = str(ln.get("product_code") or ln.get("product")
                       or ln.get("productId") or "").strip()
            if not code:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"履约行缺商品编码（product_code/product/productId）：{ln!r}")
            qty = Decimal(str(ln.get("qty") or "0"))
            path = self._line_path(code)
            if path["path"] == "dropship":
                self._append({
                    "record": "dropship_instruction", "ref": _ref(
                        "DS", f"{self._tenant}|{ref}|{code}"),
                    "event": "created", "status": "recorded",
                    "payload": {"plan_ref": ref, "order_ref": order_ref,
                                "supplier_id": path["supplier_id"],
                                "portal": "supplier-portal",
                                "product_code": code, "qty": str(qty)},
                    "note": "直发指令记录：推送供应商门户经连接器执行，模板层仅留痕",
                    "requested_by": requested_by})
                plan_lines.append({"product_code": code, "qty": str(qty),
                                   "path": "dropship",
                                   "supplier_id": path["supplier_id"],
                                   "instruction": "dropship-instruction-recorded"})
            else:
                allocation: list[dict[str, Any]] = []
                remaining = qty
                for (pcode, wh) in sorted(available):
                    if pcode != code or remaining <= 0:
                        continue
                    take = min(available[(pcode, wh)], remaining)
                    if take <= 0:
                        continue
                    allocation.append({"warehouse": wh, "qty": str(take)})
                    available[(pcode, wh)] -= take
                    remaining -= take
                    self._append({
                        "record": "outbound_instruction", "ref": _ref(
                            "OB", f"{self._tenant}|{ref}|{code}|{wh}"),
                        "event": "created", "status": "recorded",
                        "payload": {"plan_ref": ref, "order_ref": order_ref,
                                    "warehouse": wh, "product_code": code,
                                    "qty": str(take)},
                        "note": "仓级出库指令记录：执行归外部 WMS，编排层零写 WMS",
                        "requested_by": requested_by})
                shortage = remaining if remaining > 0 else Decimal(0)
                plan_lines.append({
                    "product_code": code, "qty": str(qty), "path": "warehouse",
                    "allocations": allocation,
                    **({"shortage": str(shortage),
                        "note": "货源不足：如实短缺，不伪造可履约（补货后重排）"}
                       if shortage > 0 else {})})
        self._append({"record": "fulfillment_plan", "ref": ref,
                      "event": "created", "status": "open",
                      "payload": {"handoff_ref": handoff_ref, "order_ref": order_ref,
                                  "customer_id": str(customer_id),
                                  "lines": plan_lines},
                      "requested_by": requested_by,
                      "note": "履约路径编排完成：直发=指令留痕；仓内=多仓分配+出库指令留痕"})
        return {"ref": ref, "record": "fulfillment_plan", "lines": plan_lines,
                "order_ref": order_ref}

    def _line_path(self, product_code: str) -> dict[str, Any]:
        prod = self._master.find_by_code("product", product_code)
        if prod is None:
            raise TemplateError("NOT_FOUND", f"商品未建档：{product_code}")
        supplier_id = str(prod["payload"].get("supplier_id") or "")
        if supplier_id:
            supplier = self._master.get_entity(supplier_id)
            if supplier is not None and supplier["kind"] == "supplier" \
                    and supplier.get("state") == "active" \
                    and supplier["payload"].get("direct_ship") is True:
                whitelist = supplier["payload"].get("direct_ship_products") or []
                if not whitelist or product_code in [str(x) for x in whitelist]:
                    return {"path": "dropship", "supplier_id": supplier_id}
        return {"path": "warehouse"}

    # ---- 2) 运单回流（轨迹/回单/签收，只读适配器 + 留痕）----

    def record_waybill(self, plan_ref: str, waybill_no: str, carrier: str, *,
                       requested_by: str = "agent") -> dict[str, Any]:
        plan = self._latest(plan_ref)
        if plan.get("record") != "fulfillment_plan":
            raise TemplateError("INVALID_ARGUMENT", f"非履约计划：{plan_ref}")
        ref = _ref("WB", f"{self._tenant}|{plan_ref}|{waybill_no}")
        self._append({"record": "waybill", "ref": ref, "event": "created",
                      "status": "open",
                      "payload": {"plan_ref": plan_ref,
                                  "waybill_no": str(waybill_no),
                                  "carrier": str(carrier)},
                      "requested_by": requested_by,
                      "note": "运单登记（承运商页面集/门户回流）"})
        return {"ref": ref, "record": "waybill", "waybill_no": str(waybill_no)}

    def track_waybill(self, waybill_no: str) -> dict[str, Any]:
        """运单轨迹（只读适配器）。"""
        return self._logistics.read_track(waybill_no)

    def record_pod(self, waybill_no: str, *, requested_by: str = "agent"
                   ) -> dict[str, Any]:
        """电子回单/签收回流：读适配器 + 模板层留痕（append-only）。"""
        pod = self._logistics.read_pod(waybill_no)
        ref = _ref("POD", f"{self._tenant}|{waybill_no}|pod")
        self._append({"record": "pod", "ref": ref, "event": "received",
                      "status": "recorded", "payload": dict(pod),
                      "requested_by": requested_by,
                      "note": "电子回单/签收回流记录（读适配器 + 留痕；"
                              "签收触发收入确认经内核 proposal 车道，由账务面驱动）"})
        return {"ref": ref, "pod": pod}

    # ---- 2b) 承运商回流回执（CP 页面集）：写同一运单事件档，不建第二状态机 ----

    def _waybill(self, waybill_ref: str) -> dict[str, Any]:
        wb = self._latest(waybill_ref)
        if wb.get("record") != "waybill":
            raise TemplateError("INVALID_ARGUMENT", f"非运单记录：{waybill_ref}")
        return wb

    def _carrier_responded(self, waybill_ref: str) -> str | None:
        """承运商回执当前决定（accepted/rejected），未回执返回 None。"""
        for e in self._events():
            if e.get("ref") == waybill_ref \
                    and e.get("event") in ("carrier_accepted", "carrier_rejected"):
                return "accepted" if e["event"] == "carrier_accepted" else "rejected"
        return None

    def record_carrier_response(self, waybill_ref: str, decision: str, *,
                                note: str = "",
                                responded_by: str = "carrier-portal"
                                ) -> dict[str, Any]:
        """承运商接单回执：accepted/rejected；rejected 须附 note（可解释拒绝）。

        - rejected 为终态：后续月台/提货/签收车道一律拒绝（改派由租户侧重下
          运单承载，本层不改派不重建单）；
        - accepted 不改运单状态（open 流转继续）。
        """
        wb = self._waybill(waybill_ref)
        if decision not in ("accepted", "rejected"):
            raise TemplateError("INVALID_ARGUMENT",
                                "承运回执 decision 须为 accepted / rejected",
                                detail={"decision": decision})
        if decision == "rejected" and not str(note or "").strip():
            raise TemplateError("INVALID_ARGUMENT",
                                "拒绝承运须附 note（可解释拒绝纪律）")
        if str(wb.get("status") or "") != "open":
            raise TemplateError("WORKFLOW_STATE",
                                f"运单当前态不接受承运回执（{wb.get('status')}）:"
                                f"{waybill_ref}")
        if self._carrier_responded(waybill_ref) is not None:
            raise TemplateError("WORKFLOW_STATE",
                                f"承运回执已裁决（幂等回放由读面承担）：{waybill_ref}")
        self._append({"record": "waybill", "ref": waybill_ref,
                      "event": f"carrier_{decision}",
                      "status": "rejected" if decision == "rejected" else "open",
                      "payload": {"decision": decision, "note": str(note or ""),
                                  "responded_by": responded_by,
                                  "waybill_no": wb["payload"].get("waybill_no")},
                      "requested_by": responded_by,
                      "note": "承运商接单回执（CP 页面集回流）：写入运单同一事件档；"
                              "rejected 终态拒绝留痕，改派由租户侧重下运单"})
        return {"waybill_ref": waybill_ref, "decision": decision,
                "status": "rejected" if decision == "rejected" else "open",
                "responded_by": responded_by}

    def record_dock_reservation(self, waybill_ref: str, slot_id: str, *,
                                warehouse: str = "", slot_day: str = "",
                                responded_by: str = "carrier-portal"
                                ) -> dict[str, Any]:
        """月台预约留痕（CP 回流）：slotId 为仓库作业编排侧的不透明引用。

        月台时段主数据/可约时段计算归仓库作业编排（编排层未建模月台资源，
        本层只留预约回执）；改约=再次预约新时段，取消由 cancel_dock_reservation。
        """
        wb = self._waybill(waybill_ref)
        if str(wb.get("status") or "") != "open":
            raise TemplateError("WORKFLOW_STATE",
                                f"运单当前态不可预约月台（{wb.get('status')}）："
                                f"{waybill_ref}")
        if not str(slot_id or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "月台预约须带 slot_id")
        ref = _ref("DR", f"{self._tenant}|{waybill_ref}|{slot_id}"
                         f"|{uuid.uuid4().hex}")
        self._append({"record": "dock_reservation", "ref": ref,
                      "event": "created", "status": "reserved",
                      "payload": {"waybill_ref": waybill_ref,
                                  "waybill_no": wb["payload"].get("waybill_no"),
                                  "slot_id": str(slot_id),
                                  "warehouse": str(warehouse or ""),
                                  "slot_day": str(slot_day or ""),
                                  "responded_by": responded_by},
                      "requested_by": responded_by,
                      "note": "月台预约回执（CP 回流）：与仓库作业编排联动；"
                              "可约时段目录归仓库作业编排（未建模，如实挂账）"})
        return {"ref": ref, "status": "reserved", "slot_id": str(slot_id)}

    def cancel_dock_reservation(self, reservation_ref: str, *,
                                responded_by: str = "carrier-portal"
                                ) -> dict[str, Any]:
        """取消月台预约（同端点语义字段的取消车道）：留痕不改原记录。"""
        target = next((e for e in self._events()
                       if e.get("ref") == reservation_ref
                       and e.get("record") == "dock_reservation"
                       and e.get("status") == "reserved"), None)
        if target is None:
            raise TemplateError("NOT_FOUND",
                                f"可取消的月台预约不存在或已取消：{reservation_ref}")
        self._append({"record": "dock_reservation", "ref": reservation_ref,
                      "event": "cancelled", "status": "cancelled",
                      "payload": dict(target.get("payload") or {}),
                      "requested_by": responded_by,
                      "note": "月台预约取消（CP 回流留痕）"})
        return {"ref": reservation_ref, "status": "cancelled"}

    def record_pickup_checkin(self, waybill_ref: str, *, location: dict | None = None,
                              image_refs: list[str] | None = None,
                              scan_code: str = "",
                              responded_by: str = "carrier-portal"
                              ) -> dict[str, Any]:
        """提货打卡回执（CP 回流）：回执驱动在途状态。

        采集纪律：定位/影像均为可选采集（privacy-by-default），采集项在
        隐私声明过审后启用；本层只存引用，不收文件体/不存精确轨迹。
        """
        wb = self._waybill(waybill_ref)
        if str(wb.get("status") or "") != "open":
            raise TemplateError("WORKFLOW_STATE",
                                f"运单当前态不可提货打卡（{wb.get('status')}）："
                                f"{waybill_ref}")
        ref = _ref("PC", f"{self._tenant}|{waybill_ref}|{uuid.uuid4().hex}")
        self._append({"record": "pickup_checkin", "ref": ref,
                      "event": "created", "status": "recorded",
                      "payload": {"waybill_ref": waybill_ref,
                                  "waybill_no": wb["payload"].get("waybill_no"),
                                  "location": dict(location) if location else None,
                                  "image_refs": [str(i) for i in (image_refs or [])],
                                  "scan_code": str(scan_code or ""),
                                  "responded_by": responded_by},
                      "requested_by": responded_by,
                      "note": "提货打卡回执（CP 回流）：驱动在途状态；"
                              "定位/影像为可选采集（隐私声明过审后启用）"})
        return {"ref": ref, "status": "recorded",
                "waybill_no": wb["payload"].get("waybill_no")}

    def record_carrier_pod(self, waybill_ref: str, *, documents: list[str] | None = None,
                           exception_note: str = "",
                           responded_by: str = "carrier-portal"
                           ) -> dict[str, Any]:
        """承运商 POD 签收回执（CP 回流）：驱动运单结单与计费事件（计费同源）。

        异常签收（exception_note 非空）不结单：由调用方进处置车道留痕；
        签收触发收入确认经内核 proposal 车道，由账务面驱动（本层零落账）。
        """
        wb = self._waybill(waybill_ref)
        if str(wb.get("status") or "") != "open":
            raise TemplateError("WORKFLOW_STATE",
                                f"运单当前态不可签收上报（{wb.get('status')}）："
                                f"{waybill_ref}")
        no = str(wb["payload"].get("waybill_no") or "")
        ref = _ref("POD", f"{self._tenant}|{no}|carrier|{uuid.uuid4().hex}")
        self._append({"record": "pod", "ref": ref, "event": "reported",
                      "status": "recorded",
                      "payload": {"waybill_no": no,
                                  "waybill_ref": waybill_ref,
                                  "documents": [str(d) for d in (documents or [])],
                                  "exception_note": str(exception_note or ""),
                                  "reported_by": responded_by,
                                  "source": "carrier-report"},
                      "requested_by": responded_by,
                      "note": "承运商 POD 回执（CP 回流）：驱动运单结单与计费事件"
                              "（计费规则引擎同源）；异常签收进处置车道"})
        return {"ref": ref, "waybill_no": no,
                "exception": bool(str(exception_note or "").strip())}

    def carrier_receipts(self, waybill_ref: str) -> list[dict[str, Any]]:
        """运单关联回执当前态（月台/提货/POD，编排档同源 read 面）。

        同 ref 多事件（如月台预约→取消）取最新一条 = 当前态。
        """
        latest: dict[str, dict[str, Any]] = {}
        for e in self._events():
            payload = e.get("payload") or {}
            if e.get("record") in ("dock_reservation", "pickup_checkin", "pod") \
                    and payload.get("waybill_ref") == waybill_ref:
                latest[e.get("ref")] = {
                    "record": e.get("record"), "ref": e.get("ref"),
                    "event": e.get("event"), "status": e.get("status"),
                    "payload": dict(payload)}
        return list(latest.values())

    # ---- 3) 全单物流视图 ----

    def fulfillment_view(self, order_ref: str) -> dict[str, Any]:
        """全单物流视图：路径计划 + 指令 + 运单 + 轨迹 + 回单按订单聚合。"""
        plans = [self._latest(e["ref"]) for e in self._events()
                 if e.get("record") == "fulfillment_plan"
                 and e["event"] == "created"
                 and e["payload"].get("order_ref") == order_ref]
        if not plans:
            raise TemplateError("NOT_FOUND", f"订单无履约计划：{order_ref}")
        plan_refs = [p["ref"] for p in plans]
        view: list[dict[str, Any]] = []
        for pref in plan_refs:
            plan = self._latest(pref)
            waybills = [w for w in self._events()
                        if w.get("record") == "waybill" and w["event"] == "created"
                        and w["payload"].get("plan_ref") == pref]
            enriched = []
            for wb in waybills:
                no = wb["payload"]["waybill_no"]
                item = {"waybill_no": no, "carrier": wb["payload"]["carrier"]}
                try:
                    item["track_events"] = self._logistics.read_track(no)["events"]
                except TemplateError:
                    item["track_events"] = None  # 轨迹不可得如实置空，不伪造
                pods = [p for p in self._events()
                        if p.get("record") == "pod"
                        and p["payload"].get("waybill_no") == no]
                item["pod"] = pods[-1]["payload"] if pods else None
                enriched.append(item)
            dropships = [d for d in self._events()
                         if d.get("record") == "dropship_instruction"
                         and d["event"] == "created"
                         and d["payload"].get("plan_ref") == pref]
            outbounds = [o for o in self._events()
                         if o.get("record") == "outbound_instruction"
                         and o["event"] == "created"
                         and o["payload"].get("plan_ref") == pref]
            view.append({"plan_ref": pref, "lines": plan["payload"]["lines"],
                         "dropship_instructions": [
                             {"supplier_id": d["payload"]["supplier_id"],
                              "product_code": d["payload"]["product_code"],
                              "qty": d["payload"]["qty"]} for d in dropships],
                         "outbound_instructions": [
                             {"warehouse": o["payload"]["warehouse"],
                              "product_code": o["payload"]["product_code"],
                              "qty": o["payload"]["qty"]} for o in outbounds],
                         "waybills": enriched})
        return {"order_ref": order_ref, "plans": view,
                "caliber_note": "全单物流视图：编排留痕 + 只读适配器回流；"
                                "零外部写入"}

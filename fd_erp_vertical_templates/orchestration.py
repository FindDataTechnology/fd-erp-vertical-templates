"""采购到订单编排（add-supply-chain-vertical 任务 4.1/4.2/4.4）。

驱动形态：编排层只产生任务与留痕，执行在外部系统（供应商门户/WMS/金蝶），
账务事实全部经 KernelClient 工具面。

采购编排（4.1）：多来源采购需求归集 → 采购订单提案（模板确认车道）→
确认后推送指令记录（实际推送经萬星连接器到供应商门户，模板层仅留痕）→
到货预告 → 入库任务（任务单生成，执行归外部 WMS）。

订单受理编排（4.4）：信用校验（kernel query_credit；额度不足 fail-closed
拒单并携带额度明细解释）→ 取价引擎（pricing.resolve_price，规则链溯源）→
信用占用（kernel reserve_credit，账本记账）→ 订单提案（模板确认车道）→
确认后履约移交（handoff 到交付编排）。取消订单经 kernel release_credit 回冲。

工作流记录为业务单据（非账务事实）：JSONL append-only，按租户隔离；
审批走 pending/confirm 确认车道，拒绝留痕。
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog
from .kernel_client import KernelClient
from .masterdata import MasterData
from .pricing import PricingRuleBook

ORCH_SCHEMA = "erp-supply-orchestration/v1"


def _order_reservation_days() -> int:
    """订单占用的存续期（天）：模板层按单据类型声明，内核只校验上界。

    该值是"一张订单从受理到确认最多等多久"的业务口径，不是技术默认值——
    所以放在模板层并可由部署覆盖，而不是藏在内核里。
    """
    try:
        return max(1, int(os.environ.get("ERP_ORDER_RESERVATION_DAYS", "7")))
    except ValueError:
        return 7


def _dec(value: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what} 非法：{value!r}") from exc
    if d < 0:
        raise TemplateError("INVALID_ARGUMENT", f"{what} 不得为负：{value!r}")
    return d


def _fmt(d: Decimal) -> str:
    """定点字符串（免科学计数法：2E+2 → '200'）。"""
    return format(d.normalize(), "f")


#: 内核的「显式未知」一等值（与 fd_erp_ledger_mcp.events.UNKNOWN 同形）。
#: 到货预告不带库位/批次/单位时写它，而不是空串——空串会被读成「没有」
#: （内核按空值拒收必填字段），显式未知才是「此刻不知道」。
UNKNOWN = "__unknown__"


def _kernel_error(line: int, step: str, exc: Exception) -> dict[str, Any]:
    """把内核异常折成可回传的差错项（内存错误码在 `code` 属性上，不做类型依赖）。"""
    return {"line": line, "step": step,
            "code": getattr(exc, "code", None) or exc.__class__.__name__,
            "message": str(exc)[:500]}


def _ref(prefix: str, seed: str) -> str:
    return f"{prefix}-{hashlib.sha1(seed.encode()).hexdigest()[:10].upper()}"


class ProcurementOrchestrator:
    """采购编排：需求汇总 → 采购订单（确认车道）→ 推送留痕 → 到货预告 → 入库任务。"""

    def __init__(self, tenant_id: str, data_dir: str | Path,
                 master: MasterData, *, kernel: KernelClient | None = None,
                 requested_by_default: str = "agent"):
        self._tenant = tenant_id
        self._master = master
        self._log = EventLog(Path(data_dir) / f"orchestration-{tenant_id}.jsonl")
        self._default_by = requested_by_default
        # 可空是刻意的：模板层可独立用于「无内核」的工作流演练；此时到货事实
        # 只留编排记录，响应里如实写 `kernel.skipped=no_kernel`，不伪造库存与分录。
        self._kernel = kernel

    # ---- 事件档 ----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def _append(self, record: dict[str, Any]) -> None:
        self._log.append({"schema": ORCH_SCHEMA, "tenant_id": self._tenant,
                          **record})

    def _get(self, ref: str) -> dict[str, Any]:
        hit = next((e for e in self._events() if e.get("ref") == ref), None)
        if hit is None:
            raise TemplateError("NOT_FOUND", f"编排记录不存在：{ref}")
        return hit

    def _latest(self, ref: str) -> dict[str, Any]:
        """同 ref 多事件（状态推进）时的当前态 = 最新一条。"""
        hits = [e for e in self._events() if e.get("ref") == ref]
        if not hits:
            raise TemplateError("NOT_FOUND", f"编排记录不存在：{ref}")
        return hits[-1]

    def pending_requests(self) -> list[dict[str, Any]]:
        return [e for e in self._events() if e.get("status") == "pending"]

    def _confirm(self, request_id: str, decided_by: str) -> dict[str, Any]:
        events = self._events()
        target = next((e for e in events
                       if e.get("request_id") == request_id
                       and e.get("status") == "pending"), None)
        if target is None:
            raise TemplateError("NOT_FOUND", f"待确认请求不存在或已裁决：{request_id}")
        target["status"] = "confirmed"
        target["decided_by"] = decided_by
        target["decided_at_note"] = "confirmed-lane"
        self._log.rewrite(events)
        self._append({"record": target["record"], "ref": target["ref"],
                      "event": "confirmed", "status": "confirmed",
                      "payload": target.get("payload") or {},
                      "note": "确认车道拍板：提案生效", "decided_by": decided_by,
                      "request_id": request_id})
        return target

    # ---- 1) 需求汇总 ----

    def submit_demand(self, source: str, lines: list[dict[str, Any]],
                      *, requested_by: str | None = None) -> dict[str, Any]:
        """多来源采购需求归集（来源留痕：手工/补货/销售驱动等）。"""
        if not str(source or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "需求须带来源 source")
        if not lines:
            raise TemplateError("INVALID_ARGUMENT", "需求行不得为空")
        norm: list[dict[str, Any]] = []
        for ln in lines:
            code = str(ln.get("product_code") or "").strip()
            if not code:
                raise TemplateError("INVALID_ARGUMENT", "需求行缺 product_code")
            qty = _dec(ln.get("qty"), "需求数量")
            if qty <= 0:
                raise TemplateError("INVALID_ARGUMENT", "需求数量须 > 0")
            unit = str(ln.get("unit") or "").strip()
            norm.append({"product_code": code, "qty": str(qty),
                         "unit": unit, **({"note": ln["note"]} if ln.get("note") else {})})
        ref = _ref("DM", f"{self._tenant}|{source}|{uuid.uuid4().hex}")
        self._append({"record": "demand", "ref": ref, "event": "created",
                      "status": "open", "payload": {"source": source,
                                                    "lines": norm},
                      "requested_by": requested_by or self._default_by})
        return {"ref": ref, "record": "demand", "lines": len(norm),
                "note": "需求已归集（来源留痕）"}

    def aggregate_demand(self, demand_refs: list[str], *,
                         requested_by: str | None = None) -> dict[str, Any]:
        """需求按商品汇总并换算到采购单位（多单位换算经商品主档）。"""
        if not demand_refs:
            raise TemplateError("INVALID_ARGUMENT", "须给出待汇总的需求单")
        per_product: dict[str, dict[str, Any]] = {}
        for ref in demand_refs:
            d = self._get(ref)
            if d.get("record") != "demand" or d.get("event") != "created":
                raise TemplateError("WORKFLOW_STATE", f"需求单不可汇总（非 open）：{ref}")
            for ln in d["payload"]["lines"]:
                code = ln["product_code"]
                prod = self._master.find_by_code("product", code)
                if prod is None:
                    raise TemplateError("NOT_FOUND", f"商品未建档：{code}")
                units = {str(u["unit"]): u for u in prod["payload"]["units"]}
                unit = str(ln.get("unit") or "")
                purchase_unit = next((str(u["unit"]) for u in prod["payload"]["units"]
                                      if u["kind"] == "purchase"), None)
                if not unit:
                    unit = purchase_unit or next(
                        str(u["unit"]) for u in prod["payload"]["units"]
                        if u["kind"] == "base")
                if unit not in units:
                    raise TemplateError("INVALID_ARGUMENT",
                                        f"单位未建档：{unit}（商品 {code}）")
                qty_base = (Decimal(ln["qty"])
                            * Decimal(str(units[unit]["rate"])))
                qty_purchase = qty_base / Decimal(str(
                    units[purchase_unit]["rate"])) if purchase_unit else qty_base
                bucket = per_product.setdefault(code, {
                    "product_code": code,
                    "purchase_unit": purchase_unit,
                    "qty_purchase": Decimal(0), "demand_refs": []})
                bucket["qty_purchase"] += qty_purchase
                bucket["demand_refs"].append(ref)
        lines = [{"product_code": k, "purchase_unit": v["purchase_unit"],
                  "qty": _fmt(v["qty_purchase"].quantize(Decimal("0.000001"))),
                  "demand_refs": v["demand_refs"]}
                 for k, v in sorted(per_product.items())]
        agg_ref = _ref("AG", f"{self._tenant}|agg|{uuid.uuid4().hex}")
        self._append({"record": "demand_aggregate", "ref": agg_ref,
                      "event": "created", "status": "open",
                      "payload": {"demand_refs": list(demand_refs), "lines": lines},
                      "requested_by": requested_by or self._default_by})
        return {"ref": agg_ref, "record": "demand_aggregate", "lines": lines}

    # ---- 2) 采购订单提案（确认车道）----

    def propose_purchase_order(self, aggregate_ref: str, supplier_id: str, *,
                               requested_by: str | None = None) -> dict[str, Any]:
        """采购订单提案（pending，确认车道）：校验供应商与商品档案状态。"""
        agg = self._get(aggregate_ref)
        if agg.get("record") != "demand_aggregate" or agg.get("event") != "created":
            raise TemplateError("WORKFLOW_STATE",
                                f"汇总单不可再提案（非 open）：{aggregate_ref}")
        supplier = self._master.get_entity(str(supplier_id))
        if supplier is None or supplier["kind"] != "supplier":
            raise TemplateError("NOT_FOUND", f"供应商未建档：{supplier_id}")
        if supplier.get("state") == "frozen":
            raise TemplateError("MASTER_DATA_STATE",
                                f"供应商已冻结（证照管控或准入停用），不得下采购单：{supplier_id}")
        for ln in agg["payload"]["lines"]:
            prod = self._master.find_by_code("product", ln["product_code"])
            if prod is None:
                raise TemplateError("NOT_FOUND", f"商品未建档：{ln['product_code']}")
            if prod.get("state") not in (None, "active"):
                raise TemplateError("MASTER_DATA_STATE",
                                    f"商品非可采购状态（{prod.get('state')}）："
                                    f"{ln['product_code']}")
        ref = _ref("PO", f"{self._tenant}|{aggregate_ref}|{supplier_id}")
        self._append({"record": "purchase_order", "ref": ref,
                      "request_id": uuid.uuid4().hex,
                      "event": "proposed", "status": "pending",
                      "payload": {"supplier_id": str(supplier_id),
                                  "aggregate_ref": aggregate_ref,
                                  "lines": agg["payload"]["lines"]},
                      "requested_by": requested_by or self._default_by,
                      "note": "采购订单提案经确认车道：确认后生效，拒绝留痕"})
        return {"ref": ref, "request_id": self._latest(ref)["request_id"],
                "status": "pending", "supplier_id": str(supplier_id),
                "lines": agg["payload"]["lines"]}

    def confirm_purchase_order(self, request_id: str,
                               *, decided_by: str = "owner") -> dict[str, Any]:
        target = self._confirm(request_id, decided_by)
        po = self._latest(target["ref"])
        return {"ref": po["ref"], "status": po["status"],
                "supplier_id": po["payload"]["supplier_id"],
                "note": "采购订单已确认；推送供应商须另发推送指令（留痕）"}

    # ---- 3) 推送指令记录（实际推送经连接器，模板层仅留痕）----

    def record_push(self, po_ref: str, *, channel: str = "supplier-portal",
                    operator: str = "agent") -> dict[str, Any]:
        po = self._latest(po_ref)
        if po.get("record") != "purchase_order":
            raise TemplateError("INVALID_ARGUMENT", f"非采购订单：{po_ref}")
        if po.get("status") != "confirmed":
            raise TemplateError("WORKFLOW_STATE",
                                f"采购订单未确认不得推送（当前 {po.get('status')}）：{po_ref}")
        self._append({"record": "push", "ref": _ref(
                          "PS", f"{self._tenant}|{po_ref}|{channel}"),
                      "event": "created", "status": "recorded",
                      "payload": {"po_ref": po_ref, "channel": channel},
                      "note": "推送指令记录：实际推送经萬星连接器到供应商门户，"
                              "模板层仅留痕（外部写入不在模板层）",
                      "requested_by": operator})
        self._append({"record": "purchase_order", "ref": po_ref,
                      "event": "pushed", "status": "pushed",
                      "payload": {"channel": channel}})
        return {"po_ref": po_ref, "status": "pushed", "channel": channel,
                "note": "推送已留痕；实际推送由连接器执行"}

    def _po_status(self, po_ref: str) -> str:
        """采购单当前态（含供应商回执终态：accepted/rejected）。"""
        return str(self._latest(po_ref).get("status") or "")

    def record_supplier_response(self, po_ref: str, decision: str, *,
                                 note: str = "",
                                 responded_by: str = "supplier-portal"
                                 ) -> dict[str, Any]:
        """供应商接单回执（SP 页面集回流）：写入与采购单同一状态机（同 ref 事件档）。

        - decision ∈ accepted/rejected；rejected 须附 note（可解释拒绝纪律）；
        - 仅已推送（pushed）采购单可回执；accepted 后到货预告/发货反馈照常
          （回执驱动到货预告编排）；rejected 为终态，后续车道一律拒绝；
        - 不建第二状态机：回执是 purchase_order 记录上的事件。
        """
        po = self._latest(po_ref)
        if po.get("record") != "purchase_order":
            raise TemplateError("INVALID_ARGUMENT", f"非采购订单：{po_ref}")
        if decision not in ("accepted", "rejected"):
            raise TemplateError("INVALID_ARGUMENT",
                                "接单回执 decision 须为 accepted / rejected",
                                detail={"decision": decision})
        if decision == "rejected" and not str(note or "").strip():
            raise TemplateError("INVALID_ARGUMENT",
                                "拒绝接单须附 note（可解释拒绝纪律）")
        status = str(po.get("status") or "")
        if status != "pushed":
            raise TemplateError("WORKFLOW_STATE",
                                f"采购订单当前态不接受接单回执（{status}）：{po_ref}",
                                detail={"po_ref": po_ref, "status": status})
        self._append({"record": "purchase_order", "ref": po_ref,
                      "event": f"supplier_{decision}", "status": decision,
                      "payload": {"decision": decision, "note": str(note or ""),
                                  "responded_by": responded_by},
                      "requested_by": responded_by,
                      "note": "供应商接单回执（SP 页面集回流）：写入采购单同一状态机；"
                              "rejected 终态拒绝留痕，不驱动到货预告"})
        return {"po_ref": po_ref, "decision": decision, "status": decision,
                "responded_by": responded_by}

    def delivery_feedbacks(self, po_ref: str) -> list[dict[str, Any]]:
        """某采购单的发货反馈记录（编排档同源 read 面）。"""
        return [e for e in self._events()
                if e.get("record") == "delivery_feedback"
                and (e.get("payload") or {}).get("po_ref") == po_ref]

    def record_delivery_feedback(self, po_ref: str, lines: list[dict[str, Any]], *,
                                 carrier: str = "", waybill_no: str = "",
                                 documents: list[str] | None = None,
                                 expected_day: str | None = None,
                                 responded_by: str = "supplier-portal"
                                 ) -> dict[str, Any]:
        """供应商发货反馈（SP 页面集回流）：回执留痕 + 自动生成到货预告。

        - 行级发货明细必带（product_code + 正数 qty）；物流商/单号/凭证影像可选；
        - 与入库任务联动（编排面同源）：反馈自动生成到货预告，入库任务由到货
          预告驱动（租户侧车道），本方法不直接开任务；
        - 凭证影像件仅存引用（中台文件服务上传契约另行接入，本层不收文件体）。
        """
        po = self._latest(po_ref)
        if po.get("record") != "purchase_order":
            raise TemplateError("INVALID_ARGUMENT", f"非采购订单：{po_ref}")
        status = str(po.get("status") or "")
        if status not in ("pushed", "accepted"):
            raise TemplateError("WORKFLOW_STATE",
                                f"采购订单当前态不接受发货反馈（{status}）：{po_ref}",
                                detail={"po_ref": po_ref, "status": status})
        if not lines:
            raise TemplateError("INVALID_ARGUMENT", "发货反馈行不得为空")
        norm: list[dict[str, Any]] = []
        for ln in lines:
            # 行键名兼容（2026-11-08 跨入口实测）：小程序发货反馈页发的是
            # `{product, qty}`（pages/sp/delivery-feedback/index.tsx），本函数此前
            # 只认 `product_code` —— 真前端走这条路径必 400，而单测手工造的是
            # product_code 所以全绿（跨入口形状漂移，单侧测试是伪证）。
            code = str(ln.get("product_code") or ln.get("product")
                       or ln.get("productId") or "").strip()
            if not code:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"发货反馈行缺商品编码（product_code/product/productId）：{ln!r}")
            qty = _dec(ln.get("qty"), "发货数量")
            if qty <= 0:
                raise TemplateError("INVALID_ARGUMENT", "发货数量须 > 0")
            norm.append({"product_code": code, "qty": _fmt(qty),
                         "unit": str(ln.get("unit") or "")})
        ref = _ref("DF", f"{self._tenant}|{po_ref}|{uuid.uuid4().hex}")
        self._append({"record": "delivery_feedback", "ref": ref,
                      "event": "created", "status": "recorded",
                      "payload": {"po_ref": po_ref, "lines": norm,
                                  "carrier": str(carrier or ""),
                                  "waybill_no": str(waybill_no or ""),
                                  "documents": [str(d) for d in (documents or [])],
                                  "responded_by": responded_by},
                      "requested_by": responded_by,
                      "note": "供应商发货反馈（SP 页面集回流）：与入库任务联动"
                              "（到货预告自动生成）；凭证影像件仅存引用"})
        arrival = self.record_arrival_notice(po_ref, norm, expected_day=expected_day,
                                             requested_by=responded_by)
        return {"feedback_ref": ref, "po_ref": po_ref,
                "arrival_ref": arrival["ref"], "lines": norm,
                "kernel": arrival.get("kernel")}

    # ---- 4) 到货预告 ----

    def record_arrival_notice(self, po_ref: str, lines: list[dict[str, Any]], *,
                              expected_day: str | None = None,
                              requested_by: str | None = None) -> dict[str, Any]:
        po = self._latest(po_ref)
        if po.get("record") != "purchase_order" or po.get("status") \
                not in ("pushed", "accepted"):
            raise TemplateError("WORKFLOW_STATE",
                                f"采购订单未推送/未接单不得录到货预告：{po_ref}")
        if not lines:
            raise TemplateError("INVALID_ARGUMENT", "到货预告行不得为空")
        ref = _ref("AN", f"{self._tenant}|{po_ref}|{uuid.uuid4().hex}")
        self._append({"record": "arrival_notice", "ref": ref,
                      "event": "created", "status": "open",
                      "payload": {"po_ref": po_ref,
                                  "expected_day": expected_day,
                                  "lines": [dict(l) for l in lines]},
                      "requested_by": requested_by or self._default_by,
                      "note": "到货预告（供应商门户/页面集回流）"})
        # 编排记录先落盘：它是工作流真相（这单到货申报确实发生了）。事件层随后补，
        # 逐行幂等（stable_id = ref:行号），补失败也只会让这一行的 kernel 段报错，
        # 不会留下「有事件无单据」或「有单据却谎称有事件」两种残局。
        facts = self._emit_arrival_facts(ref, po_ref, lines,
                                         requested_by or self._default_by)
        return {"ref": ref, "record": "arrival_notice", "po_ref": po_ref,
                "lines": len(lines), "kernel": facts}

    # ---- 事件层：到货事实 → 对象数量面 → 过账（establish-erp-event-kernel 7.1）----

    def _emit_arrival_facts(self, ref: str, po_ref: str,
                            lines: list[dict[str, Any]],
                            requested_by: str) -> dict[str, Any]:
        """把到货预告逐行落成 `goods_received` 事件，投影库存并触发过账。

        三个口径决定（都写在这里，因为它们是「事实怎么进库」的关键）：

        1. **一行一事件，键是 `ref:行号`**：同一单同商品两行是两笔事实，不能用行内容
           做幂等键；键绑在到货预告 ref 上，重放同一 ref 由内核按
           (租户, 车道, stable_id) 去重，不会重复计数。
        2. **库位/批次/单位缺省写 `__unknown__`**：SP 到货预告不带库位（库位在入库
           任务才定），写空串会被内核读成「没有」并拒收必填字段；显式未知才是
           「此刻不知道」。库存按未知库位维度累计，读方能看到维度而不是看见一个假的库位。
        3. **过账权仍在核心**：金额与科目由内核的生效过账规则派生；无规则、或缺规则
           引用的载荷字段（到货预告通常没有成本金额）时，内核挂待补清单并抛
           `POSTING_RULE_MISSING`。这里如实记 `posted=False` 与原因，**不伪造已过账**。
        """
        if self._kernel is None:
            return {"ok": False, "skipped": "no_kernel", "lines": [],
                    "reason": "未注入 KernelClient：到货事实未落事件层，"
                              "库存数量面与分录都不会有这一笔"}
        out: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for idx, raw in enumerate(lines, start=1):
            line = dict(raw)
            sku = str(line.get("product_code") or line.get("product")
                      or line.get("productId") or "").strip()
            if not sku:
                errors.append({"line": idx, "code": "INVALID_ARGUMENT",
                               "message": f"到货行缺商品编码：{line!r}"})
                continue
            qty = _dec(line.get("qty"), f"到货数量（第 {idx} 行）")
            unit = str(line.get("unit") or "").strip() or UNKNOWN
            warehouse = str(line.get("warehouse") or "").strip() or UNKNOWN
            lot = str(line.get("lot") or "").strip() or UNKNOWN
            stable_id = f"{ref}:{idx}"
            payload = {"sku": sku, "quantity": _fmt(qty), "unit": unit,
                       "warehouse": warehouse, "lot": lot, "po_ref": po_ref}
            # 金额类可选字段只在到货行真的带了才写：没带就是「不知道金额」，
            # 过账规则引用 payload.cost 时内核会如实挂待补（不猜金额），
            # 而不是拿 0 顶上把账记平。
            for opt in ("cost", "currency"):
                if line.get(opt) not in (None, ""):
                    payload[opt] = str(line[opt])
            try:
                event = self._kernel.ingest_event(
                    "goods_received", stable_id, datetime.now(timezone.utc), payload,
                    source_ref=ref,
                    provenance={"source": "supplier-arrival-notice",
                                "arrival_ref": ref, "po_ref": po_ref,
                                "requested_by": requested_by})
            except Exception as exc:  # noqa: BLE001 — 协议边界：内存错误码不做类型依赖
                errors.append(_kernel_error(idx, "ingest_event", exc))
                continue
            balance = None
            try:
                balance = self._kernel.project_event(
                    event["id"], "sku", sku, _fmt(qty), unit,
                    {"warehouse": warehouse, "lot": lot})
            except Exception as exc:  # noqa: BLE001
                errors.append(_kernel_error(idx, "project_event", exc))
            posting: dict[str, Any]
            try:
                posting = dict(self._kernel.post_event(event["id"]))
            except Exception as exc:  # noqa: BLE001
                posting = {"posted": False, "reason": "rule_missing_or_incomplete",
                           **_kernel_error(idx, "post_event", exc)}
            out.append({"line": idx, "stable_id": stable_id,
                        "event_id": event["id"], "event_status": event.get("status"),
                        "duplicate": event.get("duplicate", False),
                        "quantity": _fmt(qty), "unit": unit,
                        "dimension": {"warehouse": warehouse, "lot": lot},
                        "balance": (balance or {}).get("quantity"),
                        "posted": posting.get("posted", False),
                        "txn_id": posting.get("txn_id"),
                        "posting_reason": posting.get("reason"),
                        "posting_code": posting.get("code"),
                        "posting_error": posting.get("message")})
        return {"ok": not errors, "lines": out, "errors": errors}


    # ---- 5) 入库任务（执行归外部 WMS）----

    def create_inbound_task(self, arrival_ref: str, warehouse: str, *,
                            requested_by: str | None = None) -> dict[str, Any]:
        an = self._get(arrival_ref)
        if an.get("record") != "arrival_notice":
            raise TemplateError("INVALID_ARGUMENT", f"非到货预告：{arrival_ref}")
        ref = _ref("IB", f"{self._tenant}|{arrival_ref}|{warehouse}")
        # 行键名兼容 + 缺键如实拒绝（2026-11-08）：到货预告把前端载荷原样存
        # （SP 页面集提交的是 {product, qty}），这里旧实现只读 product_code，
        # 会把商品编码**静默存成空串**——不是 400 而是脏数据，下游 WMS 对账才炸。
        lines = []
        for ln in an["payload"]["lines"]:
            code = str(ln.get("product_code") or ln.get("product")
                       or ln.get("productId") or "").strip()
            if not code:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"到货预告行缺商品编码（product_code/product/productId）：{ln!r}")
            lines.append({"product_code": code, "qty": str(ln.get("qty")),
                          "unit": str(ln.get("unit") or "")})
        self._append({"record": "inbound_task", "ref": ref,
                      "event": "created", "status": "open",
                      "payload": {"arrival_ref": arrival_ref,
                                  "warehouse": str(warehouse), "lines": lines},
                      "requested_by": requested_by or self._default_by,
                      "note": "入库任务单已生成：执行归外部 WMS（PDA/波次），"
                              "台账单向同步，编排层零写 WMS"})
        return {"ref": ref, "record": "inbound_task", "warehouse": str(warehouse),
                "lines": lines,
                "note": "入库任务已生成：执行归外部 WMS；"
                        "入库落账经 KernelClient proposal 车道（由账务面驱动）"}


class OrderOrchestrator:
    """订单受理编排：信用校验（fail-closed）→ 取价 → 信用占用 → 订单确认 → 履约移交。"""

    def __init__(self, tenant_id: str, data_dir: str | Path,
                 kernel: KernelClient, master: MasterData,
                 pricing: PricingRuleBook, *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._master = master
        self._pricing = pricing
        self._currency = base_currency
        self._log = EventLog(Path(data_dir) / f"orchestration-{tenant_id}.jsonl")

    # ---- 事件档（与采购共享编排档）----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def _append(self, record: dict[str, Any]) -> None:
        self._log.append({"schema": ORCH_SCHEMA, "tenant_id": self._tenant,
                          **record})

    def _latest(self, ref: str) -> dict[str, Any]:
        hits = [e for e in self._events() if e.get("ref") == ref]
        if not hits:
            raise TemplateError("NOT_FOUND", f"编排记录不存在：{ref}")
        return hits[-1]

    def pending_requests(self) -> list[dict[str, Any]]:
        return [e for e in self._events() if e.get("status") == "pending"]

    # ---- 受理 ----

    def accept_order(self, customer_id: str, lines: list[dict[str, Any]], day: str,
                     *, requested_by: str = "agent") -> dict[str, Any]:
        """订单受理：信用校验 → 取价 → 信用占用 → 订单提案（pending）。

        额度不足 fail-closed 拒单：返回 accepted=False + 额度明细解释
        （limit/ar_balance/reserved/available/requested/shortfall），不落订单、
        不占额度、不伪造可履约。
        """
        customer = self._master.get_entity(str(customer_id))
        if customer is None or customer["kind"] != "customer":
            raise TemplateError("NOT_FOUND", f"客户未建档：{customer_id}")
        if customer.get("state") == "frozen":
            raise TemplateError("MASTER_DATA_STATE",
                                f"客户已冻结，不得受理订单：{customer_id}")
        if not lines:
            raise TemplateError("INVALID_ARGUMENT", "订单行不得为空")

        # ---- 1) 取价（先价后信用，解释单携带金额）----
        priced: list[dict[str, Any]] = []
        total = Decimal(0)
        for ln in lines:
            code = str(ln.get("product_code") or "").strip()
            prod = self._master.find_by_code("product", code)
            if prod is None:
                raise TemplateError("NOT_FOUND", f"商品未建档：{code}")
            if prod.get("state") != "active":
                raise TemplateError("MASTER_DATA_STATE",
                                    f"商品非可售状态（{prod.get('state')}）：{code}")
            qty = _dec(ln.get("qty"), "订单数量")
            if qty <= 0:
                raise TemplateError("INVALID_ARGUMENT", "订单数量须 > 0")
            unit = str(ln.get("unit") or "")
            if unit:
                # 多单位下单：换算到销售单位计价留痕（qty 保留原单位）
                self._master.convert_qty(code, qty, unit, unit)
            quote = self._pricing.resolve_price(self._tenant, str(customer_id),
                                                code, day, qty)
            total += Decimal(quote["line_total"])
            priced.append({**quote, "unit": unit,
                           **({"note": ln["note"]} if ln.get("note") else {})})

        # ---- 2) 信用校验（fail-closed 可解释拒单）----
        # 「未登记额度」必须与「额度不足」区分（2026-11-08 产上首现）：此前
        # query_credit 回 NOT_FOUND（MCP 形态是 {ok:false,...} 错误壳，进程内形态
        # 抛 ErpError），下一行 `_dec(None)` 把它报成
        # `INVALID_ARGUMENT: 信用额度 limit 非法：None` —— 看着像调用方传错参数，
        # 实则是运维前置动作（set_credit_limit）没做。先判形态，给可执行的错误码。
        try:
            credit = self._kernel.query_credit(str(customer_id)) or {}
        except Exception as exc:  # 进程内形态：ErpError NOT_FOUND
            if getattr(exc, "code", None) != "NOT_FOUND":
                raise
            raise TemplateError(
                "CREDIT_NOT_REGISTERED",
                f"该客户未登记信用额度，不能受理订单：{customer_id}",
                detail={"customer_id": str(customer_id),
                        "remedy": "运维在内核登记信用额度（set_credit_limit）后重试"}) from exc
        if not isinstance(credit, dict) or credit.get("ok") is False \
                or credit.get("limit") in (None, ""):
            err = (credit.get("error") or {}) if isinstance(credit, dict) else {}
            if isinstance(credit, dict) and credit.get("ok") is False \
                    and err.get("code") not in (None, "NOT_FOUND"):
                raise TemplateError(str(err.get("code")),
                                    str(err.get("message") or "信用查询失败"))
            raise TemplateError(
                "CREDIT_NOT_REGISTERED",
                f"该客户未登记信用额度，不能受理订单：{customer_id}",
                detail={"customer_id": str(customer_id),
                        "remedy": "运维在内核登记信用额度（set_credit_limit）后重试"})
        limit = _dec(credit.get("limit"), "信用额度 limit")
        ar = _dec(credit.get("ar_balance"), "应收余额 ar_balance")
        reserved = _dec(credit.get("reserved"), "已占用 reserved")
        available = _dec(credit.get("available"), "可用额度 available")
        if total > available:
            return {
                "accepted": False, "stage": "credit_check", "order_ref": None,
                "explanation": {
                    "customer_id": str(customer_id),
                    "limit": _fmt(limit), "ar_balance": _fmt(ar),
                    "reserved": _fmt(reserved), "available": _fmt(available),
                    "requested_amount": _fmt(total.quantize(Decimal("0.000001"))),
                    "shortfall": _fmt((total - available)
                                     .quantize(Decimal("0.000001"))),
                    "currency": self._currency,
                    "note": "订单金额超出可用信用额度（额度 = limit - 应收 - 已占用），"
                            "fail-closed 拒单；回款或调额后可重新下单",
                },
            }

        # ---- 3) 信用占用（账本记账；stable_id 幂等）----
        # 失效时点由模板层按单据类型给出：内核行业无关，不知道一张订单该活多久。
        # 未声明的占用无法被识别为孤儿（change: close-orphan-credit-reservations）。
        order_seed = f"{self._tenant}|{customer_id}|{day}|{uuid.uuid4().hex}"
        order_ref = _ref("SO", order_seed)
        credit_stable = hashlib.sha1(
            f"credit-reserve|{self._tenant}|{order_ref}".encode()).hexdigest()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=_order_reservation_days())
        ).isoformat()
        reserve = self._kernel.reserve_credit(
            str(customer_id), credit_stable, _fmt(total.quantize(Decimal("0.000001"))),
            self._currency, source_ref=f"order:{order_ref}", expires_at=expires_at)

        # ---- 4) 订单提案（模板确认车道）----
        self._append({"record": "order", "ref": order_ref,
                      "request_id": uuid.uuid4().hex,
                      "event": "proposed", "status": "pending",
                      "payload": {
                          "customer_id": str(customer_id), "day": day,
                          "lines": priced,
                          "amount_total": _fmt(total.quantize(Decimal("0.000001"))),
                          "currency": self._currency,
                          "credit_reservation": {"stable_id": credit_stable,
                                                 "amount": _fmt(total.quantize(
                                                     Decimal("0.000001"))),
                                                 "kernel_result": reserve},
                      },
                      "requested_by": requested_by,
                      "note": "订单提案经确认车道：确认后履约移交"})
        return {"accepted": True, "order_ref": order_ref, "status": "pending",
                "request_id": self._latest(order_ref)["request_id"],
                "amount_total": _fmt(total.quantize(Decimal("0.000001"))),
                "currency": self._currency,
                "credit_reservation": {"stable_id": credit_stable,
                                       "amount": _fmt(total.quantize(Decimal("0.000001")))},
                "priced_lines": priced}

    def confirm_order(self, request_id: str, *,
                      decided_by: str = "owner") -> dict[str, Any]:
        """订单确认（确认车道）→ 履约移交记录（handoff 到交付编排）。"""
        events = self._events()
        target = next((e for e in events
                       if e.get("request_id") == request_id
                       and e.get("status") == "pending"), None)
        if target is None:
            raise TemplateError("NOT_FOUND", f"待确认请求不存在或已裁决：{request_id}")
        target["status"] = "confirmed"
        target["decided_by"] = decided_by
        target["decided_at_note"] = "confirmed-lane"
        self._log.rewrite(events)
        self._append({"record": "order", "ref": target["ref"],
                      "event": "confirmed", "status": "confirmed",
                      "payload": target.get("payload") or {},
                      "decided_by": decided_by, "request_id": request_id})
        order_ref = target["ref"]
        handoff_ref = _ref("FH", f"{self._tenant}|{order_ref}|handoff")
        self._append({"record": "fulfillment_handoff", "ref": handoff_ref,
                      "event": "created", "status": "open",
                      "payload": {"order_ref": order_ref,
                                  "lines": target["payload"]["lines"],
                                  "customer_id": target["payload"]["customer_id"]},
                      "note": "履约移交：交付编排（fulfillment）按商品行编排路径"})
        return {"order_ref": order_ref, "status": "confirmed",
                "handoff_ref": handoff_ref,
                "note": "订单确认并移交履约；收入/应收落账随交付确认经内核 proposal 车道"}

    def cancel_order(self, order_ref: str, reason: str, *,
                     requested_by: str = "agent") -> dict[str, Any]:
        """订单取消：信用释放（kernel release_credit 回冲）+ 取消留痕。"""
        po = self._latest(order_ref)
        if po.get("record") != "order" or po.get("status") not in ("pending", "confirmed"):
            raise TemplateError("WORKFLOW_STATE",
                                f"订单不可取消（当前 {po.get('status')}）：{order_ref}")
        res = po["payload"].get("credit_reservation") or {}
        release = self._kernel.release_credit(
            po["payload"]["customer_id"], res.get("stable_id"),
            released_by=requested_by, reason=f"order cancelled: {reason}")
        self._append({"record": "order", "ref": order_ref,
                      "event": "cancelled", "status": "cancelled",
                      "payload": {"reason": reason, "release": release},
                      "requested_by": requested_by,
                      "note": "订单取消：信用占用经内核 release_credit 回冲（账本记账）"})
        return {"order_ref": order_ref, "status": "cancelled",
                "credit_released": release}


# ---------------------------------------------------------------------------
# 售后编排（add-aftersale-lane）
# ---------------------------------------------------------------------------

#: 售后类型封闭集（契约 §6：退/换/补）。
AFTERSALE_KINDS = ("return", "exchange", "supplement")
#: 申请单状态集合。本期只产生 submitted；其余由处置面（运营侧）推进。
AFTERSALE_STATUSES = ("submitted", "accepted", "processing", "completed", "rejected")
#: 状态 → 给人看的一句话（详情里如实表达，不编造中间节点）。
_AFTERSALE_NOTES = {
    "submitted": "已受理，等待商家处置",
    "accepted": "商家已接单，等待处置",
    "processing": "处置中",
    "completed": "处置完成",
    "rejected": "已驳回（见驳回原因）",
}


class AfterSalesOrchestrator:
    """售后申请受理与进度查询（客户侧）。

    边界（design D5）：申请是**流程记录**，不是入库事实。受理路径不写事件层——
    客户提出申请时货还没回来，此时落 `goods_returned` 等于凭空造一次入库。
    数量事实属于处置结果，随处置面（运营侧，尚未落地）一起定义。
    """

    def __init__(self, tenant_id: str, data_dir: str | Path,
                 master: MasterData, orders: "OrderOrchestrator | None" = None, *,
                 requested_by_default: str = "ue"):
        self._tenant = tenant_id
        self._master = master
        self._orders = orders
        self._log = EventLog(Path(data_dir) / f"orchestration-{tenant_id}.jsonl")
        self._default_by = requested_by_default

    # ---- 事件档（与订单/采购共用一份）----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def _append(self, record: dict[str, Any]) -> None:
        self._log.append({"schema": ORCH_SCHEMA, "tenant_id": self._tenant,
                          **record})

    # ---- 订单归属（越域与不存在同码，不泄露存在性）----

    def _own_order(self, order_ref: str, customer_id: str) -> dict[str, Any]:
        """自有订单合并态；他人订单与不存在的订单抛同一条 NOT_FOUND。"""
        if self._orders is None:
            raise TemplateError("UPSTREAM_UNAVAILABLE",
                                "售后编排未接到订单编排：无法核对订单归属")
        hits = [e for e in self._orders._events()
                if e.get("record") == "order" and e.get("ref") == order_ref]
        if not hits:
            raise TemplateError("NOT_FOUND", f"订单不存在：{order_ref}")
        merged = dict(hits[0].get("payload") or {})
        if merged.get("customer_id") != customer_id:
            # 同一条错误、同一条文案：不区分「不存在」与「不是你的」。
            raise TemplateError("NOT_FOUND", f"订单不存在：{order_ref}")
        for ev in hits[1:]:
            if ev.get("event") in ("confirmed", "cancelled"):
                merged.update({k: v for k, v in (ev.get("payload") or {}).items()
                               if k in ("market_status", "cancelled_at")})
        merged["status"] = hits[-1].get("status")
        merged["event"] = hits[-1].get("event")
        return merged

    # ---- 申请号：可念、同租户内单调 ----

    def _next_ref(self) -> str:
        n = sum(1 for e in self._events() if e.get("record") == "aftersale") + 1
        prefix = "".join(ch for ch in self._tenant.upper() if ch.isalnum())[:8] or "TENANT"
        return f"AS-{prefix}-{n:06d}"

    # ---- 受理 ----

    def request_aftersale(self, customer_id: str, order_ref: str,
                          lines: list[dict[str, Any]], kind: str, *,
                          evidence: list[str] | None = None,
                          requested_by: str | None = None) -> dict[str, Any]:
        """受理售后申请：类型 → 订单归属 → 行与数量 → 落盘（design D3 的顺序）。

        数量上限按**订单行下单量**判（本期没有「已退多少」的回写口径），
        因此详情返回该行历史累计申请量，让重复申请可见而不是假装有合计上限。
        """
        kind_norm = str(kind or "").strip().lower()
        if kind_norm not in AFTERSALE_KINDS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"售后类型须为 {'/'.join(AFTERSALE_KINDS)}，实为 {kind!r}")
        if not isinstance(lines, list) or not lines:
            raise TemplateError("INVALID_ARGUMENT", "售后申请须至少给一条订单行")

        order = self._own_order(str(order_ref or "").strip(), customer_id)
        order_lines: dict[str, Decimal] = {}
        for ln in order.get("lines") or []:
            code = str(ln.get("product") or ln.get("product_code") or "").strip()
            if code:
                order_lines[code] = _dec(ln.get("qty"), f"订单行 {code} 的下单量")

        norm: list[dict[str, Any]] = []
        for idx, ln in enumerate(lines, start=1):
            code = str(ln.get("product") or ln.get("product_code")
                       or ln.get("productCode") or "").strip()
            if not code:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"第 {idx} 行缺商品编码（product/product_code）")
            if code not in order_lines:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"第 {idx} 行商品 {code} 不在订单 {order_ref} 里")
            qty = _dec(ln.get("qty"), f"第 {idx} 行数量")
            if qty <= 0:
                raise TemplateError("INVALID_ARGUMENT", f"第 {idx} 行数量须 > 0")
            ordered = order_lines[code]
            if qty > ordered:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"第 {idx} 行商品 {code} 申请 {_fmt(qty)} 超过下单量 {_fmt(ordered)}")
            norm.append({"product": code, "qty": _fmt(qty),
                         "unit": str(ln.get("unit") or "") or None})

        ref = self._next_ref()
        submitted_at = datetime.now(timezone.utc).isoformat()
        self._append({"record": "aftersale", "ref": ref,
                      "event": "requested", "status": "submitted",
                      "payload": {"order_ref": str(order_ref).strip(),
                                  "customer_id": str(customer_id),
                                  "kind": kind_norm, "lines": norm,
                                  "evidence": [str(x) for x in (evidence or [])],
                                  "submitted_at": submitted_at,
                                  "submitted_day": submitted_at[:10]},
                      "requested_by": requested_by or self._default_by,
                      "note": "售后申请受理（流程记录；不是入库事实）"})
        return {"requestId": ref, "orderId": str(order_ref).strip(),
                "kind": kind_norm, "lines": norm, "status": "submitted",
                "status_note": _AFTERSALE_NOTES["submitted"],
                "submitted_at": submitted_at,
                "next_action": "等待商家处置面推进（accepted→processing→completed）"}

    # ---- 读面 ----

    def _view(self, ev: dict[str, Any]) -> dict[str, Any]:
        payload = dict(ev.get("payload") or {})
        ref = ev.get("ref")
        status = ev.get("status") or "submitted"
        lines = [dict(x) for x in payload.get("lines") or []]
        for ln in lines:
            ln["applied_total"] = _fmt(self._applied_total(
                payload.get("order_ref"), ln.get("product")))
        return {"requestId": ref, "orderId": payload.get("order_ref"),
                "kind": payload.get("kind"), "lines": lines,
                "evidence": payload.get("evidence") or [],
                "status": status,
                "status_note": _AFTERSALE_NOTES.get(status, status),
                "submitted_at": payload.get("submitted_at")}

    def _applied_total(self, order_ref: str | None, product: str | None) -> Decimal:
        """该订单-商品的历史累计申请量（含本次；不含已驳回）。"""
        total = Decimal(0)
        for ev in self._events():
            if ev.get("record") != "aftersale":
                continue
            payload = ev.get("payload") or {}
            if payload.get("order_ref") != order_ref:
                continue
            if ev.get("status") == "rejected":
                continue
            for ln in payload.get("lines") or []:
                if ln.get("product") == product:
                    total += _dec(ln.get("qty"), "申请量")
        return total

    def aftersale_requests(self, customer_id: str, *, status: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """列表：只返回调用方自己的申请，按提交时刻倒序。"""
        rows: list[dict[str, Any]] = []
        for ev in self._events():
            if ev.get("record") != "aftersale":
                continue
            payload = ev.get("payload") or {}
            if payload.get("customer_id") != customer_id:
                continue
            if status and ev.get("status") != status:
                continue
            day = str(payload.get("submitted_day") or "")
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            rows.append(self._view(ev))
        rows.sort(key=lambda r: str(r.get("submitted_at") or ""), reverse=True)
        page = rows[offset: offset + limit]
        return {"rows": page, "total": len(rows), "offset": offset,
                "limit": limit, "returned": len(page)}

    def aftersale_of(self, customer_id: str, ref: str) -> dict[str, Any]:
        """详情：他人单号与不存在单号同一条 NOT_FOUND。"""
        hit = next((e for e in self._events()
                    if e.get("record") == "aftersale" and e.get("ref") == ref), None)
        if hit is None or (hit.get("payload") or {}).get("customer_id") != customer_id:
            raise TemplateError("NOT_FOUND", f"售后申请不存在：{ref}")
        view = self._view(hit)
        view["timeline"] = [{"status": hit.get("status"),
                             "at": (hit.get("payload") or {}).get("submitted_at"),
                             "note": _AFTERSALE_NOTES.get(
                                 str(hit.get("status")), str(hit.get("status")))}]
        view["next_action"] = "等待商家处置面推进（accepted→processing→completed）"
        return view


# ---------------------------------------------------------------------------
# CRM 线索与拜访编排（add-crm-leads-visits）
# ---------------------------------------------------------------------------

#: 线索状态封闭集。本期只产生 `new`；推进（following/converted/dropped）没有端点，
#: 也就不假装能产生——状态字段先落库，将来加推进不需要迁移数据。
CRM_LEAD_STATUSES = ("new", "following", "converted", "dropped")


class CrmOrchestrator:
    """SE 业务员台的线索与拜访车道。

    边界（design D5）：线索/拜访是**编排流程记录**，不是事件层事实。事件层对
    `lead` 系名字有黑名单（无载体即拒），而「业务员记了一条线索」与「公司获取了
    一条可计量的销售线索事实」载体、归属、计量口径都不同——本类是前者。

    归属（design D3）：一条记录永远带 `owner_staff`。查询只返回本人的；拜访的
    客户主体必须在自己名下（`sales_rep` 对得上），线索主体必须是本人录入的。
    不满足时与「不存在」返回同一条 NOT_FOUND，不泄露存在性。
    """

    def __init__(self, tenant_id: str, data_dir: str | Path, master: MasterData):
        self._tenant = tenant_id
        self._master = master
        self._log = EventLog(Path(data_dir) / f"orchestration-{tenant_id}.jsonl")

    # ---- 事件档（与订单/采购/售后共用一份）----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def _append(self, record: dict[str, Any]) -> None:
        self._log.append({"schema": ORCH_SCHEMA, "tenant_id": self._tenant,
                          **record})

    def _next_ref(self, record: str, prefix: str, staff: str) -> str:
        n = sum(1 for e in self._events()
                if e.get("record") == record
                and (e.get("payload") or {}).get("owner_staff") == staff) + 1
        tag = "".join(ch for ch in staff.upper() if ch.isalnum()) or "STAFF"
        return f"{prefix}-{tag}-{n:06d}"

    # ---- 归属校验 ----

    def _own_customer(self, staff: str, customer_id: str) -> dict[str, Any]:
        """名下客户；他人客户与不存在的客户抛同一条 NOT_FOUND（同文案）。"""
        snap = self._master.get_entity(str(customer_id or ""))
        if snap is None or snap.get("kind") != "customer" or \
                str((snap.get("payload") or {}).get("sales_rep") or "").strip() != staff:
            raise TemplateError("NOT_FOUND", f"客户不存在：{customer_id}")
        return snap

    def _own_lead(self, staff: str, lead_ref: str) -> dict[str, Any]:
        hit = next((e for e in self._events()
                    if e.get("record") == "lead" and e.get("ref") == lead_ref), None)
        if hit is None or \
                (hit.get("payload") or {}).get("owner_staff") != staff:
            raise TemplateError("NOT_FOUND", f"线索不存在：{lead_ref}")
        return hit

    # ---- 线索 ----

    def create_lead(self, staff: str, payload: dict[str, Any]) -> dict[str, Any]:
        """建线索：`name` 必填；状态只接受 `new`（推进没有车道，不假装有）。"""
        if not str(staff or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "缺业务员工号（线索必须归属到人）")
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            raise TemplateError("INVALID_ARGUMENT", "线索名称必填（name）")
        status = str((payload or {}).get("status") or "new").strip() or "new"
        if status not in CRM_LEAD_STATUSES:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"线索状态须为 {'/'.join(CRM_LEAD_STATUSES)}，实为 {status!r}")
        if status != "new":
            # 只有建线索这一个动作；推进要改状态，而改状态需要一条有页面的车道。
            # 允许直接建一条 following 线索，等于让调用方跳过那次真实拜访。
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"新建线索只能是 new（本期无线索状态推进车道）：{status}")

        ref = self._next_ref("lead", "LD", staff)
        created_at = datetime.now(timezone.utc).isoformat()
        rec = {"name": name, "owner_staff": str(staff),
               "created_at": created_at,
               "created_day": created_at[:10]}
        for key in ("phone", "company", "source", "note"):
            value = str((payload or {}).get(key) or "").strip()
            if value:
                rec[key] = value
        self._append({"record": "lead", "ref": ref, "event": "created",
                      "status": "new",
                      "payload": rec,
                      "requested_by": f"se:{staff}",
                      "note": "线索录入（流程记录；不写事件层）"})
        return {"leadId": ref, "status": "new", "name": name,
                "owner_staff": str(staff), "created_at": created_at}

    def leads_of(self, staff: str, *, status: str | None = None,
                 offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """本人线索池，按创建时刻倒序。"""
        rows: list[dict[str, Any]] = []
        for ev in self._events():
            if ev.get("record") != "lead":
                continue
            payload = ev.get("payload") or {}
            if payload.get("owner_staff") != staff:
                continue
            if status and ev.get("status") != status:
                continue
            rows.append({
                "leadId": ev.get("ref"), "status": ev.get("status"),
                "name": payload.get("name"),
                "phone": payload.get("phone"),
                "company": payload.get("company"),
                "source": payload.get("source"),
                "note": payload.get("note"),
                "created_at": payload.get("created_at"),
            })
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        page = rows[offset: offset + limit]
        return {"rows": page, "total": len(rows), "offset": offset,
                "limit": limit, "returned": len(page)}

    # ---- 拜访 ----

    def record_visit(self, staff: str, *, customer_id: str | None = None,
                     lead_ref: str | None = None, note: str = "",
                     location: str | None = None) -> dict[str, Any]:
        """拜访登记：纪要必填；客户/线索主体至少一个；定位只存调用方给的引用。

        定位（design D4）：设备侧采集要隐私声明过审，本类不采集、不推断、不补默认值。
        给了就原样存进 `location_ref`，没给就连这个键都不写——「没有位置」与
        「位置未知」在数据上必须可区分。
        """
        if not str(staff or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "缺业务员工号（拜访必须归属到人）")
        body = str(note or "").strip()
        if not body:
            raise TemplateError("INVALID_ARGUMENT",
                                "拜访纪要必填（这次见了谁、谈了什么）")
        cust = str(customer_id or "").strip()
        lead = str(lead_ref or "").strip()
        if not cust and not lead:
            raise TemplateError(
                "INVALID_ARGUMENT",
                "拜访须给出客户或线索（customerId / leadRef 至少一个）——"
                "没有对象的拜访无法归因")
        if cust:
            self._own_customer(staff, cust)   # 越域即 404
        if lead:
            self._own_lead(staff, lead)

        ref = self._next_ref("visit", "VS", staff)
        visited_at = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {"owner_staff": str(staff), "note": body,
                                  "visited_at": visited_at,
                                  "visited_day": visited_at[:10]}
        if cust:
            payload["customer_id"] = cust
        if lead:
            payload["lead_ref"] = lead
        loc = str(location or "").strip()
        if loc:
            payload["location_ref"] = loc   # 原样存引用；不解析、不补默认
        self._append({"record": "visit", "ref": ref, "event": "created",
                      "status": "recorded", "payload": payload,
                      "requested_by": f"se:{staff}",
                      "note": "拜访登记（流程记录；定位仅存调用方引用）"})
        out = {"visitId": ref, "status": "recorded", "note": body,
               "visited_at": visited_at,
               "customerId": cust or None, "leadRef": lead or None}
        if loc:
            out["location"] = loc
        return out

    def visits_of(self, staff: str, *, date_from: str | None = None,
                  date_to: str | None = None, offset: int = 0,
                  limit: int = 50) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for ev in self._events():
            if ev.get("record") != "visit":
                continue
            payload = ev.get("payload") or {}
            if payload.get("owner_staff") != staff:
                continue
            day = str(payload.get("visited_day") or "")
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            rows.append({
                "visitId": ev.get("ref"), "status": ev.get("status"),
                "note": payload.get("note"),
                "customerId": payload.get("customer_id"),
                "leadRef": payload.get("lead_ref"),
                "location": payload.get("location_ref"),
                "visited_at": payload.get("visited_at"),
            })
        rows.sort(key=lambda r: str(r.get("visited_at") or ""), reverse=True)
        page = rows[offset: offset + limit]
        return {"rows": page, "total": len(rows), "offset": offset,
                "limit": limit, "returned": len(page)}

    def today_visit_count(self, staff: str, day: str) -> int:
        """工作台「今日拜访」：与 visits_of 同一真源，不另算一遍。"""
        return sum(1 for ev in self._events()
                   if ev.get("record") == "visit"
                   and (ev.get("payload") or {}).get("owner_staff") == staff
                   and (ev.get("payload") or {}).get("visited_day") == day)

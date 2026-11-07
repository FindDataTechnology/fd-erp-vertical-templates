"""主数据三档（add-supply-chain-vertical 任务 2.1/2.2/2.3）。

客户/供应商/商品三主档 + 商品池，泛化 stores.py（餐饮门店主数据）的确认车道
模式：create_entity → pending → confirm_request 确认生效 / reject 留痕，
生命周期变更同样经确认车道，append-only 留痕。

边界定位（boundary 审计与本文档一致，沿 stores.py）：
- 主数据不是账本：不存任何金额余额/分录事实（商品的 base_price 是商品档案
  属性，不是账务事实）——账务真源仍是内核；
- 账务影响（如收货入库落账、销售出账）全部经 KernelClient proposal 车道。

三档字段约定：
- 商品：编码唯一、多单位（base/purchase/sales/warehouse + 对基本单位换算率）、
  条码、SKU 规格、供应商关联、商品池归属；
- 客户/供应商：编码唯一、证照列表（证照类型/到期日/状态）。
证照到期：expiring_certificates(within_days) 预警；过期联动建议
（商品→建议停售、供应商→建议冻结）经确认车道生效并留痕。
商品池：多级组织池（org_path 前缀树）+ 按客户/渠道授权可见可售
（授权变更同样走确认车道）+ 定制池（仅指定客户可见）。
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog

MASTER_SCHEMA = "erp-supply-master-data/v1"
#: 三档主数据 + 商品池（池是三档的可见性组织件，同车道管理）
MASTER_KINDS = ("product", "customer", "supplier", "product_pool")
UNIT_KINDS = ("base", "purchase", "sales", "warehouse")
POOL_VISIBILITY = ("org", "authorized", "custom")  # 组织内 / 授权 / 定制
CERT_STATUS = ("valid", "expired", "revoked")

#: 过期联动目标态：商品→停售；供应商→冻结
EXPIRY_LINKAGE_TARGET = {"product": "stop_selling", "supplier": "frozen"}

_PENDING = "pending"


def _today(value: str | None = None) -> date:
    if value is None:
        return date.today()
    return date.fromisoformat(str(value)[:10])


class MasterData:
    """三档主数据：确认车道 + append-only 留痕 + 证照预警联动 + 商品池授权。

    泛化自 stores.StoreMasterData（门店四类实体），同一 pending/confirm
    纪律；存储形态 JSON Lines（每行一个事件），文件按租户隔离。
    """

    def __init__(self, tenant_id: str, data_dir: str | Path):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"masterdata-{tenant_id}.jsonl")

    # ---- 事件档 ----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def _entity_snapshot(self, entity_id: str) -> dict[str, Any] | None:
        """按事件流回放实体当前态（create/update/lifecycle/authorize 全确认链）。"""
        snap: dict[str, Any] | None = None
        for ev in self._events():
            if ev.get("entity_id") != entity_id:
                continue
            status = ev.get("status")
            if ev["event"] == "create" and status == "confirmed":
                snap = {"entity_id": entity_id, "kind": ev["kind"],
                        "payload": dict(ev["payload"]),
                        "state": ev["payload"].get("initial_state", "active")}
            elif ev["event"] in ("update", "authorize") and status == "confirmed" and snap:
                snap["payload"].update(ev["payload"])
            elif ev["event"] == "lifecycle" and status == "confirmed" and snap:
                snap["state"] = ev["payload"]["state"]
        return snap

    # ---- 建档 / 变更（先 pending，确认生效）----

    def create_entity(self, kind: str, payload: dict[str, Any],
                      *, requested_by: str = "agent") -> dict[str, Any]:
        """三档/商品池建档提案（pending，待确认）。编码唯一性在确认时强校验。"""
        if kind not in MASTER_KINDS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"主数据 kind 须为 {MASTER_KINDS} 之一",
                                detail={"kind": kind})
        payload = dict(payload)
        if kind == "product_pool":
            self._validate_pool_payload(payload)
        else:
            self._validate_payload(kind, payload)
        code = str(payload.get("code") or "")
        if self.find_by_code(kind, code) is not None:
            raise TemplateError("INVALID_ARGUMENT",
                                f"{kind} 编码已存在（一次建档全平台共用）：{code}",
                                detail={"code": code, "kind": kind})
        record = {
            "schema": MASTER_SCHEMA, "event": "create",
            "request_id": uuid.uuid4().hex, "entity_id": uuid.uuid4().hex,
            "kind": kind, "tenant_id": self._tenant, "payload": payload,
            "status": _PENDING, "requested_by": requested_by,
        }
        self._log.append(record)
        return {"request_id": record["request_id"],
                "entity_id": record["entity_id"], "kind": kind,
                "status": _PENDING,
                "note": "主数据变更经确认车道：确认后生效，拒绝留痕"}

    def confirm_request(self, request_id: str, *, decided_by: str = "owner") -> dict[str, Any]:
        return self._decide(request_id, "confirmed", decided_by)

    def reject_request(self, request_id: str, reason: str,
                       *, decided_by: str = "owner") -> dict[str, Any]:
        return self._decide(request_id, "rejected", decided_by, reason)

    def _decide(self, request_id: str, decision: str, decided_by: str,
                reason: str | None = None) -> dict[str, Any]:
        target = self._log.set_status(request_id, decision,
                                      decided_by=decided_by, reason=reason)
        if target is None:
            raise TemplateError("NOT_FOUND", f"待确认请求不存在或已裁决：{request_id}")
        if decision == "confirmed" and target["event"] == "create":
            # 确认时刻重验编码唯一（两笔 pending 同码先后确认只留第一笔）
            code = str(target["payload"].get("code") or "")
            if target["kind"] != "product_pool" and self._code_taken_by_other(
                    target["kind"], code, target["entity_id"]):
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"确认冲突：{target['kind']} 编码已被占用：{code}")
            self._log.append({"event": "create", "status": "confirmed",
                              "entity_id": target["entity_id"],
                              "kind": target["kind"], "tenant_id": self._tenant,
                              "payload": target["payload"],
                              "decided_by": decided_by, "request_id": request_id})
        if decision == "confirmed" and target["event"] in ("update", "authorize",
                                                           "lifecycle"):
            self._log.append({**{k: v for k, v in target.items()
                                 if k not in ("request_id", "requested_by",
                                              "decided_by", "decided_at_note",
                                              "reason")},
                              "decided_by": decided_by, "request_id": request_id})
        audit = {"request_id": request_id, "decision": decision,
                 "decided_by": decided_by, "entity_id": target["entity_id"],
                 "event": target["event"]}
        if decision == "rejected":
            audit["note"] = "拒绝留痕：该请求不生效"
        return audit

    def update_entity(self, entity_id: str, changes: dict[str, Any],
                      *, requested_by: str = "agent") -> dict[str, Any]:
        """主数据变更提案（pending）。停售/冻结/停用实体不得直接改档案。"""
        snap = self.get_entity(entity_id)
        if snap is None:
            raise TemplateError("NOT_FOUND", f"主数据实体不存在：{entity_id}")
        if snap.get("state") in ("stop_selling", "frozen", "archived"):
            raise TemplateError(
                "MASTER_DATA_STATE",
                f"实体处于管控态 {snap.get('state')}，变更须先解除管控态：{entity_id}")
        record = {
            "schema": MASTER_SCHEMA, "event": "update",
            "request_id": uuid.uuid4().hex, "entity_id": entity_id,
            "kind": snap["kind"], "tenant_id": self._tenant,
            "payload": changes, "status": _PENDING, "requested_by": requested_by,
        }
        self._log.append(record)
        return {"request_id": record["request_id"], "entity_id": entity_id,
                "status": _PENDING}

    def set_lifecycle(self, entity_id: str, state: str,
                      *, requested_by: str = "agent",
                      linkage: dict[str, Any] | None = None) -> dict[str, Any]:
        """生命周期变更（pending 提案，确认后生效）。

        商品：active / stop_selling / archived；客户与供应商：active / frozen /
        archived。linkage 携带联动留痕（如证照过期触发）。
        """
        snap = self.get_entity(entity_id)
        if snap is None:
            raise TemplateError("NOT_FOUND", f"主数据实体不存在：{entity_id}")
        allowed = ("active", "stop_selling", "archived") \
            if snap["kind"] == "product" else ("active", "frozen", "archived")
        if snap["kind"] == "product_pool":
            allowed = ("active", "suspended", "archived")
        if state not in allowed:
            raise TemplateError("INVALID_ARGUMENT",
                                f"{snap['kind']} state 须为 {allowed} 之一",
                                detail={"state": state})
        if snap.get("state") == "archived" and state != "active":
            raise TemplateError("MASTER_DATA_STATE",
                                "已停用实体只能复用（active）")
        record = {
            "schema": MASTER_SCHEMA, "event": "lifecycle",
            "request_id": uuid.uuid4().hex, "entity_id": entity_id,
            "kind": snap["kind"], "tenant_id": self._tenant,
            "payload": {"state": state, **({"linkage": linkage} if linkage else {})},
            "status": _PENDING, "requested_by": requested_by,
        }
        self._log.append(record)
        return {"request_id": record["request_id"], "entity_id": entity_id,
                "state": state, "status": _PENDING}

    # ---- 校验 ----

    def _validate_payload(self, kind: str, payload: dict[str, Any]) -> None:
        if not str(payload.get("code") or "").strip():
            raise TemplateError("INVALID_ARGUMENT", f"{kind} 缺编码 code")
        if not str(payload.get("name") or "").strip():
            raise TemplateError("INVALID_ARGUMENT", f"{kind} 缺名称 name")
        if kind == "product":
            self._validate_product_units(payload)
            certs = payload.get("certificates") or []
            if not isinstance(certs, list):
                raise TemplateError("INVALID_ARGUMENT", "certificates 须为数组")
            for c in certs:
                self._validate_cert(c)
        elif kind in ("customer", "supplier"):
            for c in (payload.get("certificates") or []):
                self._validate_cert(c)

    def _validate_product_units(self, payload: dict[str, Any]) -> None:
        """多单位：基本/采购/销售/仓储单位 + 对基本单位的换算率（rate>0）。"""
        units = payload.get("units")
        if not isinstance(units, list) or not units:
            raise TemplateError("INVALID_ARGUMENT",
                                "商品须带多单位 units（base/purchase/sales/warehouse）")
        seen_names: set[str] = set()
        seen_kinds: set[str] = set()
        for u in units:
            if not isinstance(u, dict):
                raise TemplateError("INVALID_ARGUMENT", "units 元素须为对象")
            unit = str(u.get("unit") or "").strip()
            ukind = str(u.get("kind") or "").strip()
            if not unit or ukind not in UNIT_KINDS:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"单位项缺 unit 或 kind 非法（须 {UNIT_KINDS}）",
                                    detail={"unit": unit, "kind": ukind})
            if ukind in seen_kinds:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"单位 kind 重复：{ukind}（每类恰一个单位）")
            if unit in seen_names:
                raise TemplateError("INVALID_ARGUMENT", f"单位名重复：{unit}")
            try:
                rate = Decimal(str(u.get("rate")))
            except (InvalidOperation, TypeError) as exc:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"单位 {unit} 换算率非法：{u.get('rate')!r}") from exc
            if rate <= 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"单位 {unit} 换算率须 > 0（对基本单位）")
            if ukind == "base" and rate != 1:
                raise TemplateError("INVALID_ARGUMENT",
                                    "基本单位（base）换算率恒为 1")
            seen_names.add(unit)
            seen_kinds.add(ukind)
        if "base" not in seen_kinds:
            raise TemplateError("INVALID_ARGUMENT", "缺基本单位（kind=base, rate=1）")

    def _validate_cert(self, cert: dict[str, Any]) -> None:
        if not isinstance(cert, dict):
            raise TemplateError("INVALID_ARGUMENT", "证照项须为对象")
        if not str(cert.get("cert_type") or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "证照缺 cert_type（营业执照/生产许可/检验报告等）")
        exp = str(cert.get("expires_at") or "").strip()
        try:
            date.fromisoformat(exp[:10])
        except (ValueError, TypeError) as exc:
            raise TemplateError("INVALID_ARGUMENT",
                                f"证照 {cert.get('cert_type')} 到期日非法：{exp!r}（YYYY-MM-DD）") from exc
        status = str(cert.get("status") or "valid")
        if status not in CERT_STATUS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"证照状态须为 {CERT_STATUS}", detail={"status": status})

    def _validate_pool_payload(self, payload: dict[str, Any]) -> None:
        if not str(payload.get("name") or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "商品池缺名称 name")
        visibility = str(payload.get("visibility") or "").strip()
        if visibility not in POOL_VISIBILITY:
            raise TemplateError("INVALID_ARGUMENT",
                                f"商品池 visibility 须为 {POOL_VISIBILITY}",
                                detail={"visibility": visibility})
        org_path = str(payload.get("org_path") or "").strip()
        if not org_path or not org_path.startswith("/"):
            raise TemplateError("INVALID_ARGUMENT",
                                "商品池 org_path 须为多级组织路径（/ 分隔，如 /集团/华东/杭州）")
        if visibility == "custom" and not (payload.get("allowed_customers") or []):
            raise TemplateError("INVALID_ARGUMENT",
                                "定制池（custom）须指定 allowed_customers（仅指定客户可见）")

    # ---- 查询面 ----

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        return self._entity_snapshot(entity_id)

    def find_by_code(self, kind: str, code: str) -> dict[str, Any] | None:
        code = str(code or "").strip()
        if not code:
            return None
        for snap in self.list_entities(kind=kind):
            if str(snap["payload"].get("code") or "") == code:
                return snap
        return None

    def _code_taken_by_other(self, kind: str, code: str,
                             entity_id: str) -> bool:
        hit = self.find_by_code(kind, code)
        return hit is not None and hit["entity_id"] != entity_id

    def list_entities(self, kind: str | None = None,
                      state: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ev in self._events():
            if ev.get("event") == "create" and ev.get("status") == "confirmed":
                eid = ev["entity_id"]
                if eid in seen:
                    continue
                seen.add(eid)
                snap = self._entity_snapshot(eid)
                if snap is None:
                    continue
                if kind and snap.get("kind") != kind:
                    continue
                if state and snap.get("state") != state:
                    continue
                out.append(snap)
        return out

    def pending_requests(self) -> list[dict[str, Any]]:
        return [e for e in self._events() if e.get("status") == _PENDING]

    # ---- 多单位换算 ----

    def convert_qty(self, product_code: str, qty: str | int | float,
                    from_unit: str, to_unit: str) -> str:
        """跨单位换算：qty(from_unit) → 基本单位 → to_unit。失配如实报错。"""
        snap = self.find_by_code("product", product_code)
        if snap is None:
            raise TemplateError("NOT_FOUND", f"商品不存在：{product_code}")
        units = {str(u["unit"]): u for u in snap["payload"]["units"]}
        if from_unit not in units or to_unit not in units:
            raise TemplateError("INVALID_ARGUMENT",
                                f"单位未建档：{from_unit}→{to_unit}"
                                f"（商品 {product_code} 建档单位：{sorted(units)}）")
        try:
            q = Decimal(str(qty))
        except InvalidOperation as exc:
            raise TemplateError("INVALID_ARGUMENT", f"数量非法：{qty!r}") from exc
        base = q * Decimal(str(units[from_unit]["rate"]))
        out = base / Decimal(str(units[to_unit]["rate"]))
        return format(out.quantize(Decimal("0.000001")).normalize(), "f")

    # ---- 证照到期预警与联动 ----

    def expiring_certificates(self, within_days: int = 30, *, today: str | None = None
                              ) -> list[dict[str, Any]]:
        """证照到期预警：within_days 内到期 + 已过期，按紧迫度排序。"""
        ref = _today(today)
        warnings: list[dict[str, Any]] = []
        for snap in self.list_entities():
            for cert in snap["payload"].get("certificates") or []:
                if str(cert.get("status")) != "valid":
                    continue
                exp = date.fromisoformat(str(cert["expires_at"])[:10])
                days_left = (exp - ref).days
                if days_left < 0:
                    level = "expired"
                elif days_left <= within_days:
                    level = "expiring"
                else:
                    continue
                warnings.append({
                    "entity_id": snap["entity_id"], "kind": snap["kind"],
                    "code": snap["payload"].get("code"),
                    "name": snap["payload"].get("name"),
                    "cert_type": cert["cert_type"], "expires_at": str(exp),
                    "days_left": days_left, "level": level,
                })
        warnings.sort(key=lambda w: w["days_left"])
        return warnings

    def propose_expiry_control(self, entity_id: str, *, today: str | None = None,
                               requested_by: str = "agent") -> dict[str, Any]:
        """证照过期联动建议：商品→建议停售；供应商→建议冻结。

        建议以生命周期提案（pending）进入确认车道，linkage 留痕触发出处；
        确认后才生效 —— 不静默改状态，不绕确认车道。
        """
        snap = self.get_entity(entity_id)
        if snap is None:
            raise TemplateError("NOT_FOUND", f"主数据实体不存在：{entity_id}")
        kind = snap["kind"]
        target = EXPIRY_LINKAGE_TARGET.get(kind)
        if target is None:
            raise TemplateError("INVALID_ARGUMENT",
                                f"证照过期联动仅适用商品/供应商，不适用：{kind}")
        ref = _today(today)
        expired = [c for c in snap["payload"].get("certificates") or []
                   if str(c.get("status")) == "valid"
                   and date.fromisoformat(str(c["expires_at"])[:10]) < ref]
        if not expired:
            return {"entity_id": entity_id, "proposed": False,
                    "note": "无已过期证照，不需联动管控"}
        cert = min(expired, key=lambda c: c["expires_at"])
        req = self.set_lifecycle(
            entity_id, target, requested_by=requested_by,
            linkage={"trigger": "cert_expired", "cert_type": cert["cert_type"],
                     "expires_at": cert["expires_at"],
                     "note": "证照过期联动建议：确认后生效（确认车道留痕）"})
        return {"entity_id": entity_id, "proposed": True, "target_state": target,
                "request_id": req["request_id"], "status": _PENDING,
                "linkage": {"trigger": "cert_expired",
                            "cert_type": cert["cert_type"],
                            "expires_at": cert["expires_at"]},
                "note": "联动建议已入确认车道（pending）：确认后生效"}

    # ---- 商品池：多级组织 + 授权可见可售 + 定制池 ----

    def create_pool(self, payload: dict[str, Any],
                    *, requested_by: str = "agent") -> dict[str, Any]:
        """商品池建档提案（kind=product_pool，同确认车道）。"""
        return self.create_entity("product_pool", payload, requested_by=requested_by)

    def authorize_pool(self, pool_entity_id: str, *, customers: list[str] | None = None,
                       channels: list[str] | None = None,
                       requested_by: str = "agent") -> dict[str, Any]:
        """授权变更提案（event=authorize，确认车道）：池的可见可售范围变更。"""
        snap = self.get_entity(pool_entity_id)
        if snap is None or snap["kind"] != "product_pool":
            raise TemplateError("NOT_FOUND", f"商品池不存在：{pool_entity_id}")
        changes: dict[str, Any] = {}
        if customers is not None:
            changes["allowed_customers"] = sorted(set(
                list(snap["payload"].get("allowed_customers") or []) + list(customers)))
        if channels is not None:
            changes["allowed_channels"] = sorted(set(
                list(snap["payload"].get("allowed_channels") or []) + list(channels)))
        if not changes:
            raise TemplateError("INVALID_ARGUMENT", "授权变更须至少给出 customers 或 channels")
        record = {
            "schema": MASTER_SCHEMA, "event": "authorize",
            "request_id": uuid.uuid4().hex, "entity_id": pool_entity_id,
            "kind": "product_pool", "tenant_id": self._tenant,
            "payload": changes, "status": _PENDING, "requested_by": requested_by,
        }
        self._log.append(record)
        return {"request_id": record["request_id"], "entity_id": pool_entity_id,
                "status": _PENDING,
                "note": "授权变更经确认车道：确认后生效，拒绝留痕"}

    def visible_product_codes(self, *, customer_id: str | None = None,
                              channel_id: str | None = None,
                              org_node: str | None = None) -> list[str]:
        """按客户/渠道/组织节点求可见可售商品集（多池并集）。

        - org 池：org_node 在池 org_path 子树内即可见；
        - authorized 池：客户或渠道在授权名单内才可见；
        - custom 池：仅 allowed_customers 内客户可见。
        仅返回 active 池内 active 商品（停售/停用商品不进入可见可售集）。
        """
        visible: set[str] = set()
        for pool in self.list_entities(kind="product_pool", state="active"):
            p = pool["payload"]
            visibility = p.get("visibility")
            if visibility == "org":
                node = str(org_node or "")
                path = str(p.get("org_path") or "")
                if not node or not (node == path or node.startswith(path.rstrip("/") + "/")):
                    continue
            elif visibility == "authorized":
                if not ((customer_id and customer_id in (p.get("allowed_customers") or []))
                        or (channel_id and channel_id in (p.get("allowed_channels") or []))):
                    continue
            elif visibility == "custom":
                if not (customer_id and customer_id in (p.get("allowed_customers") or [])):
                    continue
            for code in p.get("product_codes") or []:
                prod = self.find_by_code("product", str(code))
                if prod is not None and prod.get("state") == "active":
                    visible.add(str(code))
        return sorted(visible)

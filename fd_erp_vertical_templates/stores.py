"""门店主数据（erp-vertical-restaurant-chain 任务 3.1）。

主数据 = 非账务参考数据（门店 / POS 终端 / 收款渠道商户号 / 外卖店铺）。
边界定位（boundary 审计与本文档一致）：
- 主数据不是账本：不存任何金额/余额/分录，不承担复式记账事实 —— 内核
  账务事实的唯一真源仍是 fd-erp-ledger-mcp；故主数据存模板层参考档案
  不构成「旁路账本」。
- 主数据变更经确认车道：变更先落 pending 记录，确认才生效，拒绝留痕；
  生命周期与审计痕迹 append-only。
- 账务影响（如开店后的日结落账）仍全部走内核 proposal 车道。

存储形态：JSON Lines 追加档（每行一个事件），文件按租户隔离。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .errors import TemplateError

STORE_SCHEMA = "erp-restaurant-store-master/v1"
ENTITY_KINDS = ("store", "pos_terminal", "payment_channel_account", "delivery_shop")
LIFECYCLE_STATES = ("preparing", "open", "suspended", "closed")
OPERATION_MODES = ("direct", "franchise")  # 直营 / 加盟

_PENDING = "pending"
_CONFIRMED = "confirmed"
_REJECTED = "rejected"


class StoreMasterData:
    """门店主数据档案：pending → confirmed/rejected 确认车道 + append-only 留痕。"""

    def __init__(self, tenant_id: str, data_dir: str | Path):
        self._tenant = tenant_id
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._dir / f"store-master-{tenant_id}.jsonl"

    # ---- 内部：事件流（append-only）----

    def _events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        out = []
        for line in self._events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _append(self, event: dict[str, Any]) -> None:
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _entity_snapshot(self, entity_id: str) -> dict[str, Any] | None:
        """按事件流回放实体当前态（payload 嵌套保留 + 生命周期 state）。"""
        snap: dict[str, Any] | None = None
        for ev in self._events():
            if ev.get("entity_id") != entity_id:
                continue
            if ev["event"] == "create" and ev.get("status") == _CONFIRMED:
                snap = {
                    "entity_id": entity_id,
                    "kind": ev["kind"],
                    "payload": dict(ev["payload"]),
                    "state": ev["payload"].get("state", "preparing"),
                }
            elif ev["event"] == "update" and ev.get("status") == _CONFIRMED and snap:
                snap["payload"].update(ev["payload"])
            elif ev["event"] == "lifecycle" and ev.get("status") == _CONFIRMED and snap:
                snap["state"] = ev["payload"]["state"]
        return snap

    # ---- 建档 / 变更（先 pending，确认生效）----

    def create_entity(self, kind: str, payload: dict[str, Any],
                      *, requested_by: str = "agent") -> dict[str, Any]:
        """新门店/终端/收款账号/外卖店铺建档提案（pending，待确认）。"""
        if kind not in ENTITY_KINDS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"主数据 kind 须为 {ENTITY_KINDS} 之一", detail={"kind": kind})
        if kind == "store":
            mode = payload.get("operation_mode")
            if mode not in OPERATION_MODES:
                raise TemplateError("INVALID_ARGUMENT",
                                    "门店须标注 operation_mode（direct=直营 / franchise=加盟）",
                                    detail={"operation_mode": mode})
        record = {
            "schema": STORE_SCHEMA,
            "event": "create",
            "request_id": uuid.uuid4().hex,
            "entity_id": uuid.uuid4().hex,
            "kind": kind,
            "tenant_id": self._tenant,
            "payload": payload,
            "status": _PENDING,
            "requested_by": requested_by,
        }
        self._append(record)
        return {"request_id": record["request_id"], "entity_id": record["entity_id"],
                "kind": kind, "status": _PENDING,
                "note": "主数据变更经确认车道：确认后生效，拒绝留痕"}

    def confirm_request(self, request_id: str, *, decided_by: str = "owner") -> dict[str, Any]:
        return self._decide(request_id, _CONFIRMED, decided_by)

    def reject_request(self, request_id: str, reason: str,
                       *, decided_by: str = "owner") -> dict[str, Any]:
        return self._decide(request_id, _REJECTED, decided_by, reason)

    def _decide(self, request_id: str, decision: str, decided_by: str,
                reason: str | None = None) -> dict[str, Any]:
        events = self._events()
        target = next((e for e in events
                       if e.get("request_id") == request_id and e.get("status") == _PENDING),
                      None)
        if target is None:
            raise TemplateError("NOT_FOUND",
                                f"待确认请求不存在或已裁决：{request_id}")
        target["status"] = decision
        target["decided_by"] = decided_by
        target["decided_at_note"] = "confirmed-lane"
        if reason:
            target["reason"] = reason
        self._rewrite(events)
        if decision == _CONFIRMED and target["event"] == "create":
            self._append({"event": "create", "status": _CONFIRMED,
                          "entity_id": target["entity_id"], "kind": target["kind"],
                          "tenant_id": self._tenant, "payload": target["payload"],
                          "decided_by": decided_by, "request_id": request_id})
        audit = {"request_id": request_id, "decision": decision, "decided_by": decided_by,
                 "entity_id": target["entity_id"], "event": target["event"]}
        if decision == _REJECTED:
            audit["note"] = "拒绝留痕：该请求不生效"
        return audit

    def update_entity(self, entity_id: str, changes: dict[str, Any],
                      *, requested_by: str = "agent") -> dict[str, Any]:
        """主数据变更提案（pending）。停业门店不得变更（先复业再改）。"""
        snap = self.get_entity(entity_id)
        if snap is None:
            raise TemplateError("NOT_FOUND", f"主数据实体不存在：{entity_id}")
        if snap.get("state") == "closed":
            raise TemplateError("MASTER_DATA_STATE",
                                f"门店已停业（closed），变更须先复业：{entity_id}")
        record = {
            "schema": STORE_SCHEMA, "event": "update",
            "request_id": uuid.uuid4().hex, "entity_id": entity_id,
            "kind": snap["kind"], "tenant_id": self._tenant,
            "payload": changes, "status": _PENDING, "requested_by": requested_by,
        }
        self._append(record)
        return {"request_id": record["request_id"], "entity_id": entity_id,
                "status": _PENDING}

    def set_lifecycle(self, entity_id: str, state: str,
                      *, requested_by: str = "agent") -> dict[str, Any]:
        """门店开/停业生命周期（pending 提案，确认后生效）。"""
        if state not in LIFECYCLE_STATES:
            raise TemplateError("INVALID_ARGUMENT",
                                f"state 须为 {LIFECYCLE_STATES} 之一", detail={"state": state})
        snap = self.get_entity(entity_id)
        if snap is None:
            raise TemplateError("NOT_FOUND", f"主数据实体不存在：{entity_id}")
        if snap.get("state") == "closed" and state != "open":
            raise TemplateError("MASTER_DATA_STATE",
                                "已停业门店只能复业（open），不得转入其它状态")
        record = {
            "schema": STORE_SCHEMA, "event": "lifecycle",
            "request_id": uuid.uuid4().hex, "entity_id": entity_id,
            "kind": snap["kind"], "tenant_id": self._tenant,
            "payload": {"state": state}, "status": _PENDING,
            "requested_by": requested_by,
        }
        self._append(record)
        return {"request_id": record["request_id"], "entity_id": entity_id,
                "state": state, "status": _PENDING}

    # ---- 查询面（对账驱动用）----

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        return self._entity_snapshot(entity_id)

    def list_entities(self, kind: str | None = None,
                      state: str | None = None,
                      operation_mode: str | None = None) -> list[dict[str, Any]]:
        out = []
        seen: set[str] = set()
        for ev in self._events():
            if ev.get("event") == "create" and ev.get("status") == _CONFIRMED:
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
                if operation_mode and snap.get("payload", {}).get(
                        "operation_mode") != operation_mode:
                    continue
                out.append(snap)
        return out

    def pending_requests(self) -> list[dict[str, Any]]:
        return [e for e in self._events() if e.get("status") == _PENDING]

    def _rewrite(self, events: list[dict[str, Any]]) -> None:
        """确认/拒绝只改状态字段：重写事件档（原始 payload 零改动）。"""
        tmp = self._events_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        os.replace(tmp, self._events_path)

"""口径册（change: add-valuation-policy-book 任务 1.x）。

**存在的理由**：成本怎么算、预算怎么控、资产怎么折、合并怎么并——这些都是**管理口径**，
可枚举、可校验、可留痕。把它们散在各模块里，会得到「一个概念两套默认值」：同一批数据
在不同模块按不同默认算，谁也说不出为什么。所以口径只在这里登记，模块只读不写。

四条纪律，每条都对着一个真实事故形态：

1. **键是封闭集**：未知键直接报错，不静默忽略。静默忽略会让「改了但没生效」成为常态，
   而财务口径上「同一次结账两次不同结果、且没人知道为什么」是致命的。
2. **默认值在册**：读取生效口径时每项都标 `source=default|configured`，不许调用方自己补默认。
3. **变更走确认车道并留痕**：append-only，可回放到任一历史时点——否则「上个月报表怎么算的」
   永远答不出来。
4. **没实现的引擎标 `recorded-only`**：口径可以先定下来，但必须显式标明「记下来了、还没有
   消费者」，不许让人以为它已经在管账（`implemented=False`）。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog

POLICY_SCHEMA = "fd-erp-vertical-templates/policy-book/v1"

#: 口径键登记表：键 → {domain, values, default, impact, implemented}
#:
#: `impact` 写「改这个键要付什么代价」，不是写它是什么——前者才是调用方需要知道的。
#: `implemented=False` 表示本版只有键与默认值、没有消费者（引擎按单交付），
#: 变更会被记为 `recorded-only`，不允许被当成已生效。
KEYS: dict[str, dict[str, Any]] = {
    # ---- 成本核算 ----
    "cost_method": {
        "domain": "cost-accounting",
        "values": ("weighted-average", "fifo", "batch-actual"),
        "default": "weighted-average",
        "impact": "影响出库成本、结存金额与毛利口径；切换只影响切换后的新出库，"
                  "已出库成本不重算（重算属重述，需单独提案）",
        "implemented": True,
    },
    "provisional_basis": {
        "domain": "cost-accounting",
        "values": ("reject", "last-cost", "standard"),
        "default": "reject",
        "impact": "影响没有成本凭据的入库能否计价；reject 会让消耗该批的出库直接失败，"
                  "last-cost/standard 会在成本层留下 basis 标记并在结账单列，供日后冲回",
        "implemented": True,
    },
    "variance_policy": {
        "domain": "cost-accounting",
        "values": ("to-cogs", "capitalize", "track-only"),
        "default": "track-only",
        "impact": "影响暂估/标准成本与实际成本之差去哪：进营业成本、按比例资本化进存货、"
                  "或只跟踪不结转（只跟踪会让差异长期留在账上，但不改报表金额）",
        "implemented": True,
    },
    "close_grain": {
        "domain": "cost-accounting",
        "values": ("monthly", "weekly"),
        "default": "monthly",
        "impact": "影响结账与差异结转频率；周结反馈快，但每周期末都要重算一次，对账工作量成倍",
        "implemented": True,
    },
    # ---- 预算 ----
    "budget_dimension": {
        "domain": "budget",
        "values": ("department", "project", "channel", "cost-center"),
        "default": "cost-center",
        "impact": "影响预算表的键与超支判定口径；与成本核算共用 cost-center 可省重复录入",
        "implemented": False,
    },
    "budget_period": {
        "domain": "budget",
        "values": ("monthly", "quarterly"),
        "default": "monthly",
        "impact": "影响预算分解与考核粒度；季度考核时月度对比报表需要插值",
        "implemented": False,
    },
    "budget_control": {
        "domain": "budget",
        "values": ("hard-block", "warn-only", "post-audit"),
        "default": "warn-only",
        "impact": "影响超预算时系统是拦、放行标记还是事后审计；硬拦需要预算先给足，"
                  "否则正常业务会被卡住",
        "implemented": False,
    },
    # ---- 固定资产 ----
    "depreciation_method": {
        "domain": "fixed-assets",
        "values": ("straight-line", "double-declining", "units-of-production"),
        "default": "straight-line",
        "impact": "影响每期折旧额与净值；双倍余额递减要处理最后两年改直线的切换，"
                  "工作量法要采集产量口径",
        "implemented": True,
    },
    "asset_ledger_owner": {
        "domain": "fixed-assets",
        "values": ("platform", "customer-readonly", "customer-writeback"),
        "default": "platform",
        "impact": "影响台账归属与是否需要碰客户资产系统；只读对账不写客户系统，"
                  "回写则需要客户系统开放写接口与回滚口径",
        "implemented": True,
    },
    # ---- 合并报表 ----
    "entity_model": {
        "domain": "consolidation",
        "values": ("single-tenant-multi-entity", "tenant-per-entity"),
        "default": "single-tenant-multi-entity",
        "impact": "影响账套隔离、权限与取数路径；单租户多主体要防串主体记账，"
                  "一主体一租户则合并要跨租户只读通道",
        "implemented": False,
    },
    "elimination_scope": {
        "domain": "consolidation",
        "values": ("intercompany-ar-ap", "plus-inventory-profit", "plus-equity"),
        "default": "intercompany-ar-ap",
        "impact": "影响抵销分录张数与对账工作量；含存货未实现利润需要内部毛利数据，"
                  "含权益则接近完整合并",
        "implemented": False,
    },
    "fx_translation": {
        "domain": "consolidation",
        "values": ("closing-rate", "temporal", "none"),
        "default": "none",
        "impact": "影响外币报表折算与折算差额去向；none 表示单一币种集团，"
                  "跨币种时会算错总额",
        "implemented": False,
    },
}

DOMAINS = ("cost-accounting", "budget", "fixed-assets", "consolidation")


def _ref() -> str:
    return f"POL-{hashlib.sha1(uuid.uuid4().hex.encode()).hexdigest()[:10].upper()}"


class PolicyBook:
    """口径册：登记、校验、确认生效与历史回放。"""

    def __init__(self, tenant_id: str, data_dir: str | Path):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"policy-{tenant_id}.jsonl")

    # ---- 读面 ----

    def defaults(self) -> dict[str, Any]:
        return {k: v["default"] for k, v in KEYS.items()}

    def current(self, *, before: str | None = None) -> dict[str, Any]:
        """生效口径：默认 + 已确认变更（`before` 给请求 id 时只回放到它之前）。

        返回每项的 `source`：`default`（册内默认）或 `configured`（已拍板生效）。
        未实现的引擎另标 `enforced=False`——口径记下来了，但还没有消费者。
        """
        values = self.defaults()
        source = {k: "default" for k in KEYS}
        for rec in self._confirmed(before=before):
            for key, value in (rec.get("changes") or {}).items():
                if key in KEYS:
                    values[key] = value
                    source[key] = "configured"
        keys = {k: {**{f: KEYS[k][f] for f in ("domain", "default", "impact")},
                    "values": list(KEYS[k]["values"]),
                    "implemented": KEYS[k]["implemented"],
                    "value": values[k], "source": source[k],
                    "enforced": KEYS[k]["implemented"]} for k in KEYS}
        return {"values": values, "source": source, "keys": keys,
                "counts": {"default": sum(1 for s in source.values() if s == "default"),
                           "configured": sum(1 for s in source.values() if s == "configured"),
                           "recordedOnly": sum(1 for k in KEYS if not KEYS[k]["implemented"])},
                "caliber_note": "口径唯一出处；未设置项取册内默认。"
                                "`enforced=false` 的口径已登记但引擎未交付，改它不会改变任何计算"}

    def explain(self, key: str) -> dict[str, Any]:
        meta = self._meta(key)
        return {"key": key, **{f: meta[f] for f in ("domain", "default", "impact")},
                "values": list(meta["values"]),
                "implemented": meta["implemented"]}

    def explain_all(self, *, domain: str | None = None) -> dict[str, Any]:
        rows = [self.explain(k) for k, m in KEYS.items()
                if domain is None or m["domain"] == domain]
        return {"rows": rows, "total": len(rows), "domains": list(DOMAINS)}

    def history(self) -> dict[str, Any]:
        rows = [dict(e) for e in self._records()]
        return {"rows": rows, "total": len(rows),
                "note": "append-only：提案/拍板/驳回各一条，按写入顺序；"
                        "`current(before=<request_id>)` 可回放任一时点口径"}

    # ---- 写面（确认车道）----

    def propose(self, changes: dict[str, Any], *, requested_by: str = "ops"
                ) -> dict[str, Any]:
        """提交口径变更提案（pending）。任何一项非法即整次拒绝，不做部分生效。"""
        clean = self._validate(changes)
        now = self.current()
        noop = {k: v for k, v in clean.items() if now["values"][k] == v}
        effective = {k: v for k, v in clean.items() if k not in noop}
        if not effective:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"变更与当前口径一致，没有可生效的内容：{sorted(noop)}",
                detail={"noop": sorted(noop)})
        recorded_only = sorted(k for k in effective if not KEYS[k]["implemented"])
        request_id = _ref()
        before = {k: now["values"][k] for k in effective}
        rec = {"schema": POLICY_SCHEMA, "tenant_id": self._tenant,
               "record": "policy_change", "request_id": request_id,
               "event": "propose", "status": "pending",
               "changes": effective, "before": before,
               "recorded_only": recorded_only,
               "requested_by": requested_by,
               "requested_at": datetime.now(timezone.utc).isoformat(),
               "note": ("口径变更提案：拍板后生效" +
                        ("；其中 %s 的引擎未交付，仅登记" % recorded_only
                         if recorded_only else ""))}
        self._log.append(rec)
        return {"requestId": request_id, "status": "pending",
                "changes": effective, "before": before,
                "recordedOnly": recorded_only,
                # 提案当下就把「这条口径还没有消费者」告诉调用方——写进历史才说等于没说。
                "note": rec["note"]}

    def confirm(self, request_id: str, *, decided_by: str = "owner") -> dict[str, Any]:
        actor = str(decided_by or "").strip()
        if not actor or actor in ("system", "billing-engine"):
            raise TemplateError("INVALID_ARGUMENT",
                                "口径变更不接受程序自批：decided_by 须为人工确认者标识")
        target = self._log.set_status(request_id, "confirmed", decided_by=actor)
        if target is None:
            raise TemplateError("NOT_FOUND",
                                f"口径变更提案不存在或已裁决：{request_id}")
        now = self.current()
        return {"requestId": request_id, "status": "confirmed",
                "decidedBy": actor, "changes": target.get("changes"),
                "recordedOnly": target.get("recorded_only") or [],
                "values": now["values"],
                "note": "已生效；`current(before=%s)` 仍可取变更前口径" % request_id}

    def reject(self, request_id: str, *, reason: str,
               decided_by: str = "owner") -> dict[str, Any]:
        note = str(reason or "").strip()
        if not note:
            raise TemplateError("INVALID_ARGUMENT",
                                "驳回必须写明原因（reason）——否则没人知道口径为什么没改")
        target = self._log.set_status(request_id, "rejected",
                                      decided_by=decided_by, reason=note)
        if target is None:
            raise TemplateError("NOT_FOUND",
                                f"口径变更提案不存在或已裁决：{request_id}")
        return {"requestId": request_id, "status": "rejected", "reason": note,
                "note": "生效口径未变"}

    # ---- 内部 ----

    def _records(self) -> list[dict[str, Any]]:
        return [e for e in self._log.events() if e.get("record") == "policy_change"]

    def _confirmed(self, *, before: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rec in self._records():
            if before is not None and rec.get("request_id") == before:
                return out  # 回放到该提案之前（不含它）
            if rec.get("status") == "confirmed":
                out.append(rec)
        if before is not None:
            raise TemplateError("NOT_FOUND", f"口径变更提案不存在：{before}",
                                detail={"requestId": before})
        return out

    @staticmethod
    def _meta(key: str) -> dict[str, Any]:
        meta = KEYS.get(key)
        if meta is None:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"未知口径键：{key}（口径键是封闭集，未知键不静默忽略）",
                detail={"unknown": [key], "allowed": sorted(KEYS)})
        return meta

    def _validate(self, changes: Any) -> dict[str, str]:
        if not isinstance(changes, dict) or not changes:
            raise TemplateError("INVALID_ARGUMENT",
                                "口径变更须为非空对象：{键: 值}",
                                detail={"received": str(changes)[:120]})
        unknown = sorted(k for k in changes if k not in KEYS)
        if unknown:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"未知口径键：{'、'.join(unknown)}（封闭集，未知键不静默忽略）",
                detail={"unknown": unknown, "allowed": sorted(KEYS)})
        clean: dict[str, str] = {}
        for key, value in changes.items():
            allowed = KEYS[key]["values"]
            if not isinstance(value, str):
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"口径 {key} 的取值必须是字符串，收到 {type(value).__name__}",
                    detail={"key": key, "value": str(value)[:80],
                            "allowed": list(allowed)})
            if value not in allowed:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"口径 {key} 不支持取值「{value}」",
                    detail={"key": key, "value": value, "allowed": list(allowed)})
            clean[key] = value
        return clean

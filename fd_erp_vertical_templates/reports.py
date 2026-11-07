"""报表 preset 运行面（add-supply-chain-vertical 任务 7.1/7.2）。

- 指标口径统一定义：preset 内每个指标 caliber 带 definition_source（单一
  出处）；报表与 AI 问答同源取该口径（本模块提供 preset 加载与口径校验）。
- 阈值预警 → 处置任务：阈值规则（毛利异常/库存积压/逾期应收）评估出预警，
  处置任务生成经确认车道派单（pending → confirm），拒绝留痕。

数据源 = 账本（KernelClient 只读面）+ 模板事件记录；本模块不做取数 SQL
（零直连库表），指标值由调用方按 preset 口径声明注入评估。
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog

PRESET_SCHEMA = "erp-report-preset/v1"
#: 处置任务可派席位（确认车道留痕字段）
DISPATCH_ROLES = ("watcher", "reconciler", "decider", "operator")


def load_report_preset(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """加载并校验报表 preset（路径或 dict）：schema/reports/metric_calibers/
    threshold_alerts 结构面校验（口径单一出处 + 阈值规则字段齐备）。"""
    body: dict[str, Any]
    if isinstance(source, dict):
        body = source
    else:
        try:
            body = json.loads(Path(source).read_text(encoding="utf-8"))
        except OSError as exc:
            raise TemplateError("MANIFEST_INVALID", f"报表 preset 不可读：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise TemplateError("MANIFEST_INVALID", f"报表 preset 不是合法 JSON：{exc}") from exc
    _validate(body)
    return body


def _validate(preset: dict[str, Any]) -> None:
    if not isinstance(preset, dict):
        raise TemplateError("MANIFEST_INVALID", "报表 preset 须为对象")
    if preset.get("schema") != PRESET_SCHEMA:
        raise TemplateError("MANIFEST_INVALID",
                            f"preset schema 须为 {PRESET_SCHEMA}",
                            detail={"found": preset.get("schema")})
    if not isinstance(preset.get("reports"), list) or not preset["reports"]:
        raise TemplateError("MANIFEST_INVALID", "preset 缺 reports")
    for i, rep in enumerate(preset["reports"]):
        if not isinstance(rep, dict) or not str(rep.get("key") or "").strip():
            raise TemplateError("MANIFEST_INVALID", f"reports[{i}] 缺 key")
        if not str(rep.get("caliber") or "").strip():
            raise TemplateError("MANIFEST_INVALID",
                                f"报表 {rep.get('key')} 缺取数口径声明 caliber")
    calibers = preset.get("metric_calibers")
    if not isinstance(calibers, list) or not calibers:
        raise TemplateError("MANIFEST_INVALID",
                            "preset 缺 metric_calibers（指标口径统一定义）")
    seen: set[str] = set()
    for c in calibers:
        key = str(c.get("key") or "").strip()
        if not key:
            raise TemplateError("MANIFEST_INVALID", "metric_calibers 元素缺 key")
        if key in seen:
            raise TemplateError("MANIFEST_INVALID",
                                f"指标口径重复定义（单一出处纪律）：{key}")
        seen.add(key)
        if not str(c.get("definition") or "").strip():
            raise TemplateError("MANIFEST_INVALID", f"指标 {key} 缺 definition")
        if not str(c.get("definition_source") or "").strip():
            raise TemplateError("MANIFEST_INVALID",
                                f"指标 {key} 缺定义出处 definition_source（单一出处）")
    alerts = preset.get("threshold_alerts")
    if not isinstance(alerts, list) or not alerts:
        raise TemplateError("MANIFEST_INVALID", "preset 缺 threshold_alerts（阈值预警规则）")
    for a in alerts:
        if not str(a.get("metric") or "").strip():
            raise TemplateError("MANIFEST_INVALID", "阈值预警缺 metric")
        if not str(a.get("threshold") or "").strip():
            raise TemplateError("MANIFEST_INVALID", "阈值预警缺 threshold")
        if not str(a.get("disposal_task") or "").strip():
            raise TemplateError("MANIFEST_INVALID", "阈值预警缺 disposal_task（处置任务模板）")


class ThresholdEvaluator:
    """阈值预警评估 + 处置任务派单（确认车道）。"""

    def __init__(self, tenant_id: str, data_dir: str | Path):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"disposal-{tenant_id}.jsonl")

    def evaluate_thresholds(self, preset: dict[str, Any],
                            values: dict[str, Any]) -> list[dict[str, Any]]:
        """按 preset.threshold_alerts 评估指标值 → 预警列表。

        规则形态（preset 声明）：{metric, threshold, op, disposal_task, severity}
        - op ∈ gt/lt（大于/小于阈值即触发，缺省 gt）；
        - threshold 数值或「x%」（百分比口径）；
        - values 键 = metric 键（值数值，或含 value 字段的 dict）。
        未提供值的指标如实标记 not_evaluated（不伪造触发，也不伪造平静）。
        """
        alerts: list[dict[str, Any]] = []
        for rule in preset.get("threshold_alerts") or []:
            metric = str(rule["metric"])
            raw = values.get(metric)
            if raw is None:
                alerts.append({"metric": metric, "triggered": False,
                               "not_evaluated": True,
                               "note": "指标值未提供：如实标记，不评估"})
                continue
            value = raw.get("value") if isinstance(raw, dict) else raw
            try:
                v = Decimal(str(value))
                t = self._threshold_value(rule["threshold"])
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"指标 {metric} 值或阈值非法：{value!r}/"
                                    f"{rule['threshold']!r}") from exc
            op = str(rule.get("op") or "gt")
            triggered = v > t if op == "gt" else v < t
            if triggered:
                alerts.append({
                    "metric": metric, "triggered": True,
                    "value": str(v), "threshold": str(rule["threshold"]), "op": op,
                    "severity": str(rule.get("severity") or "medium"),
                    "disposal_task": str(rule["disposal_task"]),
                })
        return alerts

    @staticmethod
    def _threshold_value(threshold: Any) -> "Decimal":
        text = str(threshold).strip()
        if text.endswith("%"):
            return Decimal(text[:-1])
        return Decimal(text)

    def raise_disposal_task(self, alert: dict[str, Any], *, title: str,
                            assignee: str = "operator",
                            requested_by: str = "agent") -> dict[str, Any]:
        """处置任务生成（经确认车道派单）：pending → confirm/reject 留痕。"""
        if assignee not in DISPATCH_ROLES:
            raise TemplateError("INVALID_ARGUMENT",
                                f"派单席位须为 {DISPATCH_ROLES}",
                                detail={"assignee": assignee})
        if not alert.get("triggered"):
            raise TemplateError("INVALID_ARGUMENT", "仅已触发的预警可派处置任务")
        request_id = uuid.uuid4().hex
        self._log.append({
            "schema": "erp-supply-disposal/v1", "record": "disposal_task",
            "request_id": request_id, "tenant_id": self._tenant,
            "status": "pending", "requested_by": requested_by,
            "payload": {"title": str(title), "assignee": assignee,
                        "metric": alert.get("metric"),
                        "value": alert.get("value"),
                        "threshold": alert.get("threshold"),
                        "severity": alert.get("severity"),
                        "task_template": alert.get("disposal_task")},
            "note": "处置任务经确认车道派单：确认后生效，拒绝留痕",
        })
        return {"request_id": request_id, "status": "pending",
                "metric": alert.get("metric"), "assignee": assignee}

    def confirm_disposal_task(self, request_id: str, *,
                              decided_by: str = "owner") -> dict[str, Any]:
        target = self._log.set_status(request_id, "confirmed", decided_by=decided_by)
        if target is None:
            raise TemplateError("NOT_FOUND", f"待确认处置任务不存在或已裁决：{request_id}")
        return {"request_id": request_id, "decision": "confirmed",
                "decided_by": decided_by,
                "note": "处置任务已派单生效（确认车道留痕）"}

    def reject_disposal_task(self, request_id: str, reason: str, *,
                             decided_by: str = "owner") -> dict[str, Any]:
        target = self._log.set_status(request_id, "rejected",
                                      decided_by=decided_by, reason=reason)
        if target is None:
            raise TemplateError("NOT_FOUND", f"待确认处置任务不存在或已裁决：{request_id}")
        return {"request_id": request_id, "decision": "rejected",
                "reason": reason, "note": "拒绝留痕：该处置任务不派发"}

    def pending_tasks(self) -> list[dict[str, Any]]:
        return [e for e in self._log.events() if e.get("status") == "pending"]

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        """处置任务清单（read 面）：按状态过滤（None=全部，含 pending/裁决态）。"""
        tasks = [e for e in self._log.events()
                 if e.get("record") == "disposal_task"]
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        return tasks

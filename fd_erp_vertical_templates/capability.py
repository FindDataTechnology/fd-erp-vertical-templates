"""电商能力矩阵（change `truth-ecommerce-capability-surface` 任务 1.1-1.3）。

矩阵回答一个问题：「这个平台、这条流，现在到底拿不拿得到」。它是电商能力
表述的**唯一真源**——报表腿、对账降级、供给清单、绑定状态与对外话术都从
它派生，不允许各处手写。

三条纪律（每条都对应一次真实踩坑）：

1. **三态，不是两态**。`available`（拿得到）/ `unavailable`（确认拿不到，
   如拼多多上游 schema 缺失）/ `unmapped`（可接但还没接，如淘宝 provider
   未实现、证据过期）。两态会把「还没接」说成「做不到」，或者反过来把
   「做不到」说成「等接入」——两种说法对客户都是假的。
2. **`available` 要两半证据**：provider 动作存在（动作面）+ 一次真实读取
   记录（runtime_read）。只有动作没读过，等于没验证过——这就是
   `measured_at: 2026-11-06` 那类声明能写进代码的原因。
3. **过期降级到 `unmapped`，不降到 `unavailable`**。过期是「不知道了」，
   不是「没有了」。

本模块零出网、零直连库表，只读 JSON 与本地时间。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .errors import TemplateError

MATRIX_SCHEMA = "ecommerce-capability-matrix/v1"

#: 三态（见模块 docstring 第 1 条）。
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNMAPPED = "unmapped"
STATUSES = (STATUS_AVAILABLE, STATUS_UNAVAILABLE, STATUS_UNMAPPED)

#: 流（对账腿的原子单位）。refunds 当前无模板消费，先立于矩阵以免将来补条目漏审。
FLOWS = ("orders", "settlements", "inventory", "refunds")

#: 复核窗缺省值（天）。平台 API 一年会滑出受支持版本；静态 schema 缺失属缓慢事实。
DEFAULT_REVIEW_WINDOW_DAYS = {
    "platform_api": 90,
    "catalog_snapshot": 90,
    "static_schema_missing": 365,
    "merchant_erp": 90,
}

#: 证据类型：available 必须同时有 provider_action 与 runtime_read。
EVIDENCE_PROVIDER_ACTION = "provider_action"
EVIDENCE_RUNTIME_READ = "runtime_read"
EVIDENCE_CATALOG = "catalog_entry"
EVIDENCE_DEPLOYMENT = "deployment"
EVIDENCE_REQUIRED_FOR_AVAILABLE = (EVIDENCE_PROVIDER_ACTION, EVIDENCE_RUNTIME_READ)

#: 报表腿 → 矩阵流（preset 生成与运行期解析共用）。
SECTION_FLOW_ORDERS = "orders"
SECTION_FLOW_SETTLEMENTS = "settlements"
SECTION_FLOW_INVENTORY = "inventory"


@dataclass(frozen=True)
class Evidence:
    kind: str
    ref: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"kind": self.kind, "ref": self.ref}
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class Cell:
    """一格：某平台某流的能力状态。"""

    platform: str
    flow: str
    status: str
    channel: str
    reason: str
    evidence: tuple[Evidence, ...] = ()
    measured_at: date | None = None
    review_window_days: int | None = None
    #: 降级标记：本次加载时由 available 降下来的，原因里保留原状态与降级理由。
    downgraded_from: str | None = None

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "channel": self.channel,
            "evidence": [e.as_dict() for e in self.evidence],
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
            "review_window_days": self.review_window_days,
            "reason": self.reason,
        }
        if self.downgraded_from:
            out["downgraded_from"] = self.downgraded_from
        return out


@dataclass
class CapabilityMatrix:
    """矩阵本体。字段对齐 JSON 资产；派生方法集中在下方。"""

    vertical: str
    updated_at: date
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_window_days: dict[str, int] = field(default_factory=dict)
    cells: dict[tuple[str, str], Cell] = field(default_factory=dict)
    source_path: Path | None = None

    # ---- 查询面 ----

    def platform_keys(self) -> tuple[str, ...]:
        return tuple(self.platforms.keys())

    def platform_meta(self, platform: str) -> dict[str, Any]:
        return self.platforms.get(platform, {})

    def cell(self, platform: str, flow: str) -> Cell:
        try:
            return self.cells[(platform, flow)]
        except KeyError:
            # 缺格不得默认可用（spec：矩阵缺格按 unmapped 处理并报出）。
            raise TemplateError(
                "NOT_FOUND", f"矩阵缺格：{platform}.{flow}",
                detail={"platform": platform, "flow": flow}) from None

    def status(self, platform: str, flow: str) -> str:
        return self.cell(platform, flow).status

    def available_flows(self, platform: str) -> tuple[str, ...]:
        return tuple(f for f in self._flows_of(platform)
                     if self.cells.get((platform, f), None) and
                     self.cells[(platform, f)].available)

    def unavailable_flows(self, platform: str) -> tuple[str, ...]:
        return tuple(f for f in self._flows_of(platform)
                     if (platform, f) in self.cells and
                     not self.cells[(platform, f)].available)

    def _flows_of(self, platform: str) -> tuple[str, ...]:
        declared = self.platforms.get(platform, {}).get("flows")
        if isinstance(declared, dict) and declared:
            return tuple(f for f in FLOWS if f in declared)
        return tuple(f for (p, f) in self.cells if p == platform)

    # ---- 对账面 ----

    def recon_shape(self, platform: str) -> int:
        """可对齐腿数（3 = 三方，2 = 两方，1 = 单方）。

        只数 orders / settlements / inventory 三腿——refunds 是差异来源不是腿。
        """
        legit = (SECTION_FLOW_ORDERS, SECTION_FLOW_SETTLEMENTS, SECTION_FLOW_INVENTORY)
        return sum(1 for f in legit if (platform, f) in self.cells
                   and self.cells[(platform, f)].available)

    def missing_legs(self, platform: str) -> list[dict[str, Any]]:
        """差异清单要带的 `capability_missing` 归因（逐腿，含矩阵引用）。"""
        out: list[dict[str, Any]] = []
        for flow in (SECTION_FLOW_ORDERS, SECTION_FLOW_SETTLEMENTS, SECTION_FLOW_INVENTORY):
            cell = self.cells.get((platform, flow))
            if cell is None:
                out.append({"kind": "capability_missing", "flow": flow,
                            "status": STATUS_UNMAPPED, "platform": platform,
                            "reason": "矩阵缺格（未登记）",
                            "matrix_ref": f"{platform}.{flow}"})
            elif not cell.available:
                out.append({"kind": "capability_missing", "flow": flow,
                            "status": cell.status, "platform": platform,
                            "reason": cell.reason,
                            "matrix_ref": f"{platform}.{flow}"})
        return out

    def capability_missing(self, platform: str, flow: str) -> dict[str, Any] | None:
        cell = self.cells.get((platform, flow))
        if cell is None:
            return {"kind": "capability_missing", "flow": flow,
                    "status": STATUS_UNMAPPED, "platform": platform,
                    "reason": "矩阵缺格（未登记）", "matrix_ref": f"{platform}.{flow}"}
        if cell.available:
            return None
        return {"kind": "capability_missing", "flow": flow, "status": cell.status,
                "platform": platform, "reason": cell.reason,
                "matrix_ref": f"{platform}.{flow}"}

    # ---- 报表面 ----

    def resolve_sections(self, sections: Iterable[dict[str, Any]],
                         platform: str) -> list[dict[str, Any]]:
        """把 preset 的 sections 按平台解析成可渲染形状。

        - 绑了 `leg_flow` 的段：可得 → 原样；不可得 → 保留位置并标
          `unavailable` + `unavailable_reason`（**不省略、不置 0**）。
        - 未绑 `leg_flow` 的段（差异汇总/映射看板）：永远渲染。
        """
        out: list[dict[str, Any]] = []
        for sec in sections:
            flow = sec.get("leg_flow")
            if not flow:
                out.append({**sec, "availability": {"status": STATUS_AVAILABLE,
                                                    "reason": ""}})
                continue
            cell = self.cells.get((platform, flow))
            if cell is not None and cell.available:
                out.append({**sec, "availability": {"status": STATUS_AVAILABLE,
                                                    "reason": ""}})
            else:
                status = cell.status if cell is not None else STATUS_UNMAPPED
                reason = cell.reason if cell is not None else "矩阵缺格（未登记）"
                out.append({
                    **sec,
                    "availability": {"status": status, "reason": reason,
                                     "matrix_ref": f"{platform}.{flow}"},
                    "unavailable_reason": reason,
                    "metrics": [],  # 不可得腿不给 metrics，避免被渲染成 0
                })
        return out

    # ---- 摘要与指纹 ----

    def summary(self) -> dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for cell in self.cells.values():
            counts[cell.status] += 1
        return counts

    def fingerprint(self) -> str:
        """产物 hash 的输入：只取判定结果（状态/原因/证据），不含 updated_at。"""
        payload = {
            "schema": MATRIX_SCHEMA,
            "vertical": self.vertical,
            "cells": [
                {"platform": p, "flow": f, **self.cells[(p, f)].as_dict()}
                for (p, f) in sorted(self.cells)
            ],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    def review_pending(self) -> list[dict[str, Any]]:
        """待补复核清单：unmapped 逐条给原因与建议动作。"""
        out: list[dict[str, Any]] = []
        for (platform, flow) in sorted(self.cells):
            cell = self.cells[(platform, flow)]
            if cell.status == STATUS_UNMAPPED:
                out.append({
                    "platform": platform, "flow": flow,
                    "reason": cell.reason,
                    "next_action": _next_action(cell),
                    "downgraded_from": cell.downgraded_from,
                })
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MATRIX_SCHEMA,
            "vertical": self.vertical,
            "updated_at": self.updated_at.isoformat(),
            "review_window_days": self.review_window_days,
            "platforms": [
                {
                    "key": key,
                    **{k: v for k, v in meta.items() if k != "flows"},
                    "flows": {f: self.cells[(key, f)].as_dict()
                              for f in FLOWS if (key, f) in self.cells},
                }
                for key, meta in self.platforms.items()
            ],
        }


def _next_action(cell: Cell) -> str:
    if cell.downgraded_from == STATUS_AVAILABLE:
        return "复核并补一次真实读取记录（动作在、证据过期或缺）"
    kinds = {e.kind for e in cell.evidence}
    if EVIDENCE_PROVIDER_ACTION in kinds and EVIDENCE_RUNTIME_READ not in kinds:
        return "做一次真实读取并留测量记录"
    if not kinds:
        return "在连接器目录立 provider 并实现只读动作"
    return "确认承载通道是否已部署，再补证据"


# ---------------------------------------------------------------- 加载与校验


def load_matrix(source: str | Path | dict[str, Any], *,
                today: date | None = None) -> CapabilityMatrix:
    """加载矩阵并施加降级规则。

    `today` 可注入（测试用），缺省取本地日期。
    """
    body, path = _read(source)
    today = today or date.today()
    _validate_structure(body, path)

    review_windows = {**DEFAULT_REVIEW_WINDOW_DAYS,
                      **(body.get("review_window_days") or {})}
    matrix = CapabilityMatrix(
        vertical=str(body["vertical"]),
        updated_at=_parse_date(body.get("updated_at"), path, "updated_at")
        or today,
        review_window_days=review_windows,
        source_path=path,
    )
    for entry in body["platforms"]:
        key = str(entry["key"])
        if key in matrix.platforms:
            raise TemplateError("MATRIX_INVALID", f"{path}: 平台重复：{key}")
        meta = {k: v for k, v in entry.items() if k != "flows"}
        meta["flows"] = entry.get("flows") or {}
        matrix.platforms[key] = meta
        for flow, raw in (entry.get("flows") or {}).items():
            cell = _parse_cell(key, flow, raw, review_windows, path)
            matrix.cells[(key, flow)] = _apply_downgrade(cell, today)
    return matrix


def _read(source: str | Path | dict[str, Any]) -> tuple[dict[str, Any], Path | None]:
    if isinstance(source, dict):
        return source, None
    path = Path(source)
    if path.is_dir():
        path = path / "capability-matrix.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except OSError as exc:
        raise TemplateError("MATRIX_INVALID", f"矩阵不可读：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise TemplateError("MATRIX_INVALID", f"矩阵不是合法 JSON：{exc}") from exc


def _validate_structure(body: dict[str, Any], path: Path | None) -> None:
    rel = str(path or "<inline>")
    if not isinstance(body, dict):
        raise TemplateError("MATRIX_INVALID", f"{rel}: 矩阵须为对象")
    if body.get("schema") != MATRIX_SCHEMA:
        raise TemplateError("MATRIX_INVALID",
                            f"{rel}: schema 须为 {MATRIX_SCHEMA}",
                            detail={"found": body.get("schema")})
    if not str(body.get("vertical") or "").strip():
        raise TemplateError("MATRIX_INVALID", f"{rel}: 缺 vertical")
    platforms = body.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        raise TemplateError("MATRIX_INVALID", f"{rel}: platforms 须为非空数组")
    for i, entry in enumerate(platforms):
        if not isinstance(entry, dict) or not str(entry.get("key") or "").strip():
            raise TemplateError("MATRIX_INVALID", f"{rel}: platforms[{i}] 缺 key")
        flows = entry.get("flows")
        if not isinstance(flows, dict) or not flows:
            raise TemplateError("MATRIX_INVALID",
                                f"{rel}: {entry.get('key')} 缺 flows")
        for flow in flows:
            if flow not in FLOWS:
                raise TemplateError(
                    "MATRIX_INVALID",
                    f"{rel}: {entry['key']} 的流名非法：{flow}（合法：{', '.join(FLOWS)}）")
    if not isinstance(body.get("review_window_days", {}), dict):
        raise TemplateError("MATRIX_INVALID", f"{rel}: review_window_days 须为对象")


def _parse_cell(platform: str, flow: str, raw: Any,
                review_windows: dict[str, int], path: Path | None) -> Cell:
    rel = str(path or "<inline>")
    where = f"{rel}: {platform}.{flow}"
    if not isinstance(raw, dict):
        raise TemplateError("MATRIX_INVALID", f"{where}: 格须为对象")
    status = raw.get("status")
    if status not in STATUSES:
        raise TemplateError("MATRIX_INVALID",
                            f"{where}: status 须为 {'/'.join(STATUSES)}",
                            detail={"found": status})
    channel = str(raw.get("channel") or "").strip()
    if not channel:
        raise TemplateError("MATRIX_INVALID", f"{where}: 缺 channel")
    reason = str(raw.get("reason") or "").strip()
    if status != STATUS_AVAILABLE and not reason:
        raise TemplateError("MATRIX_INVALID",
                            f"{where}: 非 available 必须写 reason（对外要能解释）")
    evidence: list[Evidence] = []
    for i, e in enumerate(raw.get("evidence") or []):
        if not isinstance(e, dict) or not str(e.get("kind") or "").strip() \
                or not str(e.get("ref") or "").strip():
            raise TemplateError("MATRIX_INVALID",
                                f"{where}: evidence[{i}] 需 kind + ref")
        evidence.append(Evidence(kind=str(e["kind"]), ref=str(e["ref"]),
                                 note=str(e.get("note") or "")))
    window = raw.get("review_window_days")
    if window is None:
        kind = str(raw.get("channel_kind") or "platform_api")
        window = review_windows.get(kind, review_windows.get("platform_api", 90))
    if not isinstance(window, int) or window <= 0:
        raise TemplateError("MATRIX_INVALID",
                            f"{where}: review_window_days 须为正整数")
    return Cell(
        platform=platform, flow=flow, status=status, channel=channel,
        reason=reason, evidence=tuple(evidence),
        measured_at=_parse_date(raw.get("measured_at"), path,
                                f"{platform}.{flow}.measured_at"),
        review_window_days=window,
    )


def _apply_downgrade(cell: Cell, today: date) -> Cell:
    """available 的证据/新鲜度规则（spec：证据不可复核或超窗 → unmapped）。"""
    if cell.status != STATUS_AVAILABLE:
        return cell
    kinds = {e.kind for e in cell.evidence}
    if EVIDENCE_PROVIDER_ACTION not in kinds:
        return _demote(cell, "证据缺 provider_action（动作面未登记）")
    if EVIDENCE_RUNTIME_READ not in kinds:
        return _demote(cell, "缺真实读取记录：动作存在不等于已验证可读")
    if cell.measured_at is None:
        return _demote(cell, "缺测量时点（无法判断新鲜度）")
    age = (today - cell.measured_at).days
    if age > int(cell.review_window_days or 0):
        return _demote(cell,
                       f"测量时点超出复核窗（{age}d > {cell.review_window_days}d）")
    return cell


def _demote(cell: Cell, why: str) -> Cell:
    return replace(cell, status=STATUS_UNMAPPED, downgraded_from=STATUS_AVAILABLE,
                   reason=f"{cell.reason}｜降级：{why}" if cell.reason else why)


def _parse_date(value: Any, path: Path | None, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise TemplateError("MATRIX_INVALID",
                            f"{path or '<inline>'}: {field_name} 须为 YYYY-MM-DD",
                            detail={"found": value}) from exc


def review_due_unavailable(matrix: CapabilityMatrix, *,
                           today: date | None = None) -> list[dict[str, Any]]:
    """`unavailable` 不降级，但到期要复核一次（确认「仍然没有」）。

    这是与 available 不同的节奏：前者是「还能不能拿到」，后者是「是不是仍然拿不到」。
    """
    today = today or date.today()
    out: list[dict[str, Any]] = []
    for (platform, flow) in sorted(matrix.cells):
        cell = matrix.cells[(platform, flow)]
        if cell.status != STATUS_UNAVAILABLE:
            continue
        if cell.measured_at is None:
            out.append({"platform": platform, "flow": flow,
                        "reason": cell.reason, "basis": "从未复核"})
            continue
        age = (today - cell.measured_at).days
        if age > int(cell.review_window_days or 0):
            out.append({"platform": platform, "flow": flow,
                        "reason": cell.reason,
                        "basis": f"超出复核窗（{age}d > {cell.review_window_days}d）"})
    return out


def matrix_path_for(template_dir: str | Path) -> Path:
    """模板目录 → 矩阵路径（约定：`<template>/capability-matrix.json`）。"""
    return Path(template_dir) / "capability-matrix.json"

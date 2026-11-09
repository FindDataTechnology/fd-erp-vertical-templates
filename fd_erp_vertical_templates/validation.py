"""机器可读模板校验产物（optimize-erp-solution tasks 2.3 / 2.5 / 2.6 / 3.5）。

一条命令（`validate --template … --json`）产出 CI 与市场侧都能消费的结构：

    {"schema": "erp-vertical-validation/v1",
     "ok": bool,                       # 有 error 级违规即 false
     "template": "supply-chain",
     "vertical": "supply-chain",
     "violations": [{"code", "severity", "asset"?, "path"?, "place"?, "message", "detail"?}],
     "warnings": [...],                # severity=warning：缺一处注记这类「没说」而非「说错」
     "assets": {kind: {"declared", "file", "path"}},
     "install": {"ok", "blocked": [...]}}

纪律：
- **不依赖网络**：所有证据来自仓内文件（清单/资产/状态快照/发布记录）。
- **两次运行逐字节一致**：违规按固定键排序，输出不含时间戳。
- 每个违规带**定位**（asset/place/path）与**修法方向**（message 里写明两值或期望值）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bindings import check_template_role_bindings
from .errors import TemplateError
from .manifest import _SEMVER, load_template_manifest
from .publish import check_notes

VALIDATION_SCHEMA = "erp-vertical-validation/v1"


def validate_template(template_dir: str | Path, *,
                      record: str | Path | dict[str, Any] | None = None,
                      status: str | Path | dict[str, Any] | None = None,
                      install_gate: bool = False,
                      capability: dict[str, Any] | None = None) -> dict[str, Any]:
    """跑全部模板级检查，返回机器可读结果。

    `install_gate=True` 时角色候选集为空会把 `ok` 压成 false（安装门）；
    否则只放进 `install.blocked` 供消费方决定（模板结构校验与本状态分开报）。
    """
    root = Path(template_dir)
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    manifest = _read_manifest(root)
    vertical = str((manifest or {}).get("vertical") or root.name)

    loaded = None
    try:
        loaded = load_template_manifest(root)
    except TemplateError as exc:
        for item in _violations_from_error(exc):
            violations.append(item)

    assets = _asset_versions(root, loaded or manifest or {})
    for kind, info in assets.items():
        if kind == "capability_matrix" and info.get("file") is None:
            warnings.append({
                "code": "ASSET_VERSION_UNSTATED", "severity": "warning",
                "asset": kind, "path": info.get("path"),
                "message": (f"{vertical}: capability_matrix 是派生资产，文件未声明 version；"
                            "清单声明 " + str(info.get("declared")) + " 无从对拍"),
                "detail": {"declared": info.get("declared")},
            })

    # 发布状态：三处注记 vs 发布记录（2.6）。
    try:
        for item in check_notes(root, record=record):
            (violations if item.get("severity", "error") == "error" else warnings).append(item)
    except TemplateError as exc:
        (violations if exc.code != "NOT_FOUND" else warnings).append({
            "code": "PUBLISH_RECORD_UNAVAILABLE", "severity": "warning",
            "message": f"{vertical}: 发布记录不可用（{exc.message}）——注记无法核对",
        })

    # 安装门：角色候选集（3.5）。
    blocked: list[dict[str, Any]] = []
    try:
        blocked = check_template_role_bindings(root, status=status)
    except TemplateError as exc:
        blocked = [{"code": "ROLE_STATUS_UNVERIFIED", "severity": "error",
                    "template": vertical, "role": "-", "binding": "-",
                    "message": exc.message,
                    "fix": "跑 scripts/build_connector_status.py"}]
    if install_gate:
        violations.extend(blocked)
    else:
        warnings.extend({**b, "severity": "warning"} for b in blocked)

    if capability is not None:
        for conflict in capability.get("conflicts") or []:
            violations.append({
                "code": "CAPABILITY_" + str(conflict.get("kind", "conflict")).upper(),
                "severity": "error",
                "message": str(conflict.get("detail") or conflict.get("kind")),
                "detail": conflict,
            })

    violations.sort(key=_sort_key)
    warnings.sort(key=_sort_key)
    return {
        "schema": VALIDATION_SCHEMA,
        "ok": not violations,
        "template": str(root),
        "vertical": vertical,
        "violations": violations,
        "warnings": warnings,
        "assets": assets,
        "install": {"ok": not blocked, "blocked": blocked},
        "capability": capability,
    }


def _sort_key(item: dict[str, Any]) -> tuple:
    return (str(item.get("code")), str(item.get("asset") or ""),
            str(item.get("place") or ""), str(item.get("role") or ""),
            str(item.get("message")))


def _violations_from_error(exc: TemplateError) -> list[dict[str, Any]]:
    """TemplateError → 违规条目；资产级违规一次全带出（不藏第二条）。"""
    items = (exc.detail or {}).get("violations")
    if isinstance(items, list) and items:
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = {k: item[k] for k in ("code", "asset", "path", "message", "detail")
                     if k in item}
            entry["severity"] = "error"
            out.append(entry)
        if out:
            return out
    return [{
        "code": str((exc.detail or {}).get("violation") or exc.code),
        "severity": "error",
        "message": exc.message,
        "detail": exc.detail or {},
    }]


def _read_manifest(root: Path) -> dict[str, Any] | None:
    try:
        return json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _asset_versions(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """清单声明版本 vs 资产文件版本（原始对照表，供 CI 直接读两值）。"""
    out: dict[str, dict[str, Any]] = {}
    for kind, asset in sorted((manifest.get("assets") or {}).items()):
        if not isinstance(asset, dict):
            continue
        rel = str(asset.get("path") or "")
        declared = str(asset.get("version") or "")
        path = root / rel
        seen = None
        if path.is_file():
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(body, dict):
                    seen = body.get("version")
            except json.JSONDecodeError:
                seen = None
        out[kind] = {"declared": declared, "file": seen, "path": rel,
                     "semver": bool(_SEMVER.match(declared))
                     and isinstance(seen, str) and bool(_SEMVER.match(seen))}
    return out


def render_validation(result: dict[str, Any]) -> str:
    head = (f"模板校验：通过（{result['vertical']}）" if result["ok"]
            else f"模板校验：失败（{len(result['violations'])} 处）")
    lines = [head]
    for item in result["violations"]:
        where = item.get("asset") or item.get("place") or item.get("role") or "-"
        lines.append(f"  x [{item['code']}] {where}")
        lines.append(f"      {item['message']}")
    for item in result.get("warnings") or []:
        where = item.get("asset") or item.get("place") or item.get("role") or "-"
        lines.append(f"  ! [{item['code']}] {where}")
        lines.append(f"      {item['message']}")
    install = result.get("install") or {}
    if install.get("blocked"):
        lines.append(f"  安装门：{len(install['blocked'])} 个角色候选集为空（模板结构校验不因此变红）")
    return "\n".join(lines)


__all__ = ["VALIDATION_SCHEMA", "render_validation", "validate_template"]

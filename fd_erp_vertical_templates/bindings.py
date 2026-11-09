"""角色绑定安装门（optimize-erp-solution task 3.5，spec erp-connector-onboarding）。

角色绑定（`providerRole: true`）的候选 provider 只认**目录实测快照**
（`connector-status.json` 的 `role_bindings`，由 `scripts/build_connector_status.py`
从 `open-connector-mt/catalog/apps` 派生）。候选集为空时安装被阻断，并点名角色
与原因——不用占位 provider 顶替，也不靠「看起来像业务词」蒙混。

为什么独立成门而不是塞进 `manifest.py`：角色缺 provider 是**安装前置条件**，
不是模板结构缺陷。模板结构（清单/资产/版本/注记）可以全绿，而某个角色没有
可用 provider——这两种状态必须分开报，否则「模板有错」与「目录还没补」会被
混成一句话，客户读到的是错的原因。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import TemplateError

DEFAULT_STATUS_PATH = Path(__file__).resolve().parent.parent / "connector-status.json"


def load_status(path: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(path, dict):
        return path
    target = Path(path) if path else DEFAULT_STATUS_PATH
    try:
        body = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TemplateError(
            "NOT_FOUND",
            f"连接器状态面不在位：{target}（先跑 scripts/build_connector_status.py）"
        ) from exc
    except json.JSONDecodeError as exc:
        raise TemplateError("MANIFEST_INVALID",
                            f"连接器状态面不是合法 JSON（{target}）：{exc}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("role_bindings"), list):
        raise TemplateError("MANIFEST_INVALID",
                            f"连接器状态面缺 role_bindings（{target}）")
    return body


def role_bindings_for(status: dict[str, Any], vertical: str) -> list[dict[str, Any]]:
    return [r for r in status.get("role_bindings") or []
            if vertical in (r.get("templates") or [])]


def declared_roles(template_dir: str | Path) -> list[dict[str, Any]]:
    """模板连接器 pack 里声明的角色绑定（`providerRole: true`）。"""
    root = Path(template_dir)
    pack_path = root / "connector-pack" / "pack.json"
    if not pack_path.is_file():
        raise TemplateError("NOT_FOUND", f"连接器 pack 不存在：{pack_path}")
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for binding in pack.get("connector_bindings") or []:
        if not isinstance(binding, dict) or not binding.get("providerRole"):
            continue
        out.append({"key": binding.get("key"),
                    "role": str(binding.get("provider") or ""),
                    "optional": bool(binding.get("optional")),
                    "external_system": binding.get("external_system")})
    return out


def check_template_role_bindings(
        template_dir: str | Path, *,
        status: str | Path | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """候选集为空即返回一条阻断（点名角色、绑定点与原因）；空列表 = 可安装。"""
    root = Path(template_dir)
    vertical = _vertical(root)
    st = load_status(status)
    by_role = {str(r.get("role")): r for r in role_bindings_for(st, vertical)}
    out: list[dict[str, Any]] = []
    for binding in declared_roles(root):
        role = binding["role"]
        entry = by_role.get(role)
        if entry is None:
            out.append({
                "code": "ROLE_STATUS_UNVERIFIED",
                "severity": "error",
                "role": role,
                "binding": binding["key"],
                "template": vertical,
                "message": (f"{vertical}: 角色 {role}（绑定 {binding['key']}）在连接器状态面"
                            "没有条目——候选集无法核对，安装前先刷新 status 快照"),
                "fix": "跑 scripts/build_connector_status.py 重新派生 connector-status.json",
            })
            continue
        candidates = entry.get("candidates") or []
        if candidates:
            continue
        reason = str(entry.get("reason") or "目录无该角色可用 provider")
        out.append({
            "code": "ROLE_NO_CANDIDATE",
            "severity": "error",
            "role": role,
            "binding": binding["key"],
            "template": vertical,
            "optional": binding["optional"],
            "candidates": [],
            "message": (f"{vertical}: 角色 {role}（绑定 {binding['key']}）候选集为空，"
                        f"安装被阻断——{reason}"),
            "fix": ("在 open-connector-mt 目录新增该角色 provider 并刷新快照"
                    "（docs/pos-skeleton-contract.md 给出了动作面与验收判据）"
                    "；或用安装侧显式豁免并记录责任人与原因"),
        })
    return out


def render_role_block(blocked: list[dict[str, Any]]) -> str:
    if not blocked:
        return "角色绑定：候选集齐备（可安装）"
    lines = [f"角色绑定：安装被阻断（{len(blocked)} 条）"]
    for item in blocked:
        lines.append(f"  x {item['template']}/{item['role']}（绑定 {item['binding']}）")
        lines.append(f"      {item['message']}")
        lines.append(f"      修法：{item['fix']}")
    return "\n".join(lines)


def _vertical(root: Path) -> str:
    try:
        body = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return root.name
    return str(body.get("vertical") or root.name)


__all__ = ["DEFAULT_STATUS_PATH", "check_template_role_bindings", "declared_roles",
           "load_status", "render_role_block", "role_bindings_for"]

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
import os
import time
from pathlib import Path
from typing import Any, Callable

from .errors import TemplateError

DEFAULT_STATUS_PATH = Path(__file__).resolve().parent.parent / "connector-status.json"


def load_status(
    path: str | Path | dict[str, Any] | None = None,
    *,
    fetch: "Callable[[str], Any] | None" = None,
) -> dict[str, Any]:
    """Load the connector status surface.

    An explicit ``path``/dict is returned as-is (tests and callers that already
    have a snapshot). Otherwise the **bench read face is tried first**: the
    platform is the authority for crosswalk connectivity, and this file is its
    mirror. When the read face is unreachable we fall back to the local file,
    **and say so** — `source: local-fallback` plus a `stale` flag when the file
    is older than the configured max age. Silently using an old snapshot is the
    failure mode the three-state discipline exists to prevent: it looks
    identical to a live one.
    """
    if isinstance(path, dict):
        return path
    if path is None:
        remote = _load_from_read_face(fetch)
        if remote is not None:
            return remote
    body = _load_local(path)
    return body


#: Where the bench read face lives. Unset means "never call out" — the honest
#: default for a library imported by tests and offline tooling.
READ_FACE_ENV = "BENCH_CROSSWALK_READ_FACE"


def _load_from_read_face(fetch: "Callable[[str], Any] | None" = None):
    """The platform's connectivity answer, or ``None`` when it cannot be read.

    **This module never performs the request itself.** The template layer has a
    hard boundary — no direct HTTP, no vendor URLs — enforced mechanically by
    ``scripts/check_delivery_gates`` (``PAYMENT_DIRECT_HTTP_IMPORT``): a mapping
    layer that can reach out on its own is one commit away from writing to an
    external system. So the caller injects a ``fetch(url) -> dict`` callable, and
    every connector runtime already owns exactly one of those.

    ``None`` is not an error here: the caller falls back to the local mirror and
    marks the source. What must never happen is a silent empty answer that looks
    like "nothing is connected".
    """
    base = os.environ.get(READ_FACE_ENV, "").strip().rstrip("/")
    if not base or fetch is None:
        return None

    # The read face is per-domain; the connector surface spans verticals, so we
    # ask for the one domain this package's templates anchor to.
    #
    # Path is the MACHINE route (`/api/v1/distribution/...`), not the console's
    # `/d/<domain>/crosswalks`: that one is gated by a Logto session + a
    # `content:read` scope, which a generator or a cron job can never satisfy.
    # The machine route takes the same `X-Consumer-Token` the pull API uses. The
    # two routes return the same guarantee at an earlier stage of this change;
    # pointed at the console route the seam was unreachable BY CONSTRUCTION and
    # the fallback hid it perfectly.
    domain = os.environ.get("BENCH_CROSSWALK_DOMAIN", "erp")
    url = f"{base}/api/v1/distribution/{domain}/crosswalks"
    try:
        payload = fetch(url)
    except Exception:  # noqa: BLE001 - an unreachable read face is a fallback, not a crash
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("crosswalks"), list):
        return None
    return _status_from_crosswalks(payload)


def _status_from_crosswalks(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the read face's crosswalk list onto this package's status shape.

    The projection exists so consumers never have to know the platform's shape;
    and so a change on the platform side is one function to update, not N call
    sites. `role_bindings` comes from the local template declarations, which the
    platform does not own.
    """
    states: dict[str, dict[str, dict[str, Any]]] = {}
    for row in payload.get("crosswalks") or []:
        peer = (row or {}).get("peer") or {}
        system = peer.get("system")
        if peer.get("kind") != "external_id" or not system:
            continue
        bucket = states.setdefault(system, {})
        for state, value in ((row or {}).get("connectivity") or {}).items():
            if isinstance(value, dict) and value.get("ok"):
                bucket[state] = value
    integrations = []
    for system, cleared in sorted(states.items()):
        missing = [s for s in ("catalog", "executor_tests", "live") if s not in cleared]
        integrations.append({
            "external_system": system,
            "provider": system,
            "state": "not_connected" if missing else "connected",
            "missing": missing,
            "states": {
                s: {"ok": s in cleared, "evidence": cleared.get(s, {}).get("evidence")}
                for s in ("catalog", "executor_tests", "live")
            },
        })
    return {
        "source": "bench-read-face",
        "role_bindings": _local_role_bindings(),
        "integrations": integrations,
    }


def _local_role_bindings() -> list[dict[str, Any]]:
    """Role declarations live on the template, not on the platform."""
    try:
        body = json.loads(DEFAULT_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    bindings = body.get("role_bindings")
    return bindings if isinstance(bindings, list) else []


#: A local snapshot older than this is reported `stale: true`. Overridable so a
#: deployment can declare its own expiry; absent means "no expiry declared", and
#: the loader then says exactly that rather than inventing a number.
STALE_AFTER_DAYS_ENV = "ERP_CONNECTOR_STATUS_MAX_AGE_DAYS"


def _load_local(path: str | Path | None) -> dict[str, Any]:
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
    # Only annotate when we actually fell back: an explicit path is the caller's
    # own snapshot and calling it a fallback would be a lie.
    if path is None:
        body = dict(body)
        body.setdefault("source", "local-fallback")
        stale = _is_stale(target)
        if stale is not None:
            body["stale"] = stale
    return body


def _is_stale(target: Path) -> bool | None:
    """Whether the snapshot exceeds its declared max age; ``None`` = no expiry set."""
    raw = os.environ.get(STALE_AFTER_DAYS_ENV, "").strip()
    if not raw:
        return None
    try:
        max_days = float(raw)
    except ValueError:
        return None
    try:
        age_days = (time.time() - target.stat().st_mtime) / 86400
    except OSError:
        return None
    return age_days > max_days


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

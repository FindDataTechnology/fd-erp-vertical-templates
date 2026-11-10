"""发布门与台账（spec `ecommerce-release-gates`，任务 1.1-1.3；原 change `ship-ecommerce-commercial` 已于 2026-11-10 归档）。

上架/出包前要过的门，以及一道门跑完后要留的账。两件事解决的是同一类事故：

- **门**：矩阵改了、preset 没重生成；模板清单能加载但资产不一致；出包 VERIFY
  没过——这些都曾在别处以「人记得跑一遍」的方式兜着，结果是人没记住。
- **台账**：版本号没变但内容变了。客户拿到与记录不符的包，事后无法定责。
  与镜像 tag 的内容闸门同构：摘要没变才算同一个东西。

纪律：门只读模板与产物，台账只追加（append-only）；同版本不同摘要一律拒绝，
不做「覆盖成最新的」。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .capability import matrix_path_for
from .capability_check import check_template, render_lint
from .errors import TemplateError
from .manifest import load_template_manifest

#: 台账状态（与 design.md 附录一致）。
STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_BLOCKED = "blocked"
STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_BLOCKED)

#: 参与发布的资产：(台账 asset_id, 模板内相对路径)。
DEFAULT_ASSETS = (
    ("capability-matrix", "capability-matrix.json"),
    ("connector-pack", "connector-pack/pack.json"),
    ("report-preset", "report-preset/preset.json"),
    ("account-mapping-pack", "account-mapping/pack.json"),
    ("recon-fleet", "recon-fleet/template.json"),
)


def _sha256_bytes(blob: bytes) -> str:
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def artifact_digest(path: Path) -> str:
    """资产摘要。JSON 走规范化序列化，避免纯格式改动造成假变更。"""
    raw = path.read_bytes()
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _sha256_bytes(raw)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


def _semver_tuple(version: str) -> tuple[int, int, int]:
    try:
        major, minor, patch = str(version).split(".")
        return int(major), int(minor), int(patch)
    except (ValueError, AttributeError) as exc:
        raise TemplateError("INVALID_ARGUMENT",
                            f"版本号不是 semver：{version!r}") from exc


@dataclass(frozen=True)
class LedgerEntry:
    asset_id: str
    version: str
    matrix_hash: str
    artifact_hash: str
    published_at: str
    status: str
    note: str = ""
    external_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"asset_id": self.asset_id, "version": self.version,
               "matrix_hash": self.matrix_hash, "artifact_hash": self.artifact_hash,
               "published_at": self.published_at, "status": self.status}
        if self.note:
            out["note"] = self.note
        if self.external_reason:
            out["external_reason"] = self.external_reason
        return out


class Ledger:
    """append-only 台账（JSONL）。只增不改，同版本不同摘要直接拒。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: list[LedgerEntry] = []
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    body = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TemplateError(
                        "MANIFEST_INVALID",
                        f"台账行不是合法 JSON（{self.path}: {exc}）——台账不接受手改"
                    ) from exc
                self._entries.append(LedgerEntry(
                    asset_id=str(body.get("asset_id")),
                    version=str(body.get("version")),
                    matrix_hash=str(body.get("matrix_hash") or ""),
                    artifact_hash=str(body.get("artifact_hash") or ""),
                    published_at=str(body.get("published_at") or ""),
                    status=str(body.get("status") or STATUS_DRAFT),
                    note=str(body.get("note") or ""),
                    external_reason=str(body.get("external_reason") or ""),
                ))

    # ---- 查询 ----

    def latest(self, asset_id: str) -> LedgerEntry | None:
        for entry in reversed(self._entries):
            if entry.asset_id == asset_id:
                return entry
        return None

    def history(self, asset_id: str) -> list[LedgerEntry]:
        return [e for e in self._entries if e.asset_id == asset_id]

    # ---- 追加 ----

    def append(self, entry: LedgerEntry) -> str:
        """追加一条，返回本次动作：`appended` / `unchanged`。

        同版本 + 同摘要 → `unchanged`（幂等，不重复记账）；
        同版本 + 不同摘要 → 拒绝，要求 bump 版本。
        """
        if entry.status not in STATUSES:
            raise TemplateError("INVALID_ARGUMENT",
                                f"台账状态非法：{entry.status}")
        last = self.latest(entry.asset_id)
        if last is not None:
            new_v = _semver_tuple(entry.version)
            old_v = _semver_tuple(last.version)
            if new_v == old_v:
                if entry.artifact_hash == last.artifact_hash and \
                        entry.matrix_hash == last.matrix_hash:
                    return "unchanged"
                raise TemplateError(
                    "MANIFEST_INVALID",
                    f"{entry.asset_id} v{entry.version} 已存在但内容不同："
                    "版本号没变却改了内容，必须 bump 版本后重发",
                    detail={"asset_id": entry.asset_id, "version": entry.version,
                            "recorded": last.artifact_hash,
                            "attempted": entry.artifact_hash})
            if new_v < old_v:
                raise TemplateError(
                    "MANIFEST_INVALID",
                    f"{entry.asset_id} 版本回退：台账最新 v{last.version}，"
                    f"本次 v{entry.version}——回退要走显式的撤回流程，不能用发文覆盖",
                    detail={"asset_id": entry.asset_id})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
        self._entries.append(entry)
        return "appended"


#: 出包验证器签名：(template_dir, out_dir) -> (ok, detail)
BuildRunner = Callable[[Path, Path], "tuple[bool, str]"]


def appliance_build_runner(template_dir: Path, out_dir: Path) -> tuple[bool, str]:
    """默认出包验证：调用 `appliance_packer`（跨仓，PYTHONPATH 需含 fd-appliance）。

    装不到就**如实报不可用**，不算 PASS——门缺一项就少一分证据。
    """
    try:
        from appliance_packer import cli as packer_cli  # type: ignore
    except ImportError:
        return False, "appliance_packer 不可用（需 PYTHONPATH 含 fd-appliance）；本项未验证"
    tenant_dir = template_dir  # 由调用方传入租户目录时直接构建
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        code = packer_cli.main(["build", str(tenant_dir), "--out", str(out_dir)])
    except SystemExit as exc:  # packer 用 SystemExit 表达退出码
        code = int(exc.code or 0)
    return code == 0, f"appliance_packer build 退出码 {code}"


def run_release_gate(template_dir: str | Path, *, ledger_path: str | Path,
                     out_dir: str | Path | None = None,
                     assets: Iterable[tuple[str, str]] = DEFAULT_ASSETS,
                     build_runner: BuildRunner | None = None,
                     record: bool = True,
                     today: date | None = None) -> dict[str, Any]:
    """跑门并（可选）记账。

    返回 `{"ok": bool, "assets": [...], "conflicts": [...], "ledger": str}`；
    每个资产一条结果，失败项带 `fix`。退出码由调用方按 `ok` 决定。
    """
    root = Path(template_dir)
    today = today or date.today()
    conflicts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    # 第 1 关：能力面 + 模板清单
    capability = check_template(root, today=today, doc_paths=[])
    if not capability["ok"]:
        conflicts.extend(capability["conflicts"])
    manifest = None
    try:
        manifest = load_template_manifest(root)
    except TemplateError as exc:
        conflicts.append({"kind": "manifest-invalid", "detail": exc.message,
                          "fix": "修 manifest.json 或四件套资产后重跑"})

    matrix_file = matrix_path_for(root)
    matrix_hash = artifact_digest(matrix_file) if matrix_file.is_file() else ""
    if not matrix_hash:
        conflicts.append({"kind": "matrix-missing", "artifact": str(matrix_file),
                          "fix": "先落能力矩阵（capability-matrix.json）"})

    # 第 2 关：逐资产摘要 + 版本单调
    ledger = Ledger(ledger_path)
    for asset_id, rel in assets:
        path = root / rel
        if not path.is_file():
            results.append({"asset_id": asset_id, "path": rel, "ok": False,
                            "reason": "资产文件缺失"})
            conflicts.append({"kind": "asset-missing", "artifact": rel,
                              "fix": f"补齐 {rel} 后重跑"})
            continue
        digest = artifact_digest(path)
        version = _asset_version(manifest, rel)
        if not version:
            results.append({"asset_id": asset_id, "path": rel, "ok": False,
                            "reason": "清单里没有该资产的版本号"})
            conflicts.append({"kind": "version-missing", "artifact": rel,
                              "fix": "在 manifest.json 的 assets 段登记该资产版本"})
            continue
        last = ledger.latest(asset_id)
        unchanged = (last is not None and last.version == version
                     and last.artifact_hash == digest and last.matrix_hash == matrix_hash)
        results.append({"asset_id": asset_id, "path": rel, "version": version,
                        "artifact_hash": digest, "matrix_hash": matrix_hash,
                        "ok": True, "unchanged": unchanged})
        if record and not unchanged:
            try:
                action = ledger.append(LedgerEntry(
                    asset_id=asset_id, version=version, matrix_hash=matrix_hash,
                    artifact_hash=digest,
                    published_at=datetime.now(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    status=STATUS_DRAFT if capability["ok"] else STATUS_BLOCKED,
                    note="release-gate 自动记账",
                    external_reason="" if capability["ok"] else "能力面/清单门未过"))
            except TemplateError as exc:
                conflicts.append({"kind": "ledger-rejected", "asset_id": asset_id,
                                  "detail": exc.message,
                                  "fix": "bump 版本号后重发；台账不允许同版本改内容"})
                results[-1]["ok"] = False
            else:
                results[-1]["ledger_action"] = action

    # 第 3 关：出包验证（可选；真机/CI 有 appliance 仓时跑）
    if build_runner is not None:
        ok, detail = build_runner(root, Path(out_dir or (root / ".build")))
        results.append({"asset_id": "appliance-bundle", "ok": ok, "detail": detail})
        if not ok:
            conflicts.append({"kind": "appliance-verify-failed", "detail": detail,
                              "fix": "按 appliance_packer 输出修包后重跑；不得跳过"})

    return {"ok": not conflicts, "assets": results, "conflicts": conflicts,
            "ledger": str(ledger_path),
            "matrix_fingerprint": capability.get("fingerprint", ""),
            "capability_ok": capability["ok"]}


def _asset_version(manifest: dict[str, Any] | None, rel: str) -> str:
    """按相对路径回查清单里的资产版本（找不到返回空串）。"""
    if not manifest:
        return ""
    for asset in (manifest.get("assets") or {}).values():
        if str(asset.get("path") or "") == rel:
            return str(asset.get("version") or "")
    return ""


def render_gate(result: dict[str, Any]) -> str:
    lines = [("发布门：通过" if result["ok"]
              else f"发布门：阻断（{len(result['conflicts'])} 处）")]
    for row in result["assets"]:
        mark = "ok " if row.get("ok") else "x  "
        bits = [f"{mark}{row['asset_id']}"]
        if row.get("version"):
            bits.append(f"v{row['version']}")
        if row.get("unchanged"):
            bits.append("未变更（跳过记账）")
        if row.get("ledger_action"):
            bits.append(row["ledger_action"])
        if row.get("detail"):
            bits.append(row["detail"])
        if row.get("reason"):
            bits.append(row["reason"])
        lines.append("  " + " · ".join(bits))
    if result["conflicts"]:
        cap, other = [], []
        for c in result["conflicts"]:
            (cap if str(c.get("kind", "")).startswith(("anchor", "artifact", "preset", "pack"))
             else other).append(c)
        for c in result["conflicts"]:
            where = c.get("doc") or c.get("artifact") or c.get("asset_id") or "-"
            lines.append(f"  x [{c['kind']}] {where}")
            if c.get("detail"):
                lines.append(f"      {c['detail']}")
            lines.append(f"      修法：{c.get('fix', '—')}")
    lines.append(f"  台账：{result['ledger']}")
    return "\n".join(lines)

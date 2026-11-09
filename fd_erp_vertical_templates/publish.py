"""发布记录与三处注记派生（optimize-erp-solution tasks 2.6 / 2.7 / 2.8，design D4）。

**发布状态只有一个出处：`publish-record.json`。** 连接器 pack 里的三处注记
（顶层 `notes` / `i18n.en.notes` / `_casting.note`）由本模块从记录重写，校验器
比对三处与记录——不再让某个注释自述「未上架」而另一个写「已上架 v3」。

派生规则（一行可复述）：`<状态子句>。<该 pack 的说明文字>。`

    已上架：市场 v3（packId y8gnMHFMze-Mylsd6JXpkA）
    publish-ready 源清单（未上架，packId 未铸造）
    Published: market v3 (packId y8gnMHFMze-Mylsd6JXpkA)
    Publish-ready source (not yet published; packId not minted)

说明文字存在记录里（`notes.zh` / `notes.en` / `notes.casting`），所以改文案与改
状态都不必碰 pack 文件——那是「人记得同步」的旧做法，正是本 change 要收掉的。

市场版本不凭印象：`check_fleet_ledger` 读兄弟仓 `fleet-packs/README.md` 的机器台账
（cast 脚本与漂移哨的同一张表）逐包比对；兄弟仓不在位时如实报 unverified，不假装绿。

发布动作只做**封装与 dry-run**（task 2.7）：真发版是带凭据的对外动作，不在本
change 内执行；限流（10 次/时/作者）错误原样透传成人话。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import TemplateError

RECORD_SCHEMA = "erp-template-publish-record/v1"
DEFAULT_RECORD_PATH = Path(__file__).resolve().parent.parent / "publish-record.json"

#: 三处注记在 pack 内的定位（place → 读/写函数名）。
PLACES = ("notes", "notes_en", "casting")

#: 状态子句（唯一来源；任何注记里的状态表述必须逐字等于它）。
_CLAUSE_PUBLISHED_ZH = "已上架：市场 v{version}（packId {pack_id}）"
_CLAUSE_UNPUBLISHED_ZH = "publish-ready 源清单（未上架，packId 未铸造）"
_CLAUSE_PUBLISHED_EN = "Published: market v{version} (packId {pack_id})"
_CLAUSE_UNPUBLISHED_EN = "Publish-ready source (not yet published; packId not minted)"

#: 市场台账（兄弟仓）里的 slug → 模板键。
FLEET_LEDGER_SLUGS = {
    "supply-chain-connector-pack": "supply-chain/connector-pack",
    "restaurant-chain-connector-pack": "restaurant-chain/connector-pack",
    "crossborder-connector-pack": "crossborder/connector-pack",
}


# ------------------------------------------------------------------ 记录读取


def load_record(path: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    """加载发布记录（路径 / 已解析 dict / 缺省仓内 publish-record.json）。"""
    if isinstance(path, dict):
        body = path
    else:
        target = Path(path) if path else DEFAULT_RECORD_PATH
        try:
            body = json.loads(target.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TemplateError("NOT_FOUND", f"发布记录不可读：{target}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise TemplateError("MANIFEST_INVALID",
                                f"发布记录不是合法 JSON（{target}）：{exc}") from exc
    if not isinstance(body, dict) or body.get("schema") != RECORD_SCHEMA:
        raise TemplateError("MANIFEST_INVALID",
                            f"发布记录 schema 须为 {RECORD_SCHEMA}")
    packs = body.get("packs")
    if not isinstance(packs, dict) or not packs:
        raise TemplateError("MANIFEST_INVALID", "发布记录缺 packs 段")
    for key, entry in packs.items():
        _validate_entry(key, entry)
    return body


def _validate_entry(key: str, entry: Any) -> None:
    if not isinstance(entry, dict):
        raise TemplateError("MANIFEST_INVALID", f"发布记录 {key} 须为对象")
    if not isinstance(entry.get("published"), bool):
        raise TemplateError("MANIFEST_INVALID",
                            f"发布记录 {key}.published 须为布尔（未上架也要显式写 false）")
    if not str(entry.get("asset") or "").strip():
        raise TemplateError("MANIFEST_INVALID", f"发布记录 {key}.asset 必填")
    notes = entry.get("notes") or {}
    if not isinstance(notes, dict) or not all(
            str(notes.get(k) or "").strip() for k in PLACES):
        raise TemplateError(
            "MANIFEST_INVALID",
            f"发布记录 {key}.notes 须给全 {', '.join(PLACES)}（注记由记录派生）")
    if entry["published"]:
        version = entry.get("market_version")
        if not isinstance(version, int) or version < 1:
            raise TemplateError("MANIFEST_INVALID",
                                f"发布记录 {key}.market_version 须为正整数市场版本号")
        if not re.fullmatch(r"[\w-]{6,}", str(entry.get("pack_id") or "")):
            raise TemplateError("MANIFEST_INVALID",
                                f"发布记录 {key}.pack_id 缺失或形状可疑")
    if str(entry.get("derivation") or "applied") not in ("applied", "pending"):
        raise TemplateError("MANIFEST_INVALID",
                            f"发布记录 {key}.derivation 只能是 applied / pending")


def entry_for(record: dict[str, Any], key: str) -> dict[str, Any] | None:
    return (record.get("packs") or {}).get(key)


def record_key(vertical: str, asset: str = "connector-pack") -> str:
    return f"{vertical}/{asset}"


# ------------------------------------------------------------------ 派生


def state_clause(entry: dict[str, Any], *, lang: str = "zh") -> str:
    published = bool(entry.get("published"))
    if lang == "en":
        return (_CLAUSE_PUBLISHED_EN.format(version=entry.get("market_version"),
                                            pack_id=entry.get("pack_id"))
                if published else _CLAUSE_UNPUBLISHED_EN)
    return (_CLAUSE_PUBLISHED_ZH.format(version=entry.get("market_version"),
                                        pack_id=entry.get("pack_id"))
            if published else _CLAUSE_UNPUBLISHED_ZH)


def derive_notes(entry: dict[str, Any]) -> dict[str, str]:
    """三处注记的目标文本（状态子句 + 记录里的说明文字）。"""
    notes = entry.get("notes") or {}
    out: dict[str, str] = {}
    for place in ("notes", "casting"):
        prose = str(notes.get(place) or "").strip().rstrip("。；; ")
        out[place] = f"{state_clause(entry)}。{prose}。"
    prose_en = str(notes.get("notes_en") or "").strip().rstrip(". ")
    out["notes_en"] = f"{state_clause(entry, lang='en')}. {prose_en}."
    return out


def _read_place(pack: dict[str, Any], place: str) -> str | None:
    if place == "notes":
        return pack.get("notes")
    if place == "notes_en":
        i18n = pack.get("i18n") or {}
        en = i18n.get("en") if isinstance(i18n, dict) else None
        return (en or {}).get("notes") if isinstance(en, dict) else None
    casting = pack.get("_casting") or {}
    return casting.get("note") if isinstance(casting, dict) else None


def _write_place(pack: dict[str, Any], place: str, text: str) -> bool:
    """写回一处；返回是否真的变了（幂等：相同就不写）。"""
    if _read_place(pack, place) == text:
        return False
    if place == "notes":
        # 顶层键位置稳定放在 name/version/description 之后（原文件已有的顺序不动）。
        pack["notes"] = text
    elif place == "notes_en":
        i18n = pack.setdefault("i18n", {})
        en = i18n.setdefault("en", {})
        en["notes"] = text
    else:
        casting = pack.setdefault("_casting", {})
        casting["note"] = text
    return True


def _dump(pack: dict[str, Any], path: Path) -> None:
    """按仓内约定（indent=1，保序）写回，避免整文件格式噪声。"""
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")


def apply_notes(template_dir: str | Path, *,
                record: str | Path | dict[str, Any] | None = None,
                dry_run: bool = False) -> dict[str, Any]:
    """按发布记录重写该模板连接器 pack 的三处注记。

    返回 `{"ok", "template", "pack", "changed": [...], "values": {...}}`；
    记录里没有该模板（或无 connector-pack 条目）→ 拒绝派生（不强造状态）。
    """
    root = Path(template_dir)
    vertical = _template_vertical(root)
    key = record_key(vertical)
    rec = load_record(record)
    entry = entry_for(rec, key)
    if entry is None:
        raise TemplateError(
            "NOT_FOUND",
            f"发布记录里没有 {key}：先在 publish-record.json 登记（未上架写 "
            "published=false），注记不接受无出处的状态表述")
    asset_rel = str(entry["asset"])
    pack_path = root / asset_rel
    if not pack_path.is_file():
        raise TemplateError("NOT_FOUND", f"连接器 pack 不存在：{pack_path}")
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    wanted = derive_notes(entry)
    changed: list[str] = []
    for place in PLACES:
        if _write_place(pack, place, wanted[place]):
            changed.append(place)
    if changed and not dry_run:
        _dump(pack, pack_path)
    return {"ok": True, "template": vertical, "pack": str(asset_rel),
            "key": key, "changed": changed, "written": bool(changed and not dry_run),
            "values": wanted}


# ------------------------------------------------------------------ 校验


def _violation(code: str, message: str, *, severity: str = "error",
               place: str = "", path: str = "",
               detail: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "severity": severity,
                           "message": message}
    if place:
        out["place"] = place
    if path:
        out["path"] = path
    if detail:
        out["detail"] = detail
    return out


def check_notes(template_dir: str | Path, *,
                record: str | Path | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """三处注记 vs 发布记录（不一致即红；记录标 pending 的降级为告警并带理由）。"""
    root = Path(template_dir)
    vertical = _template_vertical(root)
    rec = load_record(record)
    key = record_key(vertical)
    entry = entry_for(rec, key)
    pack_path = root / "connector-pack" / "pack.json"
    if not pack_path.is_file():
        return []
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    if entry is None:
        out = []
        for place in PLACES:
            text = _read_place(pack, place)
            if text and _asserts_published(str(text)):
                out.append(_violation(
                    "PUBLISH_UNRECORDED_CLAIM",
                    f"{key} 在 {place} 自述已上架，但发布记录里没有该条目："
                    "状态必须来自 publish-record.json",
                    place=place, path=str(pack_path),
                    detail={"text": str(text)[:200]}))
            elif text and not _asserts_unpublished(str(text)):
                out.append(_violation(
                    "PUBLISH_NOTE_UNSTATED",
                    f"{key} 的 {place} 未写发布状态（已上架/未上架都没有）："
                    "状态须由发布记录派生，不留给读者猜",
                    severity="warning", place=place, path=str(pack_path)))
        return out

    severity = "warning" if entry.get("derivation") == "pending" else "error"
    wanted = derive_notes(entry)
    out: list[dict[str, Any]] = []
    for place in PLACES:
        found = _read_place(pack, place)
        expected = wanted[place]
        if found is None:
            out.append(_violation(
                "PUBLISH_NOTE_UNSTATED",
                f"{key} 的 {place} 未写（发布状态三处须齐）；期望：{expected}",
                severity="warning",   # 缺一处表述不算「自相矛盾」，但必须可见
                place=place, path=str(pack_path),
                detail={"expected": expected}))
            continue
        if str(found) != expected:
            out.append(_violation(
                "PUBLISH_NOTE_DRIFT",
                f"{key} 的 {place} 与发布记录不一致："
                f"记录 →「{expected}」；文件 →「{found}」"
                + (f"（该条目按记录暂缓派生：{entry.get('derivation_reason')}）"
                   if severity == "warning" else
                   "（跑 `publish-notes --template …` 从记录重写）"),
                severity=severity, place=place, path=str(pack_path),
                detail={"expected": expected, "found": str(found),
                        "published": entry.get("published"),
                        "market_version": entry.get("market_version"),
                        "pack_id": entry.get("pack_id")}))
    return out


_PUBLISHED_PATTERNS = (
    re.compile(r"已上架"),
    re.compile(r"Published: market v\d+ \(packId [\w-]+\)"),
    re.compile(r"\bPublished: market\b"),
    re.compile(r"已 live 上架"),
)

_UNPUBLISHED_PATTERNS = (
    re.compile(r"publish-ready 源清单（未上架，packId 未铸造）"),
    re.compile(r"[Pp]ublish-ready source \(not yet published; packId not minted\)"),
    re.compile(r"未上架"),
    re.compile(r"not yet published"),
)


def _asserts_published(text: str) -> bool:
    return any(p.search(text) for p in _PUBLISHED_PATTERNS)


def _asserts_unpublished(text: str) -> bool:
    return any(p.search(text) for p in _UNPUBLISHED_PATTERNS)


#: 文档 §5.1 表格行里的包名 → 记录键（中文名是文档口径，不随英文变）。
_DOC_ROW_KEYS = {
    "供应链连接器包": "supply-chain/connector-pack",
    "餐饮连锁连接器包": "restaurant-chain/connector-pack",
    "跨境电商连接器包": "crossborder/connector-pack",
}

#: 文档 §8.1 的一句话口径：`supply-chain v3 / restaurant v2 / crossborder v2`。
_DOC_MARKET_LINE = re.compile(
    r"supply-chain v(\d+)\s*/\s*restaurant v(\d+)\s*/\s*crossborder v(\d+)")


def check_docs(doc_path: str | Path, *,
               record: str | Path | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """文档里的市场版本/ packId 与发布记录对拍（task 2.8：消除 §5.1 与 §8.1 矛盾）。"""
    path = Path(doc_path)
    if not path.is_file():
        return [_violation("DOC_MISSING", f"口径文档不存在：{path}",
                           path=str(path), severity="warning")]
    rec = load_record(record)
    packs = rec["packs"]
    text = path.read_text(encoding="utf-8")
    out: list[dict[str, Any]] = []
    seen_rows: set[str] = set()

    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        key = _DOC_ROW_KEYS.get(cells[0])
        if key is None:
            continue
        seen_rows.add(key)
        entry = packs.get(key) or {}
        pack_id = cells[1].strip("` ")
        versions = [int(v) for v in re.findall(r"v(\d+)", cells[2])]
        out.extend(_compare_doc(key, entry, pack_id,
                                versions[-1] if versions else None,
                                where=f"{path.name} §5.1 表格 {cells[0]}",
                                path=str(path)))

    market = _DOC_MARKET_LINE.search(text)
    if market:
        for slug, group in (("supply-chain", 1), ("restaurant-chain", 2),
                            ("crossborder", 3)):
            key = record_key(slug)
            entry = packs.get(key) or {}
            out.extend(_compare_doc(key, entry, None, int(market.group(group)),
                                    where=f"{path.name} §8.1 一句话口径 {slug}",
                                    path=str(path)))
    else:
        out.append(_violation(
            "DOC_PUBLISH_LINE_MISSING",
            f"{path.name}: 未找到 §8.1 的市场版本口径句"
            "（`supply-chain vN / restaurant vN / crossborder vN`）——"
            "文档必须复述发布记录，不能省掉",
            path=str(path)))
    for key in _DOC_ROW_KEYS.values():
        if key not in seen_rows:
            out.append(_violation(
                "DOC_PUBLISH_ROW_MISSING",
                f"{path.name}: §5.1 表格缺 {key} 行（三模板市场版本须逐包复述记录）",
                path=str(path)))
    return out


def _compare_doc(key: str, entry: dict[str, Any], pack_id: str | None,
                 version: int | None, *, where: str, path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not entry:
        return [_violation("DOC_PUBLISH_UNRECORDED",
                           f"{where}: 发布记录里没有 {key}", path=path)]
    want_version = entry.get("market_version") if entry.get("published") else None
    want_id = entry.get("pack_id") if entry.get("published") else None
    if version != want_version:
        out.append(_violation(
            "DOC_MARKET_VERSION_DRIFT",
            f"{where}: 文档写 {version} ≠ 发布记录 {want_version}（{key}）",
            path=path,
            detail={"doc_version": version, "record_version": want_version,
                    "key": key}))
    if pack_id and want_id and pack_id != want_id:
        out.append(_violation(
            "DOC_PACK_ID_DRIFT",
            f"{where}: 文档写 packId {pack_id} ≠ 发布记录 {want_id}（{key}）",
            path=path, detail={"doc_pack_id": pack_id, "record_pack_id": want_id}))
    return out


def check_fleet_ledger(*, record: str | Path | dict[str, Any] | None = None,
                       ledger: str | Path | None = None) -> dict[str, Any]:
    """兄弟仓市场台账（fleet-packs/README.md 机器台账）vs 发布记录。

    兄弟仓不在位 → `verified=False` 如实报（不 fake 绿）；在位 → 逐包对版本与 packId。
    """
    path = Path(ledger) if ledger else (
        Path(__file__).resolve().parents[2] / "fleet-packs" / "README.md")
    rec = load_record(record)
    if not path.is_file():
        return {"verified": False, "violations": [],
                "reason": f"市场台账不在位（{path}）：市场版本只能以记录为准，待有 checkout 时复核"}
    violations: list[dict[str, Any]] = []
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        key = FLEET_LEDGER_SLUGS.get(cells[0])
        if key is None:
            continue
        found.add(key)
        entry = (rec["packs"] or {}).get(key) or {}
        version_match = re.fullmatch(r"v(\d+)", cells[2])
        doc_version = int(version_match.group(1)) if version_match else None
        doc_id = cells[1].strip("` ")
        want_version = entry.get("market_version") if entry.get("published") else None
        want_id = entry.get("pack_id") if entry.get("published") else None
        if doc_version != want_version:
            violations.append(_violation(
                "MARKET_LEDGER_VERSION_DRIFT",
                f"市场台账 {cells[0]} 版本 v{doc_version} ≠ 发布记录 v{want_version}"
                "（记录是口径出处；市场台账是实测出处——两者不一致说明记录该回填）",
                path=str(path),
                detail={"ledger_version": doc_version,
                        "record_version": want_version}))
        if want_id and doc_id != want_id:
            violations.append(_violation(
                "MARKET_LEDGER_PACK_ID_DRIFT",
                f"市场台账 {cells[0]} packId {doc_id} ≠ 发布记录 {want_id}",
                path=str(path),
                detail={"ledger_pack_id": doc_id, "record_pack_id": want_id}))
    for key in FLEET_LEDGER_SLUGS.values():
        if key not in found:
            violations.append(_violation(
                "MARKET_LEDGER_ROW_MISSING",
                f"市场台账里找不到 {key} 对应行：无法复核市场版本", path=str(path)))
    return {"verified": True, "violations": violations, "ledger": str(path)}


# ------------------------------------------------------------------ 发布封装


def publish_command(template_dir: str | Path, *, author: str,
                    pack_id: str | None = None,
                    tool: str | Path | None = None,
                    base: str | None = None,
                    dry_run: bool = True) -> list[str]:
    """构造兄弟仓 `publish_pack.py` 的目标命令（不复制工具本身，design D10）。"""
    root = Path(template_dir)
    vertical = _template_vertical(root)
    rec = load_record()
    entry = entry_for(rec, record_key(vertical)) or {}
    pack = root / str(entry.get("asset") or "connector-pack/pack.json")
    tool_path = Path(tool) if tool else (
        Path(__file__).resolve().parents[2] / "fleet-packs" / "tooling" / "publish_pack.py")
    argv = [sys.executable, str(tool_path), str(pack), "--author", author]
    target_id = pack_id or (entry.get("pack_id") if entry.get("published") else None)
    if target_id:
        argv += ["--pack-id", str(target_id)]
    if base:
        argv += ["--base", base]
    if dry_run:
        argv.append("--dry-run")
    return argv


_RATE_LIMIT_MARKERS = ("429", "rate limit", "10/hour", "限流", "too many requests")


def run_publish_tool(template_dir: str | Path, *, author: str,
                     dry_run: bool = True, pack_id: str | None = None,
                     tool: str | Path | None = None, base: str | None = None,
                     timeout: float = 120.0) -> dict[str, Any]:
    """以子进程调用兄弟仓发布工具，透传限流错误（task 2.7）。

    `dry_run=False` **不执行**：真发版是带凭据的对外动作，本 change 只封装与
    dry-run。要发版请在凭据环境里直接跑 `publish_command()` 打出的命令。
    """
    if not dry_run:
        return {"ok": False, "dry_run": False, "published": False,
                "command": publish_command(template_dir, author=author,
                                           pack_id=pack_id, tool=tool, base=base,
                                           dry_run=True),
                "message": "本仓只做封装与 dry-run：真发版须在凭据环境直接运行上面这条命令"
                           "（兄弟仓 publish_pack.py），不在本 change 内执行"}
    argv = publish_command(template_dir, author=author, pack_id=pack_id,
                           tool=tool, base=base, dry_run=True)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "dry_run": True, "published": False,
                "command": argv, "message": f"发布工具不可执行：{exc}"}
    combined = f"{proc.stdout}\n{proc.stderr}"
    lowered = combined.lower()
    rate_limited = any(marker in lowered for marker in _RATE_LIMIT_MARKERS)
    result: dict[str, Any] = {
        "ok": proc.returncode == 0 and not rate_limited,
        "dry_run": True, "published": False, "command": argv,
        "exit_code": proc.returncode,
        "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(),
        "rate_limited": rate_limited,
    }
    if rate_limited:
        result["message"] = (
            "上游限流（发布通道 10 次/时/作者）：退避后重试；"
            "归因「配额」而不是「发布失败」")
        result["error_code"] = "RATE_LIMITED"
    elif proc.returncode != 0:
        result["message"] = f"发布工具退出码 {proc.returncode}（见 stderr）"
        result["error_code"] = "PUBLISH_TOOL_FAILED"
    else:
        result["message"] = "dry-run 通过：本地校验 OK，未发送任何请求"
    return result


def render_publish_result(result: dict[str, Any]) -> str:
    lines = [("dry-run：" + ("通过" if result.get("ok") else "失败"))]
    lines.append("  命令：" + " ".join(str(x) for x in result.get("command") or []))
    if result.get("message"):
        lines.append("  说明：" + str(result["message"]))
    if result.get("stderr"):
        lines.append("  stderr：" + str(result["stderr"]).splitlines()[-1])
    return "\n".join(lines)


def _template_vertical(root: Path) -> str:
    body = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return str(body.get("vertical") or root.name)


__all__ = [
    "DEFAULT_RECORD_PATH", "FLEET_LEDGER_SLUGS", "PLACES", "RECORD_SCHEMA",
    "apply_notes", "check_docs", "check_fleet_ledger", "check_notes",
    "derive_notes", "entry_for", "load_record", "publish_command",
    "record_key", "render_publish_result", "run_publish_tool", "state_clause",
]

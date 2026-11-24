"""能力面一致性检查（spec `ecommerce-capability-matrix`，任务 6.2；原 change `truth-ecommerce-capability-surface` 已于 2026-11-10 归档）。

把四处会漂移的东西对到同一个真源上：

  能力矩阵  ←→  报表 preset / 连接器 pack（产物指纹）
  能力矩阵  ←→  对外文档的能力表述（受控锚点）

锚点形态（HTML 注释，人读无感、机器可解析）：

    <!-- capability:taobao.orders=unmapped -->

只对**能力表述那几段**加锚点，不做全文关键词匹配——`grep` 查覆盖率在
本仓已经被证明双向不可靠（误报 + 漏报）。

口径分歧的判法只有一条：锚点写的状态必须等于矩阵当前状态。不等就是漂移，
输出冲突的文档位置与矩阵条目，退出码非零。
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .capability import STATUSES, CapabilityMatrix, load_matrix, matrix_path_for

#: 锚点：平台键允许下划线，流名小写，状态取自三态。
ANCHOR_RE = re.compile(
    r"<!--\s*capability:([a-z][a-z0-9_]*)\.([a-z_]+)=([a-z_]+)\s*-->")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check_template(template_dir: str | Path, *,
                   today: date | None = None,
                   doc_paths: Iterable[str | Path] = (),
                   required_docs: Iterable[str | Path] = ()) -> dict[str, Any]:
    """跑全套检查。返回 `{"ok": bool, "checks": [...], "conflicts": [...]}`。

    失败项逐条带 `fix`——只说「不一致」不说怎么修，等于把问题退回给下一个人。

    `required_docs` 与 `doc_paths` 的区别只有一个：**缺席时算不算失败**。
    带快照兜底的文档（`doc_paths`）本机可以没有活文件，缺席只报「未扫描」；
    跨仓的对外站点源码（`required_docs`）没有快照可退，缺席就必须判红——
    否则「仓不在场 → 扫描面缩小 → 仍然报一致」正是本闸（1.2）要堵的形状。
    """
    root = Path(template_dir)
    checks: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    matrix_file = matrix_path_for(root)
    try:
        matrix = load_matrix(matrix_file, today=today)
    except Exception as exc:  # noqa: BLE001 - 任何加载失败都要变成可读结论
        return {"ok": False, "matrix": str(matrix_file),
                "checks": [{"name": "matrix-load", "ok": False, "detail": str(exc)}],
                "conflicts": [{"kind": "matrix-load", "detail": str(exc),
                               "fix": "修正 capability-matrix.json（见 capability.py 校验信息）"}]}
    checks.append({"name": "matrix-load", "ok": True,
                   "detail": f"{len(matrix.cells)} 格 / {matrix.summary()}"})

    fingerprint = matrix.fingerprint()
    for name, rel in (("report-preset", "report-preset/preset.json"),
                      ("connector-pack", "connector-pack/pack.json")):
        body = _load_json(root / rel)
        if body is None:
            conflicts.append({"kind": "artifact-missing", "artifact": rel,
                              "fix": "重跑 scripts/gen_capability_artifacts.py"})
            checks.append({"name": f"{name}-sync", "ok": False, "detail": "产物缺失"})
            continue
        got = (body.get("_generated_from") or {}).get("fingerprint")
        ok = got == fingerprint
        checks.append({"name": f"{name}-sync", "ok": ok,
                       "detail": "已同步" if ok else f"指纹不符：{got}"})
        if not ok:
            conflicts.append({
                "kind": "artifact-drift", "artifact": rel, "found": got,
                "expected": fingerprint,
                "fix": "重跑 scripts/gen_capability_artifacts.py（矩阵改过就必须重生成）"})

    # preset 的腿必须能在矩阵里找到落点
    preset = _load_json(root / "report-preset" / "preset.json") or {}
    for rep in preset.get("reports", []):
        for sec in rep.get("sections", []):
            flow = sec.get("leg_flow")
            if not flow:
                continue
            if not any((p, flow) in matrix.cells for p in matrix.platform_keys()):
                conflicts.append({
                    "kind": "preset-leg-unbound", "section": sec.get("key"),
                    "flow": flow,
                    "fix": "该腿在矩阵中无任何平台条目：要么补矩阵条目，要么删掉该段"})

    # pack 的能力声明必须与矩阵逐格一致
    pack = _load_json(root / "connector-pack" / "pack.json") or {}
    for platform, info in (pack.get("capability") or {}).get("platforms", {}).items():
        for flow, cell in (info.get("flows") or {}).items():
            want = matrix.cells.get((platform, flow))
            if want is None:
                conflicts.append({"kind": "pack-unknown-cell",
                                  "cell": f"{platform}.{flow}",
                                  "fix": "删除该声明或补矩阵条目"})
            elif cell.get("status") != want.status:
                conflicts.append({
                    "kind": "pack-drift", "cell": f"{platform}.{flow}",
                    "found": cell.get("status"), "expected": want.status,
                    "fix": "重跑 scripts/gen_capability_artifacts.py"})

    # 文档锚点
    anchor_total = 0
    docs_scanned = 0
    docs_missing: list[str] = []
    required = [Path(p) for p in required_docs]
    required_missing: list[str] = []
    # 跨仓必扫文件：缺席即判红（没有快照可退，「未扫到」不等于「没问题」）
    for path in required:
        if not path.is_file():
            required_missing.append(str(path))
            conflicts.append({
                "kind": "required-doc-missing", "doc": str(path),
                "fix": "检出该仓，或把它从 required_docs 撤下并写明为何不再受闸"})
    for doc in list(doc_paths) + [str(p) for p in required]:
        path = Path(doc)
        if not path.is_file():
            docs_missing.append(path.name)
            # required 的缺席已在上面判红，这里只记「未扫描」不再重复造冲突
            checks.append({"name": f"doc:{path.name}", "ok": path not in required,
                           "detail": "文件不存在（**未扫描**，不构成验证）"})
            continue
        docs_scanned += 1
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in ANCHOR_RE.finditer(line):
                anchor_total += 1
                platform, flow, claimed = m.group(1), m.group(2), m.group(3)
                cell = matrix.cells.get((platform, flow))
                if claimed not in STATUSES:
                    conflicts.append({
                        "kind": "anchor-bad-status", "doc": str(path),
                        "line": lineno, "claimed": claimed,
                        "fix": f"锚点状态须为 {'/'.join(STATUSES)}"})
                    continue
                if cell is None:
                    conflicts.append({
                        "kind": "anchor-unknown-cell", "doc": str(path),
                        "line": lineno, "cell": f"{platform}.{flow}",
                        "fix": "矩阵缺该格：要么补条目，要么删锚点"})
                    continue
                if cell.status != claimed:
                    conflicts.append({
                        "kind": "anchor-drift", "doc": str(path), "line": lineno,
                        "cell": f"{platform}.{flow}", "found": claimed,
                        "expected": cell.status, "reason": cell.reason,
                        "fix": "改文档措辞与锚点，使其与矩阵一致（矩阵为准）"})
    if docs_scanned == 0:
        # 「0 个锚点」和「锚点全都对」在输出上必须能分开，否则门空转也报 ok。
        detail = "未扫描任何文档，锚点未验证"
        if docs_missing:
            detail += f"（缺 {len(docs_missing)} 个：{'、'.join(docs_missing[:3])}…）"
    else:
        detail = f"{anchor_total} 个锚点 / 扫描 {docs_scanned} 份文档"
        if docs_missing:
            detail += f"（{len(docs_missing)} 份不在本机未扫描）"
    checks.append({"name": "doc-anchors", "ok": docs_scanned > 0,
                   "detail": detail})
    if required_missing:
        checks.append({"name": "required-docs",
                       "ok": False,
                       "detail": f"{len(required_missing)} 份必扫文件缺席："
                                 f"{'、'.join(Path(p).name for p in required_missing)}"})

    return {"ok": not conflicts, "matrix": str(matrix_file),
            "fingerprint": fingerprint, "summary": matrix.summary(),
            "checks": checks, "conflicts": conflicts}


def render_lint(result: dict[str, Any]) -> str:
    """人读输出：先结论，再逐条冲突与修法。"""
    lines = []
    if result["ok"]:
        lines.append(f"能力面一致（矩阵 {result.get('fingerprint')}，{result.get('summary')}）")
        for c in result["checks"]:
            lines.append(f"  ok  {c['name']}: {c['detail']}")
        return "\n".join(lines)
    lines.append(f"能力面不一致：{len(result['conflicts'])} 处冲突")
    for c in result["conflicts"]:
        where = c.get("doc") or c.get("artifact") or c.get("cell") or c.get("section") or "-"
        if c.get("line"):
            where = f"{where}:{c['line']}"
        lines.append(f"  x   [{c['kind']}] {where}")
        if c.get("cell") and c.get("doc"):
            lines.append(f"        锚点单元格：{c['cell']}")
        if c.get("found") is not None:
            lines.append(f"        文档/产物={c.get('found')}  矩阵={c.get('expected')}")
        if c.get("reason"):
            lines.append(f"        矩阵原因：{c['reason']}")
        lines.append(f"        修法：{c.get('fix')}")
    return "\n".join(lines)

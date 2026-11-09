#!/usr/bin/env python3
"""对外能力声明门（optimize-erp-solution tasks 4.1-4.6）。

四件事，全部数据驱动：

1. **声明必须带证据**（4.1/4.2）：`claims/claims.json` 里每条 claim 的 evidence
   都要能在仓内解析到（端点 / MCP 工具 / 测试名 / 资产 / 代码符号 / 发布记录 /
   发布面）。解析不到，或证据指向的端点仍是 501 占位，即判红并点名该声明。
2. **挂账自动可见**（4.3）：真源 `fd-erp-vertical-api/planned-surfaces.json` 的
   20 条 501/规划车道端点与仓内镜像 `claims/planned-surfaces.json` 逐端点对拍；
   代码新增 501 而镜像未回填 → 红并点名缺失端点。
3. **对外能力清单**（4.4）：`claims/capability-list.md` 两段（已交付 / 规划中）
   由 claims + 挂账清单 + 缺口登记生成；规划中的条数只认挂账清单（20 条），
   不认文档里手写的旧数（15）。
4. **发布口径与不可宣称清单**（4.5/4.6）：`docs/erp/README.md` 第 4、6 节里的
   「不可宣称」与发布状态段落由本脚本从 claims + publish-record 生成，夹在
   `<!-- claim-gate:begin:… -->` 标记之间；内容与生成器输出不一致即判红——
   手写的 live 断言没有生存空间。

扫描对外材料时的判据（`scan`）：命中 assertion 句式（支持开票 / 已上线 …）
而未被任何 claim 的 `patterns` 覆盖 → 未登记声明，判红；否定语境（不 / 未 /
挂账 / DO NOT）不算声明。生成的标记块本身跳过（机器生成的内容由生成器负责）。

用法：
  python3 scripts/claim_gate.py check             # 门（CI 用；退出码 0/1）
  python3 scripts/claim_gate.py check --json
  python3 scripts/claim_gate.py render            # 重写文档标记块 + 能力清单
  python3 scripts/claim_gate.py render --update-planned   # 同时回填挂账镜像
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FINDDATA = REPO.parent
CLAIMS_DIR = REPO / "claims"
CLAIMS_PATH = CLAIMS_DIR / "claims.json"
PLANNED_MIRROR = CLAIMS_DIR / "planned-surfaces.json"
CAPABILITY_LIST = CLAIMS_DIR / "capability-list.md"
BOARD_DOC = FINDDATA / "docs" / "erp" / "README.md"
API_PACKAGE = FINDDATA / "fd-erp-vertical-api" / "fd_erp_vertical_api"
API_PLANNED = FINDDATA / "fd-erp-vertical-api" / "planned-surfaces.json"
TOOL_SURFACE = FINDDATA / "fd-erp-ledger-mcp" / "tool-surface.json"

CLAIMS_SCHEMA = "erp-capability-claims/v1"
MIRROR_SCHEMA = "erp-planned-surfaces-mirror/v1"
STATUSES = ("delivered", "planned", "not_claimable")
EVIDENCE_KINDS = ("endpoint", "mcp_tool", "test", "asset", "code", "release", "surface")

sys.path.insert(0, str(REPO))


# ------------------------------------------------------------------ 加载


def load_claims(path: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(path, dict):
        body = path
    else:
        p = Path(path) if path else CLAIMS_PATH
        body = json.loads(p.read_text(encoding="utf-8"))
    _validate_claims_structure(body)
    return body


def _validate_claims_structure(body: Any) -> None:
    problems: list[str] = []
    if not isinstance(body, dict) or body.get("schema") != CLAIMS_SCHEMA:
        raise SystemExit(f"claims schema 须为 {CLAIMS_SCHEMA}")
    claims = body.get("claims")
    if not isinstance(claims, list) or not claims:
        raise SystemExit("claims 须为非空数组")
    ids: set[str] = set()
    for i, claim in enumerate(claims):
        cid = str(claim.get("id") or "")
        if not cid:
            problems.append(f"claims[{i}] 缺 id")
            continue
        if cid in ids:
            problems.append(f"claim id 重复：{cid}")
        ids.add(cid)
        if str(claim.get("status")) not in STATUSES:
            problems.append(f"{cid}.status 非法：{claim.get('status')}")
        if not str(claim.get("capability") or "").strip():
            problems.append(f"{cid}.capability 必填")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            problems.append(f"{cid}.evidence 须为非空数组（无证据的声明不得发布）")
            continue
        for j, item in enumerate(evidence):
            if not isinstance(item, dict) or item.get("kind") not in EVIDENCE_KINDS:
                problems.append(f"{cid}.evidence[{j}] kind 非法")
            elif not str(item.get("ref") or "").strip():
                problems.append(f"{cid}.evidence[{j}] 缺 ref")
    if problems:
        raise SystemExit("claims 结构非法：" + "；".join(problems))


def load_mirror(path: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(path, dict):
        body = path
    else:
        body = json.loads((Path(path) if path else PLANNED_MIRROR)
                          .read_text(encoding="utf-8"))
    if body.get("schema") != MIRROR_SCHEMA or not isinstance(body.get("endpoints"), list):
        raise SystemExit(f"挂账镜像 schema 须为 {MIRROR_SCHEMA} 且含 endpoints")
    return body


# ------------------------------------------------------------------ 证据解析


class EvidenceIndex:
    """对外材料可引用的证据集合（一次构建，逐 claim 解析）。"""

    def __init__(self, *, workspace: Path = FINDDATA,
                 api_package: Path = API_PACKAGE,
                 tool_surface: Path = TOOL_SURFACE,
                 repo: Path = REPO):
        self.workspace = workspace
        self.repo = repo
        self.routes: dict[str, Any] | None = None
        self.route_error: str | None = None
        try:
            from fd_erp_vertical_templates.coverage import parse_api_routes
            self.routes = {(r.method, _norm(r.path)): r
                           for r in parse_api_routes(api_package)}
        except Exception as exc:  # noqa: BLE001 - 证据源不在位如实报
            self.route_error = str(exc)
        try:
            from fd_erp_vertical_templates.coverage import parse_mcp_tools
            self.tools = set(parse_mcp_tools(tool_surface))
        except Exception:  # noqa: BLE001
            self.tools = set()
        from fd_erp_vertical_templates.coverage import parse_test_names
        try:
            self.tests = set(parse_test_names(repo / "tests"))
        except Exception:  # noqa: BLE001
            self.tests = set()

    def resolve(self, kind: str, ref: str, *, contains: str | None = None
                ) -> dict[str, Any]:
        """返回 {"ok", "implemented", "detail"}；`implemented` 只对端点有意义。"""
        if kind == "endpoint":
            method, _, path = ref.partition(" ")
            if self.routes is None:
                return {"ok": False, "detail": f"端点证据源不在位：{self.route_error}"}
            route = self.routes.get((method.upper(), _norm(path.strip())))
            if route is None:
                return {"ok": False, "detail": f"端点不在服务端实现：{ref}"}
            return {"ok": True, "implemented": not route.placeholder,
                    "detail": (f"{ref} 已实现（{route.source}:{route.line}）"
                               if not route.placeholder else
                               f"{ref} 仍是 501 占位（{route.source}:{route.line}）")}
        if kind == "mcp_tool":
            return ({"ok": True, "detail": f"MCP 工具 {ref} 在册"}
                    if ref in self.tools else
                    {"ok": False, "detail": f"MCP 工具不在工具面：{ref}"})
        if kind == "test":
            return ({"ok": True, "detail": f"测试 {ref} 在册"}
                    if ref in self.tests else
                    {"ok": False, "detail": f"测试名不在 tests/：{ref}"})
        if kind == "asset":
            path = self.repo / ref
            return ({"ok": True, "detail": f"资产在册：{ref}"}
                    if path.exists() else
                    {"ok": False, "detail": f"资产不存在：{ref}"})
        if kind == "code":
            path = self.workspace / ref
            if not path.is_file():
                return {"ok": False, "detail": f"代码文件不存在：{ref}"}
            if contains and contains not in path.read_text(encoding="utf-8",
                                                           errors="ignore"):
                return {"ok": False,
                        "detail": f"代码文件 {ref} 不含符号 {contains!r}"}
            return {"ok": True, "detail": f"代码证据 {ref}"
                    + (f"#{contains}" if contains else "")}
        if kind in ("release", "surface"):
            # 由 check_release 统一处理（需要发布记录上下文）
            return {"ok": True, "detail": "见发布记录核对", "deferred": True}
        return {"ok": False, "detail": f"未知证据类型：{kind}"}


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.strip())


# ------------------------------------------------------------------ 检查


def check(*, claims_path: str | Path | None = None,
          mirror_path: str | Path | None = None,
          record_path: str | Path | None = None,
          board_doc: str | Path | None = None,
          workspace: Path = FINDDATA,
          api_package: Path = API_PACKAGE,
          api_planned: Path = API_PLANNED,
          tool_surface: Path = TOOL_SURFACE) -> dict[str, Any]:
    from fd_erp_vertical_templates.publish import load_record
    from fd_erp_vertical_templates.gaps import check as gaps_check

    claims = load_claims(claims_path)
    mirror = load_mirror(mirror_path)
    record = load_record(record_path)
    doc = Path(board_doc) if board_doc else BOARD_DOC
    index = EvidenceIndex(workspace=workspace, api_package=api_package,
                          tool_surface=tool_surface)
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # ① 证据解析 + 状态一致性
    for claim in claims["claims"]:
        status = claim["status"]
        resolved_any = False
        for item in claim["evidence"]:
            outcome = index.resolve(item["kind"], str(item["ref"]),
                                    contains=item.get("contains"))
            if not outcome["ok"]:
                violations.append(_v("CLAIM_EVIDENCE_UNRESOLVED", claim,
                                     f"{item['kind']}:{item['ref']} — {outcome['detail']}"))
                continue
            resolved_any = True
            if item["kind"] == "endpoint":
                implemented = bool(outcome.get("implemented"))
                if status == "not_claimable" and implemented:
                    violations.append(_v(
                        "CLAIM_STATUS_STALE", claim,
                        f"证据显示 {item['ref']} 已实现，但 claim 仍标 not_claimable"
                        "——状态该升级（或删掉这条不可宣称项）"))
                if status in ("delivered", "planned") and not implemented:
                    violations.append(_v(
                        "CLAIM_EVIDENCE_NOT_IMPLEMENTED", claim,
                        f"声明状态 {status}，但证据端点仍是 501 占位：{item['ref']}"
                        f"——不能拿未实现的端点当证据"))
        if not resolved_any and claim["evidence"]:
            violations.append(_v("CLAIM_NO_RESOLVED_EVIDENCE", claim,
                                 "至少一条证据要指向仓内真实实现（release/surface 类除外）"))

    # ② 发布状态与发布记录（4.5）
    violations.extend(_check_release(claims, record))

    # ③ 挂账清单：代码 501 集合 vs 镜像（4.3）
    violations.extend(_check_planned(mirror, record, index, api_planned))

    # ④ 对外材料扫描（4.2）
    violations.extend(_scan_targets(claims, doc, workspace))

    # ⑤ 生成块漂移（4.6）
    violations.extend(_check_blocks(claims, record, doc))

    # ⑥ 缺口登记（3.8 的数据进能力清单；这里只确认四项齐全，门本身在 gaps.py）
    try:
        g = gaps_check()
        if not g["ok"]:
            for item in g["violations"]:
                violations.append({"code": item["code"], "severity": "error",
                                   "claim": item.get("gap"),
                                   "message": item["message"]})
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        warnings.append({"code": "GAPS_UNAVAILABLE", "severity": "warning",
                         "message": f"缺口登记不可用：{exc}"})

    violations.sort(key=_sort_key)
    warnings.sort(key=_sort_key)
    return {"schema": "erp-claim-gate/v1", "ok": not violations,
            "violations": violations, "warnings": warnings,
            "counts": {"claims": len(claims["claims"]),
                       "planned": len(mirror["endpoints"]),
                       "delivered": sum(1 for c in claims["claims"]
                                        if c["status"] == "delivered"),
                       "not_claimable": sum(1 for c in claims["claims"]
                                            if c["status"] == "not_claimable")}}


def _v(code: str, claim: dict[str, Any], message: str) -> dict[str, Any]:
    return {"code": code, "severity": "error", "claim": claim.get("id"),
            "capability": claim.get("capability"), "message": message}


def _check_release(claims: dict[str, Any], record: dict[str, Any]
                   ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    packs = record.get("packs") or {}
    surfaces = record.get("surfaces") or {}
    for claim in claims["claims"]:
        for item in claim["evidence"]:
            if item["kind"] == "release":
                entry = packs.get(str(item["ref"]))
                if entry is None:
                    out.append(_v("CLAIM_RELEASE_UNRECORDED", claim,
                                  f"证据引用的发布条目 {item['ref']} 不在发布记录里"))
                elif not entry.get("published"):
                    out.append(_v("CLAIM_RELEASE_UNPUBLISHED", claim,
                                  f"证据引用的 {item['ref']} 在发布记录里是未上架，"
                                  "不能作为已上架证据"))
            if item["kind"] == "surface":
                surface = surfaces.get(str(item["ref"]))
                if surface is None:
                    out.append(_v("CLAIM_SURFACE_UNRECORDED", claim,
                                  f"发布面 {item['ref']} 不在发布记录里"))
                elif claim["status"] == "delivered" and not surface.get("live"):
                    out.append(_v("CLAIM_SURFACE_NOT_LIVE", claim,
                                  f"声明已交付，但发布面 {item['ref']} 在记录里仍是未上线"))
        live = claim.get("asserts_live")
        if live and claim["status"] != "not_claimable":
            surface = surfaces.get(str(live)) or {}
            published = any((packs.get(str(i["ref"])) or {}).get("published")
                            for i in claim["evidence"] if i["kind"] == "release")
            if live == "market.connector-packs":
                if not published:
                    out.append(_v("CLAIM_LIVE_UNRECORDED", claim,
                                  "宣称市场已上架，但发布记录里没有任何一条已上架 pack"))
            elif not surface.get("live"):
                out.append(_v("CLAIM_LIVE_UNRECORDED", claim,
                              f"宣称已 live（{live}），但发布记录该面为空或未上线"))
    return out


def _check_planned(mirror: dict[str, Any], record: dict[str, Any],
                   index: EvidenceIndex, api_planned: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    mirror_keys = {(e["method"].upper(), _norm(e["path"])) for e in mirror["endpoints"]}
    # 镜像自洽：条数、规划车道必填、http_status 恒为 501
    if int(mirror.get("count") or 0) != len(mirror["endpoints"]):
        out.append({"code": "PLANNED_COUNT_DRIFT", "severity": "error",
                    "message": (f"挂账镜像 count={mirror.get('count')} 与条目数 "
                                f"{len(mirror['endpoints'])} 不一致")})
    for e in mirror["endpoints"]:
        if not str(e.get("planned_lane") or "").strip():
            out.append({"code": "PLANNED_LANE_MISSING", "severity": "error",
                        "message": f"挂账端点缺规划车道：{e['method']} {e['path']}"})
        if int(e.get("http_status") or 0) != 501:
            out.append({"code": "PLANNED_STATUS_INVALID", "severity": "error",
                        "message": f"挂账端点 http_status 须为 501：{e['method']} {e['path']}"})
    # 与代码实测对拍（源码不在位时如实 unverified，不假绿）
    if index.routes is None:
        out.append({"code": "PLANNED_SOURCE_UNAVAILABLE", "severity": "warning",
                    "message": f"端点证据源不在位，501 集合未核对：{index.route_error}"})
    else:
        live_501 = {(m, p) for (m, p), r in index.routes.items() if r.placeholder}
        for method, path in sorted(live_501 - mirror_keys):
            out.append({"code": "PLANNED_SURFACE_UNREGISTERED", "severity": "error",
                        "message": (f"代码新增 501 端点未登记进挂账清单：{method} {path}"
                                    "（跑 claim_gate render --update-planned）")})
        for method, path in sorted(mirror_keys - live_501):
            out.append({"code": "PLANNED_SURFACE_STALE", "severity": "error",
                        "message": (f"挂账清单端点已不是 501 或已删除：{method} {path}"
                                    "——实现后须从挂账清单移除并进「已交付」")})
    # 与真源文件对拍（存在时）
    if api_planned.is_file():
        source = json.loads(api_planned.read_text(encoding="utf-8"))
        src_keys = {(e["method"].upper(), _norm(e["path"])) for e in source["endpoints"]}
        for method, path in sorted(src_keys - mirror_keys):
            out.append({"code": "PLANNED_SURFACE_UNREGISTERED", "severity": "error",
                        "message": f"真源新增 501 端点未进镜像：{method} {path}"})
        for method, path in sorted(mirror_keys - src_keys):
            out.append({"code": "PLANNED_SURFACE_STALE", "severity": "error",
                        "message": f"镜像里的端点已不在真源：{method} {path}"})
    return out


def _scan_targets(claims: dict[str, Any], doc: Path, workspace: Path
                  ) -> list[dict[str, Any]]:
    scan = claims.get("scan") or {}
    assertions = scan.get("assertions") or []
    negatives = tuple(scan.get("negative_markers") or ())
    patterns = [(c["id"], re.compile(p)) for c in claims["claims"]
                for p in (c.get("patterns") or [])]
    targets: list[Path] = []
    for rel in (claims.get("surfaces") or {}).values():
        # 板文档由调用方（--board-doc）决定；其余面相对 finddata 工作区解析。
        if rel.endswith("docs/erp/README.md"):
            targets.append(doc)
        else:
            targets.append((workspace / rel).resolve())
    out: list[dict[str, Any]] = []
    for target in targets:
        target = target.resolve()
        if not target.is_file():
            out.append({"code": "CLAIM_TARGET_MISSING", "severity": "warning",
                        "message": f"对外材料不在位，未扫描：{target}"})
            continue
        text = target.read_text(encoding="utf-8")
        blocks = _block_spans(text, scan.get("generated_block_markers") or {})
        for match in _iter_assertions(text, assertions):
            pos = match["pos"]
            if any(a <= pos <= b for a, b in blocks):
                continue   # 机器生成的块由生成器负责，扫描不重复判定
            clause = _clause(text, pos)
            prefix = _prefix(text, pos, 60)
            if any(mark in clause or mark in prefix for mark in negatives):
                continue   # 否定语境不是声明
            covered = [cid for cid, pat in patterns if pat.search(clause)]
            if covered:
                continue
            out.append({
                "code": "CLAIM_UNDECLARED", "severity": "error",
                "path": str(target), "assertion": match["id"],
                "statement": clause.strip(),
                "message": (f"未登记的能力声明（{match['id']}）：「{clause.strip()}」"
                            f"（{target.name}）——先在 claims/claims.json 登记并给出证据"),
            })
    # 不可宣称项不得以肯定句式出现（否定语境除外）
    for claim in claims["claims"]:
        if claim["status"] != "not_claimable":
            continue
        for target in targets:
            target = target.resolve()
            if not target.is_file() or target not in _claim_targets(claim, claims, doc):
                continue
            text = target.read_text(encoding="utf-8")
            blocks = _block_spans(text, scan.get("generated_block_markers") or {})
            for pat in claim.get("patterns") or []:
                for m in re.finditer(pat, text):
                    if any(a <= m.start() <= b for a, b in blocks):
                        continue
                    clause = _clause(text, m.start())
                    prefix = _prefix(text, m.start(), 60)
                    if any(mark in clause or mark in prefix for mark in negatives):
                        continue
                    out.append({
                        "code": "CLAIM_FORBIDDEN_ASSERTION", "severity": "error",
                        "claim": claim["id"], "path": str(target),
                        "statement": clause.strip(),
                        "message": (f"不可宣称项以肯定句式出现：「{clause.strip()}」"
                                    f"（{claim['capability']}）"),
                    })
    return out


def _claim_targets(claim: dict[str, Any], claims: dict[str, Any], doc: Path
                   ) -> set[Path]:
    names = claim.get("targets") or []
    out: set[Path] = set()
    for name in names:
        rel = (claims.get("surfaces") or {}).get(name)
        if not rel:
            continue
        if rel.endswith("docs/erp/README.md"):
            out.add(doc.resolve())
        else:
            out.add((FINDDATA / rel).resolve())
    return out


def _iter_assertions(text: str, assertions: list[dict[str, Any]]):
    for item in assertions:
        for m in re.finditer(item["pattern"], text):
            yield {"id": item["id"], "pos": m.start(), "text": m.group(0)}


def _clause(text: str, pos: int) -> str:
    """匹配位置所在的句子（注释块按「连续注释行」拼成一句）。

    为什么要拼注释块：官网组件把纪律写成多行 `//` 注释，
    「不 / 宣称单体 ERP、…、官网已 deploy」跨三行才是一句话。逐行切会把
    否定词留在上一行，把一句否定读成三个肯定声明。普通文档行不拼。
    """
    start, end = _comment_block(text, pos)
    block = text[start:end]
    rel = pos - start
    # 注释块内的换行不是句子边界（多行 `//` 是一句话）；普通文档行才按行切。
    seps = ("。", "；", "|") if _is_comment_line(block) else ("。", "；", "|", "\n")
    left = max(block.rfind(ch, 0, rel) for ch in seps) + 1
    rights = [i for i in (block.find(ch, rel) for ch in seps) if i >= 0]
    right = min(rights) if rights else len(block)
    clause = block[left:right]
    clause = re.sub(r"\s*\n\s*(?://|\*)\s*", " ", clause)   # 折行标记压平
    return clause.strip(" -*>{/")


def _comment_block(text: str, pos: int) -> tuple[int, int]:
    """把 pos 扩展成一段连续注释行（`//` 或 `*` 开头），否则只取本行。"""
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    line_end = len(text) if line_end < 0 else line_end
    if not _is_comment_line(text[line_start:line_end]):
        return line_start, line_end
    start, end = line_start, line_end
    while start > 0:
        prev_end = start - 1
        prev_start = text.rfind("\n", 0, prev_end) + 1
        if not _is_comment_line(text[prev_start:prev_end]):
            break
        start = prev_start
    while end < len(text):
        next_end = text.find("\n", end + 1)
        next_end = len(text) if next_end < 0 else next_end
        if not _is_comment_line(text[end + 1:next_end]):
            break
        end = next_end
    return start, end


def _is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("//", "/*", "*", "{/*"))


def _prefix(text: str, pos: int, width: int = 40) -> str:
    """声明前的同行情境（否定判据只认同行）。

    不跨行是刻意的：跨行前缀会把上一段（很可能是生成的发布状态块）里的
    「未上线（挂账）」当成否定词，从而漏掉紧接其后的真声明——门宁可窄一点、
    可解释，也不要「因为前面出现过『未』所以这句放过」。
    """
    line_start = text.rfind("\n", 0, pos) + 1
    return text[max(line_start, pos - width):pos].replace("//", " ").replace("*", " ")


def _block_spans(text: str, markers: dict[str, list[str]]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pair in markers.values():
        if len(pair) != 2:
            continue
        start = text.find(pair[0])
        end = text.find(pair[1])
        if start >= 0 and end > start:
            spans.append((start, end + len(pair[1])))
    return spans


# ------------------------------------------------------------------ 生成


def render_not_claimable(claims: dict[str, Any]) -> str:
    lines = ["**不可宣称（数据派生，勿手改）**：以下条目由 `claims/claims.json` 与"
             "`publish-record.json` 派生，任何肯定句式出现即被 `scripts/claim_gate.py` 判红。"]
    for claim in claims["claims"]:
        if claim["status"] != "not_claimable":
            continue
        lines.append(f"- `{claim['id']}` {claim['capability']}——{claim.get('reason', '')}")
    for rule in claims.get("negative_rules") or []:
        lines.append(f"- `{rule['id']}` {rule['statement']}——{rule.get('reason', '')}")
    return "\n".join(lines)


def render_release_status(claims: dict[str, Any], record: dict[str, Any],
                          gaps: dict[str, Any] | None = None) -> str:
    packs = record.get("packs") or {}
    surfaces = record.get("surfaces") or {}
    lines = ["**发布状态（数据派生，勿手改）**：唯一出处 `publish-record.json`。"]
    for key in sorted(packs):
        entry = packs[key]
        if entry.get("published"):
            lines.append(f"- `{key}`：已上架 市场 v{entry.get('market_version')}"
                         f"（packId `{entry.get('pack_id')}`，{entry.get('published_at')}）")
        else:
            lines.append(f"- `{key}`：未上架（publish-ready 源清单）")
    for key in ("official-web.deploy", "gateway.rollout", "registry.snapshot"):
        surface = surfaces.get(key) or {}
        state = "已 live" if surface.get("live") else "未上线（挂账）"
        lines.append(f"- `{key}`：{state}——{surface.get('evidence') or '发布记录无该面条目'}")
    if gaps:
        open_gaps = [g for g in gaps.get("gaps") or [] if g.get("status") == "open"]
        for gap in open_gaps:
            lines.append(f"- 缺口 `{gap['id']}`：{gap['capability']}（责任仓 "
                         f"{gap['responsible_repo']}）——对外标注不可用")
    return "\n".join(lines)


def render_capability_list(claims: dict[str, Any], mirror: dict[str, Any],
                           record: dict[str, Any],
                           gaps: dict[str, Any] | None = None) -> str:
    lines = ["# ERP 对外能力清单（数据派生）",
             "",
             "> 由 `scripts/claim_gate.py render` 从 `claims/claims.json`、"
             "`claims/planned-surfaces.json`、`publish-record.json` 与 `gaps.json` 生成，"
             "勿手改。已交付段只列有证据的能力；规划中段的条数以挂账清单为准"
             f"（当前 {len(mirror['endpoints'])} 条），不认文档里手写的旧数。",
             "",
             "## 一、已交付（有证据）",
             ""]
    for claim in claims["claims"]:
        if claim["status"] != "delivered":
            continue
        refs = [f"{i['kind']}:{i['ref']}" for i in claim["evidence"]]
        lines.append(f"- **{claim['capability']}**  \n  证据：{'；'.join(refs)}")
    lines += ["", f"## 二、规划中（{len(mirror['endpoints'])} 条，全部来自挂账清单）", ""]
    lines.append("| 能力面 | 域 | 端点 | 规划车道 |")
    lines.append("| --- | --- | --- | --- |")
    for e in mirror["endpoints"]:
        lane = str(e.get("planned_lane") or "").replace("|", "/")
        lines.append(f"| {e['capability']} | {e['domain']} | "
                     f"`{e['method']} {e['path']}` | {lane} |")
    if gaps:
        from fd_erp_vertical_templates.gaps import load_gaps
        registered = {g["id"]: g for g in load_gaps()["gaps"]}
        open_gaps = [g for g in gaps.get("gaps") or [] if g.get("status") == "open"]
        lines += ["", f"## 三、当前不可用（缺口登记 {len(open_gaps)} 条）", ""]
        for gap in open_gaps:
            blocking = registered.get(gap["id"], {}).get("blocking") or gap.get("verdict", "")
            lines.append(f"- **{gap['capability']}**：{blocking}"
                         f"（责任仓 {gap['responsible_repo']}）")
    lines.append("")
    return "\n".join(lines)


def _check_blocks(claims: dict[str, Any], record: dict[str, Any],
                  doc: Path) -> list[dict[str, Any]]:
    if not doc.is_file():
        return [{"code": "CLAIM_DOC_MISSING", "severity": "warning",
                 "message": f"板块文档不在位，生成块未核对：{doc}"}]
    from fd_erp_vertical_templates.gaps import check as gaps_check
    try:
        gaps = gaps_check()
    except Exception:  # noqa: BLE001
        gaps = None
    text = doc.read_text(encoding="utf-8")
    markers = (claims.get("scan") or {}).get("generated_block_markers") or {}
    wanted = {
        "not-claimable": render_not_claimable(claims),
        "release-status": render_release_status(claims, record, gaps),
    }
    out: list[dict[str, Any]] = []
    for name, expected in wanted.items():
        pair = markers.get(name) or []
        if len(pair) != 2:
            continue
        found = _extract_block(text, pair)
        if found is None:
            out.append({"code": "CLAIM_BLOCK_MISSING", "severity": "error",
                        "block": name,
                        "path": str(doc),
                        "message": f"{doc.name} 缺生成块「{name}」标记——手写口径不可接受"})
        elif found.strip() != expected.strip():
            out.append({"code": "CLAIM_BLOCK_DRIFT", "severity": "error",
                        "block": name, "path": str(doc),
                        "message": (f"{doc.name} 的「{name}」块与生成器输出不一致"
                                    "（跑 claim_gate render 重写）")})
    return out


def _extract_block(text: str, pair: list[str]) -> str | None:
    start = text.find(pair[0])
    end = text.find(pair[1])
    if start < 0 or end < start:
        return None
    return text[start + len(pair[0]):end]


def render(claims: dict[str, Any], record: dict[str, Any], *,
           doc: Path, out_list: Path,
           mirror: dict[str, Any]) -> dict[str, Any]:
    from fd_erp_vertical_templates.gaps import check as gaps_check
    try:
        gaps = gaps_check()
    except Exception:  # noqa: BLE001
        gaps = None
    markers = (claims.get("scan") or {}).get("generated_block_markers") or {}
    text = doc.read_text(encoding="utf-8") if doc.is_file() else ""
    blocks = {
        "not-claimable": render_not_claimable(claims),
        "release-status": render_release_status(claims, record, gaps),
    }
    written: list[str] = []
    for name, body in blocks.items():
        pair = markers.get(name)
        if not pair or len(pair) != 2:
            continue
        block = f"{pair[0]}\n{body}\n{pair[1]}"
        start = text.find(pair[0])
        end = text.find(pair[1])
        if start >= 0 and end > start:
            text = text[:start] + block + text[end + len(pair[1]):]
        else:
            text = text.rstrip("\n") + "\n\n" + block + "\n"
        written.append(name)
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text, encoding="utf-8")
    out_list.write_text(render_capability_list(claims, mirror, record, gaps),
                        encoding="utf-8")
    return {"ok": True, "doc": str(doc), "blocks": written,
            "capability_list": str(out_list)}


def update_planned(*, api_planned: Path = API_PLANNED,
                   mirror_path: Path = PLANNED_MIRROR) -> dict[str, Any]:
    source = json.loads(api_planned.read_text(encoding="utf-8"))
    mirror = {
        "schema": MIRROR_SCHEMA,
        "note": ("挂账清单镜像（task 4.3）：真源是兄弟仓 fd-erp-vertical-api/"
                 "planned-surfaces.json（AST 派生）。本镜像去掉源码行号（行号漂移不是"
                 "缺口变化）；`claim_gate render --update-planned` 重新生成，"
                 "`claim_gate check` 逐端点对拍。"),
        "source": str(api_planned),
        "count": source["count"],
        "by_capability": source["by_capability"],
        "endpoints": [{
            "method": e["method"], "path": e["path"], "capability": e["capability"],
            "domain": e["domain"], "http_status": e.get("http_status", 501),
            "error_code": e.get("error_code", "NOT_IMPLEMENTED"),
            "planned_lane": e.get("planned_lane", ""),
        } for e in source["endpoints"]],
    }
    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    mirror_path.write_text(json.dumps(mirror, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
    return {"ok": True, "count": mirror["count"], "path": str(mirror_path)}


# ------------------------------------------------------------------ CLI


def _sort_key(item: dict[str, Any]) -> tuple:
    return (str(item.get("code")), str(item.get("claim") or ""),
            str(item.get("block") or ""), str(item.get("message") or ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对外能力声明门")
    ap.add_argument("action", choices=("check", "render", "list"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--claims")
    ap.add_argument("--mirror")
    ap.add_argument("--record")
    ap.add_argument("--board-doc", default=None,
                    help="板块文档（缺省 finddata/docs/erp/README.md）")
    ap.add_argument("--api-package", default=str(API_PACKAGE))
    ap.add_argument("--api-planned", default=str(API_PLANNED))
    ap.add_argument("--tool-surface", default=str(TOOL_SURFACE))
    ap.add_argument("--update-planned", action="store_true",
                    help="render 时同时从真源回填挂账镜像")
    args = ap.parse_args(argv)

    if args.action == "check":
        result = check(claims_path=args.claims, mirror_path=args.mirror,
                       record_path=args.record, board_doc=args.board_doc,
                       api_package=Path(args.api_package),
                       api_planned=Path(args.api_planned),
                       tool_surface=Path(args.tool_surface))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            print("能力声明门：" + ("通过" if result["ok"] else
                                   f"阻断（{len(result['violations'])} 处）"))
            print(f"  声明 {result['counts']['claims']} 条 / 已交付 "
                  f"{result['counts']['delivered']} / 不可宣称 "
                  f"{result['counts']['not_claimable']} / 规划中 "
                  f"{result['counts']['planned']}")
            for item in result["violations"]:
                print(f"  x [{item['code']}] {item.get('claim') or item.get('block') or ''}")
                print(f"      {item['message']}")
            for item in result["warnings"]:
                print(f"  ! [{item['code']}] {item['message']}")
        return 0 if result["ok"] else 1

    if args.update_planned:
        print(json.dumps(update_planned(api_planned=Path(args.api_planned)),
                         ensure_ascii=False, indent=1))
    claims = load_claims(args.claims)
    mirror = load_mirror(args.mirror)
    from fd_erp_vertical_templates.publish import load_record
    record = load_record(args.record)
    doc = Path(args.board_doc) if args.board_doc else BOARD_DOC
    if args.action == "list":
        print(render_capability_list(claims, mirror, record))
        return 0
    result = render(claims, record, doc=doc, out_list=CAPABILITY_LIST, mirror=mirror)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

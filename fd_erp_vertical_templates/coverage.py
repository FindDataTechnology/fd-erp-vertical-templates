"""功能点覆盖矩阵（change `optimize-erp-solution` tasks 1.1-1.7，design D1/D2）。

矩阵回答一个问题：「客户这份功能清单，现在到底覆盖了多少、还差什么」。它是
覆盖度、一期分层裁剪与模板覆盖声明的**唯一出处**——三处口径都从这里派生，
不允许各写一份。

四条纪律（每条都对应一条 spec 要求）：

1. **归类只认证据**（D2 / task 1.3）。每个功能点都要解析出至少一条实现证据
   （服务端端点 / MCP 工具 / 模板资产路径 / 测试名）。解析得到且实现就绪 →
   `直接命中`；只解析到契约/资产（端点是 501 占位、只有页面集资产或测试名）
   → `模式复用`；一条都解析不到 → `新建`。人工写「直接命中」但证据解析不出来
   会被校验器打红——标注是自证，不连代码就会腐化。
2. **客户中立**（D1 / task 1.1）。矩阵只存脱敏编号 + 功能域 + 子域 + 归类 +
   证据；客户清单本体只以受控目录的路径引用（`import` 拒绝仓内源文件），
   条目里出现客户名即拒绝并点名。
3. **重算幂等**（task 1.5）。输出无时间戳、排序稳定（`sort_keys` + 稳定点序），
   同一输入两次输出逐字节一致；存在未归类点时矩阵不得定稿、不得导出。
4. **覆盖声明由矩阵派生**（task 1.6）。模板页面集的 `blueprint.coverage`
   计数不是手写数字：它由 `api-contract.md` 的功能点对账表展开成矩阵点，
   再按证据归类后计数；声明与派生值不一致即校验失败并指出模板、域与两处计数。

跨仓证据（服务端端点、MCP 工具）落在 `coverage/evidence.json` 快照里：快照
由 `coverage evidence` 实测生成并入库，这样 CI 单独检出本仓也能重算矩阵，
而不会被「兄弟仓不在位」降级成一律 `新建`。本模块零出网、零直连库表。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import TemplateError

#: 矩阵 schema（客户中性：只含脱敏编号，不含清单原文）。
MATRIX_SCHEMA = "erp-coverage-matrix/v1"
#: 跨仓证据快照 schema。
EVIDENCE_SCHEMA = "erp-coverage-evidence/v1"
#: 分层导出 schema。
LAYERS_SCHEMA = "erp-coverage-layers/v1"
#: 脱敏编号 → 原文映射（只允许落在仓外受控目录）。
MAPPING_SCHEMA = "erp-coverage-id-mapping/v1"

#: 四类归类（spec：每点 MUST 归入其一）。
CLASS_DIRECT = "直接命中"
CLASS_PATTERN = "模式复用"
CLASS_NEW = "新建"
CLASS_NOT_DOING = "不做"
CLASSIFICATIONS = (CLASS_DIRECT, CLASS_PATTERN, CLASS_NEW, CLASS_NOT_DOING)
#: 计入「已覆盖」的两类。
CLASS_COVERED = (CLASS_DIRECT, CLASS_PATTERN)

#: 四类证据源（task 1.2）。
EVIDENCE_ENDPOINT = "endpoint"
EVIDENCE_MCP_TOOL = "mcp_tool"
EVIDENCE_TEMPLATE_ASSET = "template_asset"
EVIDENCE_TEST = "test"
EVIDENCE_TYPES = (EVIDENCE_ENDPOINT, EVIDENCE_MCP_TOOL,
                  EVIDENCE_TEMPLATE_ASSET, EVIDENCE_TEST)

#: 分层导出：归类 → 文件键（四层）。
LAYERS = (
    (CLASS_DIRECT, "direct-hit"),
    (CLASS_PATTERN, "pattern-reuse"),
    (CLASS_NEW, "new-build"),
    (CLASS_NOT_DOING, "not-doing"),
)

#: 脱敏编号格式（不含任何客户可识别信息）。
_ID_RE = re.compile(r"^FP-[0-9A-F]{10}$")
_PARAM_RE = re.compile(r"\{[^}]*\}")
_ENDPOINT_REF_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)\s*$")
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
#: api-contract 端点行：`| GET | \`/path\` | ... |`
_CONTRACT_ROW_RE = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`")
#: 标注「出处不在锚点枚举里」的功能域（spec 附加 / 别的 change 的 tasks 关联）。
_FOREIGN_PROVENANCE_RE = re.compile(r"（[^）]*(?:spec|tasks)[^）]*）")
#: 法人实体后缀：客户名检测的通用规则（不靠名单也能拦住公司全称）。
_CUSTOMER_SUFFIX_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}(?:有限公司|股份有限公司)")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COVERAGE_DIR = REPO_ROOT / "coverage"
DEFAULT_EVIDENCE_PATH = DEFAULT_COVERAGE_DIR / "evidence.json"
DEFAULT_MATRIX_DIR = DEFAULT_COVERAGE_DIR / "matrices"
DEFAULT_LAYERS_DIR = DEFAULT_COVERAGE_DIR / "layers"
DEFAULT_CUSTOMER_MARKERS_PATH = DEFAULT_COVERAGE_DIR / "customer-markers.json"

#: 端点默认扫描面（task 1.2：fd-erp-vertical-api 的 app.py / faces.py）。
API_MODULE_FILES = ("app.py", "faces.py")


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class Route:
    """一条服务端端点（装饰器实测）。"""

    method: str
    path: str
    function: str
    placeholder: bool
    source: str
    line: int

    @property
    def ref(self) -> str:
        return f"{self.method} {self.path}"

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "path": self.path,
                "function": self.function, "placeholder": self.placeholder,
                "source": self.source, "line": self.line}


@dataclass(frozen=True)
class EvidenceRef:
    """一条证据引用（类型 + 引用值）。"""

    kind: str
    ref: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.kind, "ref": self.ref}
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class Resolution:
    """一次证据解析结果（resolved=解析到；implemented=实现就绪）。"""

    kind: str
    ref: str
    resolved: bool
    implemented: bool | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.kind, "ref": self.ref, "resolved": self.resolved,
                "implemented": self.implemented, "detail": self.detail}


@dataclass(frozen=True)
class MatrixPoint:
    """一个功能点（脱敏编号 + 功能域/子域 + 归类 + 证据）。"""

    id: str
    domain: str
    subdomain: str
    classification: str
    evidence: tuple[EvidenceRef, ...] = ()
    surface: str = ""
    anchor: bool = True
    row_status: str = ""
    not_doing_reason: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "domain": self.domain,
            "subdomain": self.subdomain,
            "classification": self.classification,
            "evidence": [e.as_dict() for e in self.evidence],
        }
        if self.surface:
            out["surface"] = self.surface
        if not self.anchor:
            out["anchor"] = False
        if self.row_status:
            out["row_status"] = self.row_status
        if self.not_doing_reason:
            out["not_doing_reason"] = self.not_doing_reason
        if self.note:
            out["note"] = self.note
        return out

    def sort_key(self) -> tuple[str, ...]:
        return (self.surface, self.domain, self.subdomain, self.id)


@dataclass
class CoverageMatrix:
    """矩阵本体（点集 + 定稿标记 + 出处）。"""

    vertical: str
    points: list[MatrixPoint] = field(default_factory=list)
    finalized: bool = False
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": MATRIX_SCHEMA,
            "vertical": self.vertical,
            "source": self.source,
            "finalized": self.finalized,
            "points": [p.as_dict() for p in sorted(self.points,
                                                   key=lambda p: p.sort_key())],
        }


# ------------------------------------------------------------------ 解析器


def normalize_endpoint_path(path: str) -> str:
    """把 `{product_id}` / `{productId}` 统一成 `{}`（参数名不参与比对）。"""
    return _PARAM_RE.sub("{}", path.strip())


def parse_api_routes(api_package_dir: str | Path) -> list[Route]:
    """枚举服务端端点：解析 app.py / faces.py 的路由装饰器（AST，不 import）。

    占位判定：处理函数体里出现 `NOT_IMPLEMENTED` 或 `_not_implemented(` ——
    501 占位端点因此不会被算成「实现就绪」。
    """
    pkg = Path(api_package_dir)
    if not pkg.is_dir():
        raise TemplateError("NOT_FOUND", f"服务端端点证据源不在位：{pkg}")
    routes: list[Route] = []
    for name in API_MODULE_FILES:
        path = pkg / name
        if not path.is_file():
            raise TemplateError("NOT_FOUND", f"服务端端点证据源缺文件：{path}")
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(text, node) or ""
            placeholder = ("NOT_IMPLEMENTED" in body) or ("_not_implemented(" in body)
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not isinstance(func, ast.Attribute) or func.attr not in _HTTP_METHODS:
                    continue
                if not dec.args or not isinstance(dec.args[0], ast.Constant):
                    continue
                if not isinstance(dec.args[0].value, str):
                    continue
                routes.append(Route(method=func.attr.upper(),
                                    path=dec.args[0].value,
                                    function=node.name,
                                    placeholder=placeholder,
                                    source=name, line=dec.lineno))
    routes.sort(key=lambda r: (r.path, r.method))
    return routes


def parse_mcp_tools(tool_surface_path: str | Path) -> list[str]:
    """枚举 MCP 工具：读 tool-surface.json 的 tool_annotations 键集。"""
    path = Path(tool_surface_path)
    if not path.is_file():
        raise TemplateError("NOT_FOUND", f"MCP 工具面证据源不在位：{path}")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{path}: 不是合法 JSON：{exc}") from exc
    tools = body.get("tool_annotations")
    if not isinstance(tools, dict) or not tools:
        raise TemplateError("INVALID_ARGUMENT",
                            f"{path}: 缺 tool_annotations（MCP 工具面唯一出处）")
    return sorted(tools)


def parse_test_names(tests_dir: str | Path) -> list[str]:
    """枚举测试名：tests/ 下 test_*.py 里的 test_* 函数名（去重排序）。"""
    root = Path(tests_dir)
    if not root.is_dir():
        raise TemplateError("NOT_FOUND", f"测试名证据源不在位：{root}")
    names: set[str] = set()
    for path in sorted(root.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:  # 语法错的测试文件不猜，直接报
            raise TemplateError("INVALID_ARGUMENT", f"{path}: 解析失败：{exc}") from exc
        for node in tree.body:  # 只认模块级测试函数，嵌套函数不是测试
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name.startswith("test_"):
                names.add(node.name)
    return sorted(names)


def parse_template_assets(templates_root: str | Path) -> dict[str, dict[str, Any]]:
    """枚举模板资产：每个模板的四件套 + 扩展件路径与页面集页面 id。

    返回 `{模板名: {"assets": {件名: 相对路径}, "pagesets": {端: [页面 id]}}}`，
    路径相对模板目录（与 manifest 同口径）。
    """
    root = Path(templates_root)
    if not root.is_dir():
        raise TemplateError("NOT_FOUND", f"模板资产证据源不在位：{root}")
    out: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            body = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TemplateError("INVALID_ARGUMENT",
                                f"{manifest_path}: 不是合法 JSON：{exc}") from exc
        template_dir = manifest_path.parent
        assets: dict[str, str] = {}
        for kind, asset in (body.get("assets") or {}).items():
            if isinstance(asset, dict) and str(asset.get("path") or "").strip():
                assets[kind] = str(asset["path"])
        pagesets: dict[str, list[str]] = {}
        for ps_manifest in sorted((template_dir / "pagesets").glob("*/manifest.json")):
            try:
                ps = json.loads(ps_manifest.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"{ps_manifest}: 不是合法 JSON：{exc}") from exc
            surface = _surface_of(ps, ps_manifest.parent.name)
            pagesets[surface] = sorted(p["id"] for p in ps.get("pages") or []
                                       if isinstance(p, dict) and p.get("id"))
        out[template_dir.name] = {"assets": assets, "pagesets": pagesets}
    return out


def _surface_of(pageset_manifest: dict[str, Any], dir_name: str) -> str:
    """端标识：优先 `authorization.pagesetKey`（`<vertical>.<end>`），退化到目录名前缀。"""
    key = str((pageset_manifest.get("authorization") or {}).get("pagesetKey") or "")
    if "." in key:
        return key.rsplit(".", 1)[-1]
    return dir_name.split("-")[0]


def build_evidence_snapshot(*, repo_root: str | Path | None = None,
                            api_package: str | Path | None = None,
                            tool_surface: str | Path | None = None) -> dict[str, Any]:
    """实测跨仓证据并生成快照（无时间戳：同一代码产生同一字节）。"""
    root = Path(repo_root or REPO_ROOT).resolve()
    api_pkg = Path(api_package) if api_package else \
        root.parent / "fd-erp-vertical-api" / "fd_erp_vertical_api"
    tools_path = Path(tool_surface) if tool_surface else \
        root.parent / "fd-erp-ledger-mcp" / "tool-surface.json"
    routes = parse_api_routes(api_pkg)
    tools = parse_mcp_tools(tools_path)
    health = sorted({r.path for r in routes if r.path in ("/health", "/healthz")})
    return {
        "schema": EVIDENCE_SCHEMA,
        "measured_from": {
            "api_package": _rel_to(api_pkg, root),
            "tool_surface": _rel_to(tools_path, root),
        },
        "counts": {
            "routes": len(routes),
            "placeholder_routes": sum(1 for r in routes if r.placeholder),
            "health_probes": health,
            "mcp_tools": len(tools),
        },
        "routes": [r.as_dict() for r in routes],
        "mcp_tools": tools,
    }


def _rel_to(path: Path, root: Path) -> str:
    """仓相对路径（跨仓用 ../ 形式；只为溯源，不含机器相关信息以外的语义）。"""
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        try:
            return "../" + path.resolve().relative_to(root.parent).as_posix()
        except ValueError:
            return path.name


# ------------------------------------------------------------------ 证据索引


@dataclass
class EvidenceIndex:
    """证据解析器：端点 / MCP 工具走快照，模板资产与测试名走本仓实测。"""

    routes: dict[tuple[str, str], Route]
    mcp_tools: frozenset[str]
    repo_root: Path
    test_names: frozenset[str] = frozenset()
    counts: dict[str, Any] = field(default_factory=dict)
    _json_cache: dict[Path, dict[str, Any]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any], *,
                      repo_root: str | Path | None = None,
                      include_tests: bool = True) -> "EvidenceIndex":
        if snapshot.get("schema") != EVIDENCE_SCHEMA:
            raise TemplateError("INVALID_ARGUMENT",
                                f"证据快照 schema 须为 {EVIDENCE_SCHEMA}",
                                detail={"found": snapshot.get("schema")})
        root = Path(repo_root or REPO_ROOT).resolve()
        routes: dict[tuple[str, str], Route] = {}
        for raw in snapshot.get("routes") or []:
            route = Route(method=str(raw["method"]).upper(),
                          path=str(raw["path"]),
                          function=str(raw.get("function") or ""),
                          placeholder=bool(raw.get("placeholder")),
                          source=str(raw.get("source") or ""),
                          line=int(raw.get("line") or 0))
            routes[(route.method, normalize_endpoint_path(route.path))] = route
        tests = frozenset(parse_test_names(root / "tests")) if include_tests else frozenset()
        return cls(routes=routes,
                   mcp_tools=frozenset(snapshot.get("mcp_tools") or []),
                   repo_root=root, test_names=tests,
                   counts=dict(snapshot.get("counts") or {}))

    @classmethod
    def live(cls, *, repo_root: str | Path | None = None,
             api_package: str | Path | None = None,
             tool_surface: str | Path | None = None) -> "EvidenceIndex":
        snapshot = build_evidence_snapshot(repo_root=repo_root,
                                           api_package=api_package,
                                           tool_surface=tool_surface)
        return cls.from_snapshot(snapshot, repo_root=repo_root)

    # ---- 解析面 ----

    def resolve(self, ref: EvidenceRef) -> Resolution:
        if ref.kind == EVIDENCE_ENDPOINT:
            return self._resolve_endpoint(ref)
        if ref.kind == EVIDENCE_MCP_TOOL:
            ok = ref.ref.strip() in self.mcp_tools
            return Resolution(ref.kind, ref.ref, ok, True if ok else None,
                              "" if ok else "工具不在 tool-surface.json")
        if ref.kind == EVIDENCE_TEMPLATE_ASSET:
            return self._resolve_asset(ref)
        if ref.kind == EVIDENCE_TEST:
            ok = ref.ref.strip() in self.test_names
            return Resolution(ref.kind, ref.ref, ok, None,
                              "" if ok else "测试名不在 tests/")
        return Resolution(ref.kind, ref.ref, False, None,
                          f"未知证据类型：{ref.kind}")

    def _resolve_endpoint(self, ref: EvidenceRef) -> Resolution:
        match = _ENDPOINT_REF_RE.match(ref.ref.strip())
        if not match:
            return Resolution(ref.kind, ref.ref, False, False,
                              "端点引用须形如 `GET /path`")
        method, path = match.group(1), normalize_endpoint_path(match.group(2))
        route = self.routes.get((method, path))
        if route is None:
            return Resolution(ref.kind, ref.ref, False, False, "端点不在服务端路由表")
        if route.placeholder:
            return Resolution(ref.kind, ref.ref, True, False,
                              f"端点 501 占位（{route.source}:{route.line}）")
        return Resolution(ref.kind, ref.ref, True, True,
                          f"{route.source}:{route.line} {route.function}")

    def _resolve_asset(self, ref: EvidenceRef) -> Resolution:
        path_part, _, fragment = ref.ref.strip().partition("#")
        target = (self.repo_root / path_part).resolve()
        if not target.is_file():
            return Resolution(ref.kind, ref.ref, False, None,
                              f"模板资产不存在：{path_part}")
        if not fragment:
            return Resolution(ref.kind, ref.ref, True, None, "")
        kind, _, value = fragment.partition("/")
        if kind != "page":
            return Resolution(ref.kind, ref.ref, False, None,
                              f"未知资产片段：#{fragment}（支持 #page/<id>）")
        body = self._json(target)
        ids = {p["id"] for p in body.get("pages") or []
               if isinstance(p, dict) and p.get("id")}
        if value in ids:
            return Resolution(ref.kind, ref.ref, True, None, "")
        return Resolution(ref.kind, ref.ref, False, None,
                          f"页面集无页面 {value}")

    def _json(self, path: Path) -> dict[str, Any]:
        if path not in self._json_cache:
            try:
                self._json_cache[path] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"{path}: 不是合法 JSON：{exc}") from exc
        return self._json_cache[path]


def load_evidence(path: str | Path | None = None, *,
                  repo_root: str | Path | None = None,
                  include_tests: bool = True) -> EvidenceIndex:
    """加载入库证据快照（缺快照 fail-closed：不静默降级成「一律新建」）。"""
    target = Path(path) if path else (Path(repo_root or REPO_ROOT) / "coverage" / "evidence.json")
    if not target.is_file():
        raise TemplateError(
            "NOT_FOUND",
            f"跨仓证据快照不在位：{target}（先跑 `coverage evidence` 实测生成并入库）")
    try:
        snapshot = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{target}: 不是合法 JSON：{exc}") from exc
    return EvidenceIndex.from_snapshot(snapshot, repo_root=repo_root,
                                       include_tests=include_tests)


def classify_evidence(evidence: Sequence[EvidenceRef],
                      index: EvidenceIndex | None) -> str:
    """按证据归类（D2：不靠人工标注）。"""
    if not evidence:
        return CLASS_NEW
    if index is None:
        return CLASS_NEW
    resolutions = [index.resolve(e) for e in evidence]
    if any(r.resolved and r.implemented for r in resolutions):
        return CLASS_DIRECT
    if any(r.resolved for r in resolutions):
        return CLASS_PATTERN
    return CLASS_NEW


# ------------------------------------------------------------ api-contract 解析


@dataclass(frozen=True)
class ContractRow:
    """功能点覆盖对账表的一行（功能域 + 承载页面 + 状态）。"""

    domain: str
    pages: tuple[str, ...]
    status: str
    anchor: bool


@dataclass
class PagesetContract:
    """一份页面集 api-contract.md 的结构化视图。"""

    path: Path
    sections: list[dict[str, Any]] = field(default_factory=list)
    rows: list[ContractRow] = field(default_factory=list)

    def anchor_rows(self) -> list[ContractRow]:
        return [r for r in self.rows if r.anchor]

    def section_domains(self) -> list[str]:
        out: list[str] = []
        for sec in self.sections:
            for dom in sec.get("domains") or []:
                if dom not in out:
                    out.append(dom)
        return out

    def endpoints(self) -> list[str]:
        out: list[str] = []
        for sec in self.sections:
            for method, path in sec.get("endpoints") or []:
                ref = f"{method} {path}"
                if ref not in out:
                    out.append(ref)
        return out


def parse_pageset_contract(path: str | Path) -> PagesetContract:
    """解析页面集契约：功能点分节（域 + 端点）与「功能点覆盖对账」表。

    锚点域判定：对账表的功能域单元格若带 `（… spec …）` / `（… tasks …）` 标注，
    说明它出自别的 spec/任务而不是锚点蓝图枚举，**不计入**锚点域计数
    （supply-chain 实测：ue 7 / se 3 / sp 4 / cp 4，与 blueprint.coverage 一致）。
    """
    contract_path = Path(path)
    if not contract_path.is_file():
        raise TemplateError("NOT_FOUND", f"页面集缺 api-contract.md：{contract_path}")
    contract = PagesetContract(path=contract_path)
    section: dict[str, Any] | None = None
    in_reconcile = False
    for line in contract_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            in_reconcile = "功能点覆盖对账" in title
            section = {"title": title, "domains": _section_domains(title),
                       "endpoints": []}
            contract.sections.append(section)
            continue
        if in_reconcile:
            row = _parse_reconcile_row(line)
            if row is not None:
                contract.rows.append(row)
            continue
        if section is None:
            continue
        match = _CONTRACT_ROW_RE.match(line)
        if match:
            section["endpoints"].append((match.group(1), match.group(2)))
    if not contract.rows:
        raise TemplateError("INVALID_ARGUMENT",
                            f"{contract_path}: 缺「功能点覆盖对账」表（覆盖声明无出处）")
    return contract


def _section_domains(title: str) -> list[str]:
    """分节标题里的功能点域：`## 1. 商品域（功能点：商品检索 / 商品详情）`。"""
    if "功能点：" not in title:
        return []
    body = title.split("功能点：", 1)[1].rstrip("） \t")
    head = re.split(r"[；;]", body)[0]
    out = []
    for part in head.split(" / "):
        name = part.strip().rstrip("）").strip()
        if name:
            out.append(name)
    return out


def _parse_reconcile_row(line: str) -> ContractRow | None:
    if not line.startswith("|"):
        return None
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 3:
        return None
    if cells[0].startswith("功能域") or set(cells[0]) <= set("-: "):
        return None
    cell = cells[0]
    pages = tuple(p.strip() for p in cells[1].split(" / ") if p.strip())
    status = cells[2].strip()
    if not cell or not pages:
        return None
    return ContractRow(domain=_domain_name(cell), pages=pages, status=status,
                       anchor=not _FOREIGN_PROVENANCE_RE.search(cell))


def _domain_name(cell: str) -> str:
    return re.sub(r"（[^）]*）\s*$", "", cell).strip()


# -------------------------------------------------------- 模板矩阵与声明派生


def _template_vertical(template_dir: Path) -> str:
    manifest = template_dir / "manifest.json"
    if manifest.is_file():
        try:
            body = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TemplateError("MANIFEST_INVALID",
                                f"{manifest}: 不是合法 JSON：{exc}") from exc
        vertical = str(body.get("vertical") or "").strip()
        if vertical:
            return vertical
    return template_dir.name


def _point_id(*parts: str) -> str:
    blob = "\x1f".join(parts).encode("utf-8")
    return "FP-" + hashlib.sha256(blob).hexdigest()[:10].upper()


def build_template_matrix(template_dir: str | Path,
                          evidence: EvidenceIndex | None) -> CoverageMatrix:
    """由页面集契约 + 证据解析派生模板覆盖矩阵（同一输入 → 同一字节）。"""
    tpl = Path(template_dir)
    vertical = _template_vertical(tpl)
    pagesets_root = tpl / "pagesets"
    points: list[MatrixPoint] = []
    if pagesets_root.is_dir():
        for ps_dir in sorted(p for p in pagesets_root.iterdir()
                             if (p / "manifest.json").is_file()):
            try:
                ps = json.loads((ps_dir / "manifest.json").read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise TemplateError("MANIFEST_INVALID",
                                    f"{ps_dir}/manifest.json: 不是合法 JSON：{exc}") from exc
            surface = _surface_of(ps, ps_dir.name)
            contract = parse_pageset_contract(ps_dir / "api-contract.md")
            pages = {p["id"]: p for p in ps.get("pages") or []
                     if isinstance(p, dict) and p.get("id")}
            rel = f"templates/{vertical}/pagesets/{ps_dir.name}/manifest.json"
            for row in contract.rows:
                for page_id in row.pages:
                    points.append(_build_point(
                        vertical=vertical, surface=surface, row=row,
                        page_id=page_id, page=pages.get(page_id),
                        asset_path=rel, evidence=evidence))
    return CoverageMatrix(vertical=vertical, points=points,
                          source=f"templates/{vertical}",
                          finalized=True)


def _build_point(*, vertical: str, surface: str, row: ContractRow,
                 page_id: str, page: dict[str, Any] | None,
                 asset_path: str, evidence: EvidenceIndex | None) -> MatrixPoint:
    asset_ref = f"{asset_path}#page/{page_id}"
    refs: list[EvidenceRef] = []
    if page is None:
        # 覆盖声明引用了页面集清单里不存在的页面：证据只剩那条解析不到的资产引用。
        refs.append(EvidenceRef(EVIDENCE_TEMPLATE_ASSET, asset_ref,
                                note="页面集清单缺该页面"))
    else:
        refs.append(EvidenceRef(EVIDENCE_TEMPLATE_ASSET, asset_ref))
        for api in page.get("apis") or []:
            if not isinstance(api, str):
                continue
            match = _ENDPOINT_REF_RE.match(api.strip())
            if match:
                refs.append(EvidenceRef(EVIDENCE_ENDPOINT,
                                        f"{match.group(1)} {match.group(2)}"))
    # 证据去重排序：同一端点被多页共用时矩阵不重复计数。
    unique = {(r.kind, r.ref, r.note): r for r in refs}
    ordered = tuple(unique[k] for k in sorted(unique))
    classification = classify_evidence(ordered, evidence)
    note = ""
    if classification == CLASS_PATTERN and evidence is not None:
        placeholders: list[str] = []
        for ref in ordered:
            if ref.kind != EVIDENCE_ENDPOINT:
                continue
            resolution = evidence.resolve(ref)
            if resolution.resolved and not resolution.implemented:
                placeholders.append(ref.ref)
        if placeholders:
            note = "端点 501 占位（契约在、实现未就绪）：" + "、".join(placeholders)
    return MatrixPoint(
        id=_point_id(vertical, surface, row.domain, page_id),
        domain=row.domain, subdomain=page_id, classification=classification,
        evidence=ordered, surface=surface, anchor=row.anchor,
        row_status=row.status, note=note)


def coverage_counts(matrix: CoverageMatrix) -> dict[str, Any]:
    """四层计数 + 按端（页面集）的域级计数。"""
    layer_counts = {c: 0 for c in CLASSIFICATIONS}
    for point in matrix.points:
        if point.classification in layer_counts:
            layer_counts[point.classification] += 1
    surfaces: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[str, str], list[MatrixPoint]] = {}
    for point in matrix.points:
        if point.surface and point.anchor:
            groups.setdefault((point.surface, point.domain), []).append(point)
    for (surface, domain), pts in sorted(groups.items()):
        bucket = surfaces.setdefault(surface, {
            "coveredAreas": 0, "pendingTrimAreas": 0, "uncoveredAreas": 0,
            "covered": [], "pending_trim": [], "uncovered": []})
        if any(p.classification in CLASS_COVERED for p in pts):
            bucket["coveredAreas"] += 1
            bucket["covered"].append(domain)
        elif all(p.classification == CLASS_NOT_DOING for p in pts):
            bucket["pendingTrimAreas"] += 1
            bucket["pending_trim"].append(domain)
        else:
            bucket["uncoveredAreas"] += 1
            bucket["uncovered"].append(domain)
    return {
        "schema": MATRIX_SCHEMA,
        "vertical": matrix.vertical,
        "finalized": matrix.finalized,
        "layer_counts": layer_counts,
        "surfaces": surfaces,
        "totals": {
            "points": len(matrix.points),
            "unclassified": len(unclassified_ids(matrix)),
            "coveredAreas": sum(s["coveredAreas"] for s in surfaces.values()),
            "pendingTrimAreas": sum(s["pendingTrimAreas"] for s in surfaces.values()),
            "uncoveredAreas": sum(s["uncoveredAreas"] for s in surfaces.values()),
        },
    }


def check_template_coverage_declaration(
        template_dir: str | Path, *,
        evidence: EvidenceIndex | None = None,
        matrix: CoverageMatrix | None = None,
        repo_root: str | Path | None = None) -> list[str]:
    """校验模板覆盖声明（blueprint 域级计数）是否与矩阵派生值一致。

    返回违规清单（空 = 通过）。三类检查：
    1. 契约分节里的功能域必须在「功能点覆盖对账」表回填（新增域未回填即红）；
    2. 声明计数 vs 现算派生计数（新增/删除承载页面即红，指出两处计数）；
    3. 入库矩阵 vs 契约端点（契约新增端点但矩阵未重算即红，点名域）。
    """
    tpl = Path(template_dir)
    pagesets_root = tpl / "pagesets"
    if not pagesets_root.is_dir():
        return []
    ps_dirs = sorted(p for p in pagesets_root.iterdir() if (p / "manifest.json").is_file())
    if not ps_dirs:
        return []
    vertical = _template_vertical(tpl)
    index = evidence if evidence is not None else load_evidence(repo_root=repo_root)
    if matrix is None:
        matrix = _load_committed_matrix(vertical, repo_root=repo_root)
    violations: list[str] = []
    derived_matrix = build_template_matrix(tpl, index)
    derived = coverage_counts(derived_matrix)["surfaces"]
    committed = coverage_counts(matrix)["surfaces"] if matrix is not None else {}

    for ps_dir in ps_dirs:
        try:
            ps = json.loads((ps_dir / "manifest.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TemplateError("MANIFEST_INVALID",
                                f"{ps_dir}/manifest.json: 不是合法 JSON：{exc}") from exc
        surface = _surface_of(ps, ps_dir.name)
        contract = parse_pageset_contract(ps_dir / "api-contract.md")
        anchor_domains = [r.domain for r in contract.anchor_rows()]
        blob = "；".join(f"{r.domain}({r.status})→{','.join(r.pages)}"
                         for r in contract.anchor_rows())

        # ① 分节功能域必须在对账表回填。
        for domain in contract.section_domains():
            if not any(domain == a or domain.startswith(a) for a in anchor_domains):
                violations.append(
                    f"{vertical}/{surface}: 功能域「{domain}」未在 api-contract.md "
                    f"功能点覆盖对账表回填（新增域未回填，覆盖声明无从派生）")

        # ② 声明计数 vs 派生计数。
        declared = ((ps.get("blueprint") or {}).get("coverage") or {})
        got = derived.get(surface, {"coveredAreas": 0, "pendingTrimAreas": 0,
                                    "uncoveredAreas": 0})
        for field_name, key in (("coveredAreas", "coveredAreas"),
                                ("pendingTrimAreas", "pendingTrimAreas")):
            if field_name not in declared:
                violations.append(
                    f"{vertical}/{surface}: blueprint.coverage 缺 {field_name}"
                    f"（覆盖声明须由矩阵派生，派生值 {got.get(key, 0)}）")
                continue
            if int(declared[field_name] or 0) != int(got.get(key, 0)):
                violations.append(
                    f"{vertical}/{surface}: 域级计数与矩阵派生值不一致"
                    f"（blueprint.coverage.{field_name} 声明 "
                    f"{int(declared[field_name] or 0)} ≠ 派生 {int(got.get(key, 0))}；"
                    f"对账表域={blob}）")
        if got.get("uncoveredAreas"):
            violations.append(
                f"{vertical}/{surface}: 有 {got['uncoveredAreas']} 个锚点域解析不到证据"
                f"（{', '.join(got.get('uncovered') or [])}）——先补证据或标「不做」并写理由")

        # ③ 入库矩阵 vs 契约端点（新增端点未重算矩阵即红）。
        # 只核对声明了「功能点」的分节：工作台/对账协助/结算查询等辅助面不是锚点
        # 功能点，本就不进覆盖计数（进了反而虚高）。
        if surface in committed:
            matrix_refs = _matrix_endpoint_refs(matrix, surface)
            for section in contract.sections:
                if not section.get("domains"):
                    continue
                for method, path in section.get("endpoints") or []:
                    ref = f"{method} {path}"
                    if ref not in matrix_refs:
                        violations.append(
                            f"{vertical}/{surface}: 契约端点「{ref}」未进覆盖矩阵"
                            f"（新增端点未回填，矩阵需重算：`coverage build --template …`）")
    return violations


def _matrix_endpoint_refs(matrix: CoverageMatrix, surface: str) -> set[str]:
    refs: set[str] = set()
    for point in matrix.points:
        if point.surface != surface:
            continue
        for ref in point.evidence:
            if ref.kind == EVIDENCE_ENDPOINT:
                match = _ENDPOINT_REF_RE.match(ref.ref)
                if match:
                    refs.add(f"{match.group(1)} {match.group(2)}")
    return refs


def _load_committed_matrix(vertical: str, *,
                           repo_root: str | Path | None = None) -> CoverageMatrix | None:
    path = Path(repo_root or REPO_ROOT) / "coverage" / "matrices" / f"{vertical}.json"
    if not path.is_file():
        return None
    return load_matrix(path)


# ------------------------------------------------------------------ 矩阵 I/O


def matrix_dumps(matrix: CoverageMatrix) -> str:
    """矩阵序列化：稳定排序 + 无时间戳（重算幂等靠这里）。"""
    return json.dumps(matrix.as_dict(), ensure_ascii=False, indent=2,
                      sort_keys=True) + "\n"


def matrix_fingerprint(matrix: CoverageMatrix) -> str:
    return "sha256:" + hashlib.sha256(matrix_dumps(matrix).encode("utf-8")).hexdigest()


def save_matrix(matrix: CoverageMatrix, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(matrix_dumps(matrix), encoding="utf-8")
    return target


def load_matrix(source: str | Path | dict[str, Any]) -> CoverageMatrix:
    """加载矩阵（结构校验只查形状；归类与证据一致由 `validate_matrix` 管）。"""
    if isinstance(source, dict):
        body = source
    else:
        path = Path(source)
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TemplateError("MATRIX_INVALID", f"矩阵不可读：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise TemplateError("MATRIX_INVALID", f"矩阵不是合法 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise TemplateError("MATRIX_INVALID", "矩阵须为对象")
    if body.get("schema") != MATRIX_SCHEMA:
        raise TemplateError("MATRIX_INVALID", f"schema 须为 {MATRIX_SCHEMA}",
                            detail={"found": body.get("schema")})
    if not str(body.get("vertical") or "").strip():
        raise TemplateError("MATRIX_INVALID", "缺 vertical")
    raw_points = body.get("points")
    if not isinstance(raw_points, list):
        raise TemplateError("MATRIX_INVALID", "points 须为数组")
    points: list[MatrixPoint] = []
    for i, raw in enumerate(raw_points):
        if not isinstance(raw, dict):
            raise TemplateError("MATRIX_INVALID", f"points[{i}] 须为对象")
        refs: list[EvidenceRef] = []
        raw_evidence = raw.get("evidence")
        if not isinstance(raw_evidence, list):
            raise TemplateError("MATRIX_INVALID",
                                f"{raw.get('id') or f'points[{i}]'}: 缺 evidence 数组")
        for j, ev in enumerate(raw_evidence):
            if not isinstance(ev, dict) or not str(ev.get("type") or "").strip() \
                    or not str(ev.get("ref") or "").strip():
                raise TemplateError("MATRIX_INVALID",
                                    f"{raw.get('id') or f'points[{i}]'}: evidence[{j}] 需 type + ref")
            refs.append(EvidenceRef(kind=str(ev["type"]), ref=str(ev["ref"]),
                                    note=str(ev.get("note") or "")))
        points.append(MatrixPoint(
            id=str(raw.get("id") or "").strip(),
            domain=str(raw.get("domain") or "").strip(),
            subdomain=str(raw.get("subdomain") or "").strip(),
            classification=str(raw.get("classification") or "").strip(),
            evidence=tuple(refs),
            surface=str(raw.get("surface") or "").strip(),
            anchor=bool(raw.get("anchor", True)),
            row_status=str(raw.get("row_status") or "").strip(),
            not_doing_reason=str(raw.get("not_doing_reason") or "").strip(),
            note=str(raw.get("note") or "").strip(),
        ))
    return CoverageMatrix(vertical=str(body["vertical"]).strip(), points=points,
                          finalized=bool(body.get("finalized")),
                          source=str(body.get("source") or ""))


# ------------------------------------------------------------- 矩阵校验/定稿


def load_customer_markers(path: str | Path | None = None) -> tuple[str, ...]:
    """客户名denylist（只放名字，不放清单本体；可用环境变量追加）。

    环境变量 `ERP_COVERAGE_CUSTOMER_MARKERS` 逗号分隔，追加到文件名单之后。
    """
    markers: list[str] = []
    target = Path(path) if path else DEFAULT_CUSTOMER_MARKERS_PATH
    if target.is_file():
        try:
            body = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TemplateError("INVALID_ARGUMENT",
                                f"{target}: 不是合法 JSON：{exc}") from exc
        for item in body.get("markers") or []:
            if str(item).strip():
                markers.append(str(item).strip())
    import os
    env = os.environ.get("ERP_COVERAGE_CUSTOMER_MARKERS") or ""
    markers.extend(m.strip() for m in env.split(",") if m.strip())
    deduped: list[str] = []
    for m in markers:
        if m not in deduped:
            deduped.append(m)
    return tuple(deduped)


def _customer_hits(text: str, markers: Sequence[str]) -> list[str]:
    hits = [m for m in markers if m and m in text]
    match = _CUSTOMER_SUFFIX_RE.search(text)
    if match:
        hits.append(match.group(0))
    return hits


def unclassified_ids(matrix: CoverageMatrix) -> list[str]:
    """未归类点编号（归类缺失/非法，或缺功能域/子域）。"""
    out: list[str] = []
    for point in matrix.points:
        if point.classification not in CLASSIFICATIONS or \
                not point.domain or not point.subdomain:
            out.append(point.id or f"<无编号:{point.domain or '?'}>")
    return sorted(out)


def validate_matrix(matrix: CoverageMatrix, *,
                    evidence: EvidenceIndex | None = None,
                    customer_markers: Sequence[str] | None = None) -> None:
    """校验矩阵（形状 + 客户中性 + 归类由证据决定 + 定稿前置）。"""
    markers = tuple(customer_markers) if customer_markers is not None \
        else load_customer_markers()
    violations: list[str] = []
    seen: set[str] = set()
    for point in matrix.points:
        label = point.id or "<无编号>"
        if not _ID_RE.match(point.id):
            violations.append(f"{label}: 功能点编号须为脱敏编号形态 FP-XXXXXXXXXX")
        if point.id in seen:
            violations.append(f"{label}: 编号重复")
        seen.add(point.id)
        if point.classification not in CLASSIFICATIONS:
            violations.append(
                f"{label}: 未归类（classification={point.classification or '<空>'}，"
                f"合法值：{'/'.join(CLASSIFICATIONS)}）")
        if not point.domain or not point.subdomain:
            violations.append(f"{label}: 未归类（缺功能域或子域）")
        if point.classification in CLASS_COVERED and not point.evidence:
            # 「新建」按定义就是解析不到证据，不可能带证据；只有落进已覆盖两类的点
            # 才必须附证据——否则「直接命中」可以只靠一句话成立。
            violations.append(
                f"{label}: {point.classification} 必须附至少一条实现证据"
                f"（能力名/端点/模板资产/测试名）")
        for ev in point.evidence:
            if ev.kind not in EVIDENCE_TYPES:
                violations.append(
                    f"{label}: 证据类型非法 {ev.kind!r}（合法：{'/'.join(EVIDENCE_TYPES)}）")
        if point.classification == CLASS_NOT_DOING and not point.not_doing_reason:
            violations.append(f"{label}: 标「不做」必须带理由（not_doing_reason）")
        # 归类只能由证据解析得出，不接受手写标注。
        if evidence is not None and point.classification in CLASSIFICATIONS:
            recomputed = classify_evidence(point.evidence, evidence)
            if recomputed != point.classification:
                violations.append(
                    f"{label}: 归类与证据不符（声明 {point.classification}，"
                    f"证据解析为 {recomputed}）——归类不靠人工标注")
        for field_name, text in (("domain", point.domain),
                                 ("subdomain", point.subdomain),
                                 ("note", point.note),
                                 ("not_doing_reason", point.not_doing_reason)):
            for hit in _customer_hits(text, markers):
                violations.append(
                    f"{label}: 含客户名「{hit}」（{field_name}）——矩阵必须客户中性")
    if matrix.finalized and unclassified_ids(matrix):
        violations.append(
            f"矩阵标为定稿但存在未归类点：{', '.join(unclassified_ids(matrix))}")
    if violations:
        raise TemplateError("MATRIX_INVALID", "；".join(violations),
                            detail={"violations": violations,
                                    "unclassified": unclassified_ids(matrix)})


def finalize_matrix(matrix: CoverageMatrix) -> CoverageMatrix:
    """定稿：存在未归类点即拒绝（并点名编号）。"""
    pending = unclassified_ids(matrix)
    if pending:
        raise TemplateError("MATRIX_INVALID",
                            f"存在未归类点，矩阵不得定稿：{', '.join(pending)}",
                            detail={"unclassified": pending})
    matrix.finalized = True
    return matrix


# --------------------------------------------------------------- 受控导入


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def import_external_list(external: str | Path, *,
                         out_matrix: str | Path,
                         out_mapping: str | Path,
                         repo_root: str | Path | None = None,
                         salt: str | None = None,
                         evidence: EvidenceIndex | None = None,
                         customer_markers: Sequence[str] | None = None,
                         vertical: str = "imported") -> dict[str, Any]:
    """受控导入外部功能清单，生成脱敏编号矩阵 + 仓外映射。

    - 源清单 MUST 位于仓外（客户清单本体不入仓，D1）；
    - 矩阵只含脱敏编号 + 功能域/子域 + 归类 + 证据，不含清单原文；
    - 编号由 `sha256(salt + 原文)` 派生，salt 存仓外映射文件；
    - 同一源 + 同一 salt 两次导入逐字节一致。
    """
    root = Path(repo_root or REPO_ROOT).resolve()
    src = Path(external)
    if not src.is_file():
        raise TemplateError("INVALID_ARGUMENT", f"外部清单不存在：{src}")
    if _is_within(src.resolve(), root):
        raise TemplateError(
            "INVALID_ARGUMENT",
            f"外部清单不得位于仓内：{src}（客户清单本体不入仓，只以仓外路径引用）")
    mapping_path = Path(out_mapping)
    if _is_within(mapping_path.resolve(), root):
        raise TemplateError(
            "INVALID_ARGUMENT",
            f"编号→原文映射必须落仓外：{mapping_path}（原文入仓即泄漏客户清单）")
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{src}: 不是合法 JSON：{exc}") from exc
    entries = raw.get("points") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise TemplateError("INVALID_ARGUMENT",
                            f"{src}: 清单须为非空数组（或 {{'points': [...]}}）")

    # salt 优先取显式入参，其次复用已有映射文件（重导入编号不变），否则新生成。
    existing = _read_mapping(mapping_path)
    if not salt:
        salt = str(existing.get("salt") or "") or secrets.token_hex(16)

    points: list[MatrixPoint] = []
    mapping_entries: dict[str, Any] = dict(existing.get("entries") or {})
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TemplateError("INVALID_ARGUMENT", f"{src}: points[{i}] 须为对象")
        text = str(entry.get("text") or "").strip()
        if not text:
            raise TemplateError("INVALID_ARGUMENT",
                                f"{src}: points[{i}] 缺 text（原文只落仓外映射）")
        point_id = _import_id(salt, text)
        refs: list[EvidenceRef] = []
        for j, ev in enumerate(entry.get("evidence") or []):
            if not isinstance(ev, dict) or not str(ev.get("type") or "").strip() \
                    or not str(ev.get("ref") or "").strip():
                raise TemplateError("INVALID_ARGUMENT",
                                    f"{src}: points[{i}].evidence[{j}] 需 type + ref")
            refs.append(EvidenceRef(kind=str(ev["type"]), ref=str(ev["ref"])))
        classification = classify_evidence(refs, evidence)
        reason = str(entry.get("not_doing_reason") or "").strip()
        if entry.get("classification") == CLASS_NOT_DOING or reason:
            classification = CLASS_NOT_DOING if reason else classification
        points.append(MatrixPoint(
            id=point_id,
            domain=str(entry.get("domain") or "").strip(),
            subdomain=str(entry.get("subdomain") or "").strip(),
            classification=classification, evidence=tuple(refs),
            surface=str(entry.get("surface") or "").strip(),
            anchor=bool(entry.get("anchor", True)),
            not_doing_reason=reason))
        mapping_entries[point_id] = {
            "text": text,
            "domain": str(entry.get("domain") or ""),
            "subdomain": str(entry.get("subdomain") or ""),
            "source": src.name,
        }

    matrix = CoverageMatrix(vertical=vertical, points=points,
                            source="external:imported", finalized=False)
    # 客户中性 + 形状校验先跑：含客户名即拒绝并点名（此时尚未写矩阵）。
    validate_matrix(matrix, evidence=evidence, customer_markers=customer_markers)
    finalize_matrix(matrix)
    save_matrix(matrix, out_matrix)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(
        json.dumps({"schema": MAPPING_SCHEMA, "salt": salt,
                    "entries": mapping_entries}, ensure_ascii=False,
                   indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "points": len(points),
        "layers": coverage_counts(matrix)["layer_counts"],
        "matrix": str(Path(out_matrix)),
        "mapping": str(mapping_path),
        "fingerprint": matrix_fingerprint(matrix),
    }


def _import_id(salt: str, text: str) -> str:
    blob = f"{salt}\x1f{text}".encode("utf-8")
    return "FP-" + hashlib.sha256(blob).hexdigest()[:10].upper()


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------- 分层导出


def validate_numeric_claims(claims: Iterable[dict[str, Any]],
                            repo_root: str | Path | None = None) -> None:
    """价格/工期/客户规模数字必须带仓内可核验出处，无出处即失败。"""
    root = Path(repo_root or REPO_ROOT).resolve()
    violations: list[str] = []
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            violations.append(f"claims[{i}]: 须为对象（field/value/source）")
            continue
        field_name = str(claim.get("field") or "").strip()
        value = claim.get("value")
        source = str(claim.get("source") or "").strip()
        label = field_name or f"claims[{i}]"
        if value is None or str(value).strip() == "":
            violations.append(f"{label}: 缺 value")
            continue
        if not source:
            violations.append(f"{label}: 缺 source（价格/工期/客户规模数字须带仓内出处）")
            continue
        target = (root / source).resolve()
        if not _is_within(target, root) or not target.is_file():
            violations.append(f"{label}: source 不在仓内或不存在：{source}")
            continue
        text = target.read_text(encoding="utf-8", errors="ignore")
        digits = re.sub(r"[^0-9.]", "", str(value))
        variants = {str(value).strip(), digits}
        if digits and "." not in digits:
            variants.add(f"{int(digits):,}")
        if not any(v and v in text for v in variants):
            violations.append(
                f"{label}: source {source} 内找不到该数字（{value}）——出处不可核验")
    if violations:
        raise TemplateError("INVALID_ARGUMENT",
                            "导出包含无出处数字：" + "；".join(violations),
                            detail={"missing_source_fields": violations})


def export_layers(matrix: CoverageMatrix, out_dir: str | Path, *,
                  repo_root: str | Path | None = None,
                  commercial_claims: Iterable[dict[str, Any]] | None = None,
                  customer_markers: Sequence[str] | None = None) -> dict[str, Any]:
    """导出四层清单（直接命中 / 模式复用 / 新建 / 不做）。

    矩阵有未归类点或未定稿即拒绝导出（并返回未归类编号）；导出内容只含
    功能点编号与证据；带价格/工期/规模数字时必须过 `validate_numeric_claims`。
    """
    pending = unclassified_ids(matrix)
    if pending:
        raise TemplateError("MATRIX_INVALID",
                            f"矩阵仍有未归类点，拒绝导出：{', '.join(pending)}",
                            detail={"unclassified": pending})
    if not matrix.finalized:
        raise TemplateError("MATRIX_INVALID", "矩阵未定稿，拒绝导出（先 finalize）")
    validate_matrix(matrix, customer_markers=customer_markers)
    claims = list(commercial_claims or [])
    if claims:
        validate_numeric_claims(claims, repo_root=repo_root)

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    index_layers: list[dict[str, Any]] = []
    for classification, key in LAYERS:
        records = [
            {"id": p.id, "evidence": [e.as_dict() for e in sorted(
                p.evidence, key=lambda e: (e.kind, e.ref))]}
            for p in sorted(matrix.points, key=lambda p: p.sort_key())
            if p.classification == classification
        ]
        body = {"schema": LAYERS_SCHEMA, "layer": classification,
                "vertical": matrix.vertical, "count": len(records),
                "points": records}
        (target / f"{key}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        index_layers.append({"key": key, "layer": classification,
                             "file": f"{key}.json", "count": len(records)})
    index = {
        "schema": LAYERS_SCHEMA,
        "vertical": matrix.vertical,
        "matrix_fingerprint": matrix_fingerprint(matrix),
        "commercial_claims": claims,
        "layers": index_layers,
        "totals": {entry["layer"]: entry["count"] for entry in index_layers},
    }
    (target / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return index

"""模板边界审计（erp-vertical-templates 任务 1.4）。

机械化证明模板层四条边界（模板 spec「模板与内核边界」+ 清单 constraints）：
1. 无旁路账本：模板包不存账务事实（无余额/分录类落盘），账务只经内核工具面。
2. 无直连库表：不 import 任何 DB 驱动；不出现内核表名的 SQL 字符串。
3. 回写仅经内核白名单车道：舰队蓝图里账本回写工具 ⊆ 内核 8 工具白名单，
   且 confirm_proposal（确认门）只允许出现在拍板席位；写回路径是
   submit_proposal（proposal 待确认），不存在直写落账。
4. 对外部系统零写入：连接器只有 read 面（无 write/push/post_ 方法）。

审计是纯静态（AST + JSON），不需要运行模板代码，只读不写。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .kernel_client import KernelClient  # noqa: F401  （审计基准：唯一合法账务通道）

KERNEL_TOOL_WHITELIST = frozenset({
    "submit_proposal", "list_proposals", "confirm_proposal", "reject_proposal",
    "query_balance", "query_entries", "run_reconciliation", "get_recon_report",
})
#: 确认门只属于拍板席位（owner 拍板）；巡检/核对/纠偏岗只提交 proposal。
DECIDER_TITLE_MARKS = ("拍板", "owner", "Owner")
DB_DRIVER_MODULES = frozenset({
    "psycopg", "psycopg2", "pymongo", "sqlite3", "mysql", "MySQLdb",
    "sqlalchemy", "duckdb", "redis", "pyodbc",
})
KERNEL_TABLE_NAMES = ("journal_entries", "proposals", "recon_reports",
                      "recon_task_items", "fx_facts", "universal_accounts")
EXTERNAL_WRITE_METHOD_PREFIXES = ("write", "push", "post_", "put_", "patch_", "delete_")


def audit_template(package_dir: str | Path, asset_dirs: list[str | Path] | None = None
                   ) -> dict[str, Any]:
    """审计模板层边界。返回 {ok, checks:[{name,ok,detail}], violations:[...]}。

    - package_dir：模板 Python 包目录（fd_erp_vertical_templates）。
    - asset_dirs：四件套资产目录（舰队蓝图/pack 清单 JSON 等）。
    """
    checks: list[dict[str, Any]] = []
    violations: list[str] = []

    pkg = Path(package_dir)
    py_files = sorted(pkg.rglob("*.py"))
    checks.append(_audit_no_db_drivers(py_files, violations))
    checks.append(_audit_no_kernel_table_sql(py_files, violations))
    checks.append(_audit_kernel_via_client_only(py_files, violations))
    checks.append(_audit_connectors_readonly(pkg, violations))

    for asset_dir in (asset_dirs or []):
        checks.append(_audit_fleet_blueprints(Path(asset_dir), violations))

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks, "violations": violations}


# ---------------------------------------------------------------------------
# Python 包静态面
# ---------------------------------------------------------------------------

def _audit_no_db_drivers(py_files: list[Path], violations: list[str]) -> dict[str, Any]:
    """边界 2a：零直连库表 —— 不 import 任何 DB 驱动。"""
    found: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in DB_DRIVER_MODULES:
                        found.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in DB_DRIVER_MODULES:
                    found.append(f"{path.name}: from {node.module}")
    ok = not found
    if not ok:
        violations.extend(found)
    return {"name": "no-direct-db-drivers", "ok": ok,
            "detail": "模板包零 DB 驱动 import（账务只经内核工具面）" if ok else found}


def _audit_no_kernel_table_sql(py_files: list[Path],
                               violations: list[str]) -> dict[str, Any]:
    """边界 2b：字符串常量中不得同时出现内核表名与 SQL 动词
    （杜绝手写 SQL 绕过工具面；纯文档提及不带 SQL 动词不误伤）。"""
    found: list[str] = []
    verbs = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                upper = text.upper()
                for table in KERNEL_TABLE_NAMES:
                    if table in text and any(v in upper for v in verbs):
                        found.append(f"{path.name}: 字符串常量含内核表 {table} "
                                     "与 SQL 动词（疑似直连库表）")
                        break
    ok = not found
    if not ok:
        violations.extend(found)
    return {"name": "no-kernel-table-sql", "ok": ok,
            "detail": "无内核表名 SQL 字符串（AST 常量扫描）" if ok else found}


def _audit_kernel_via_client_only(py_files: list[Path],
                                  violations: list[str]) -> dict[str, Any]:
    """边界 1/3 前置：模板包不 import 内核实现（fd_erp_ledger_mcp）。

    账务通道唯一合法形态 = kernel_client.KernelClient（MCP 工具面同形协议）。
    直接 import 内核实现 = 绕过工具面抽象，按违规计。
    """
    found: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "fd_erp_ledger_mcp":
                        found.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "fd_erp_ledger_mcp":
                    found.append(f"{path.name}: from {node.module}")
    ok = not found
    if not ok:
        violations.extend(found)
    return {"name": "kernel-via-client-only", "ok": ok,
            "detail": "模板包不 import 内核实现；账务只经 KernelClient（MCP 同形）" if ok else found}


def _audit_connectors_readonly(pkg: Path, violations: list[str]) -> dict[str, Any]:
    """边界 4：连接器只有 read 面 —— 适配器类不得定义外部写方法。"""
    writes = _collect_connector_writes(pkg)
    ok = not writes
    if not ok:
        violations.extend(writes)
    return {"name": "connectors-readonly", "ok": ok,
            "detail": "连接器零外部写方法（read 面only，零外部写入实证）" if ok else writes}


def _collect_connector_writes(pkg: Path) -> list[str]:
    found: list[str] = []
    connectors = pkg / "connectors"
    if not connectors.is_dir():
        return found
    for path in sorted(connectors.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for prefix in EXTERNAL_WRITE_METHOD_PREFIXES:
                    if node.name.startswith(prefix):
                        found.append(f"connectors/{path.name}: 方法 {node.name}() "
                                     f"疑似外部写面（连接器只读滩头）")
    return found


# ---------------------------------------------------------------------------
# 舰队蓝图静态面
# ---------------------------------------------------------------------------

def _audit_fleet_blueprints(asset_dir: Path, violations: list[str]) -> dict[str, Any]:
    """边界 3：回写仅经内核白名单车道。

    - 引用 fd-erp-ledger-mcp 的 mcpServers.allowTools ⊆ 内核 8 工具白名单。
    - confirm_proposal（确认门）只允许出现在拍板席位（title 含 拍板/owner）；
      巡检/核对/纠偏岗只能 submit_proposal（proposal 待确认）。
    """
    found: list[str] = []
    for path in sorted(asset_dir.rglob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TemplateError("BOUNDARY_VIOLATION",
                                f"{path}: 蓝图 JSON 非法：{exc}") from exc
        _audit_one_blueprint(body, str(path), found)
    ok = not found
    if not ok:
        violations.extend(found)
    return {"name": f"fleet-writeback-whitelist({asset_dir})", "ok": ok,
            "detail": "回写工具 ⊆ 内核白名单；确认门只在拍板席位" if ok else found}


def _audit_one_blueprint(body: dict[str, Any], rel: str,
                         found: list[str]) -> None:
    """白名单车道审计（pack 级 allowTools 口径）：
    - 内核工具 ⊆ 8 工具白名单；
    - allowTools 含 confirm_proposal（确认门）时，fleet 必须显式声明拍板席位
      （成员 title 含 拍板/owner）——核对/巡检岗只 submit_proposal，纪律在
      persona 文本（机器可查的是席位存在性与工具闭包）。"""
    for server in body.get("mcpServers", []) or []:
        if str(server.get("registryName") or "") != "fd-erp-ledger-mcp":
            continue
        tools = server.get("allowTools") or []
        for tool in tools:
            if tool not in KERNEL_TOOL_WHITELIST:
                found.append(f"{rel}: 非白名单内核工具 {tool}")
        if "confirm_proposal" in tools:
            members = (body.get("fleet") or {}).get("members") or []
            titles = [str(m.get("title") or "") for m in members]
            has_decider = any(
                any(mark in title for mark in DECIDER_TITLE_MARKS)
                for title in titles)
            if not has_decider:
                found.append(
                    f"{rel}: allowTools 含 confirm_proposal 但 fleet 无拍板席位"
                    "（title 须含 拍板/owner）——确认门只属拍板席位")

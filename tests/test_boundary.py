"""模板边界审计（任务 1.4 / 5.1）：无旁路账本、无直连库表、回写白名单车道、
连接器只读 —— 正例全过 + 负例全拦。"""
from __future__ import annotations

import json
from pathlib import Path

from fd_erp_vertical_templates.boundary import audit_template, _audit_one_blueprint

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "fd_erp_vertical_templates"


def test_real_package_passes():
    """正例：模板包本体 + 两套模板资产全过（机械化证明四条边界）。"""
    report = audit_template(PKG, [REPO / "templates" / "crossborder",
                                  REPO / "templates" / "restaurant-chain"])
    assert report["ok"], report["violations"]
    names = {c["name"] for c in report["checks"]}
    assert {"no-direct-db-drivers", "no-kernel-table-sql", "kernel-via-client-only",
            "connectors-readonly"} <= names
    assert any(c["name"].startswith("fleet-writeback-whitelist") for c in report["checks"])


def test_db_driver_import_flagged(tmp_path):
    """负例：psycopg import = 直连库表，审计必须拦下。"""
    (tmp_path / "rogue.py").write_text("import psycopg\n", encoding="utf-8")
    report = audit_template(tmp_path)
    assert not report["ok"]
    assert any("psycopg" in v for v in report["violations"])


def test_kernel_table_sql_flagged(tmp_path):
    """负例：手写内核表 SQL = 旁路工具面，审计必须拦下。"""
    (tmp_path / "rogue.py").write_text(
        'SQL = "SELECT * FROM journal_entries WHERE tenant_id = %s"\n',
        encoding="utf-8")
    report = audit_template(tmp_path)
    assert not report["ok"]
    assert any("journal_entries" in v for v in report["violations"])


def test_kernel_import_flagged(tmp_path):
    """负例：模板包 import 内核实现 = 绕过工具面抽象，审计必须拦下。"""
    (tmp_path / "rogue.py").write_text("from fd_erp_ledger_mcp import proposals\n",
                                       encoding="utf-8")
    report = audit_template(tmp_path)
    assert not report["ok"]
    assert any("fd_erp_ledger_mcp" in v for v in report["violations"])


def test_connector_write_method_flagged(tmp_path):
    """负例：连接器出现外部写方法 = 违反只读滩头，审计必须拦下。"""
    connectors = tmp_path / "connectors"
    connectors.mkdir()
    (connectors / "rogue.py").write_text(
        "class Rogue:\n"
        "    def write_order(self, order):\n"
        "        return order\n",
        encoding="utf-8")
    (tmp_path / "ok.py").write_text("X = 1\n", encoding="utf-8")
    report = audit_template(tmp_path)
    assert not report["ok"]
    assert any("write_order" in v for v in report["violations"])


def _blueprint(members_titles, allow_tools, mcp_only_kernel=True):
    servers = [{"registryName": "fd-erp-ledger-mcp", "allowTools": allow_tools}]
    if not mcp_only_kernel:
        servers.append({"registryName": "fd-info", "allowTools": ["list_since"]})
    return {
        "mcpServers": servers,
        "fleet": {"members": [{"ref": f"m{i}", "title": t}
                              for i, t in enumerate(members_titles)]},
    }


def test_blueprint_confirm_requires_decider():
    """负例：allowTools 含 confirm_proposal 但无拍板席位 → 拦下。"""
    bp = _blueprint(["巡检岗", "核对岗"],
                    ["submit_proposal", "confirm_proposal"])
    found: list[str] = []
    _audit_one_blueprint(bp, "bp.json", found)
    assert any("拍板席位" in v for v in found)


def test_blueprint_whitelist_subset():
    """正例 + 负例：8 工具白名单内全过；非白名单工具拦下。"""
    bp = _blueprint(["核对岗", "拍板岗（owner）"],
                    ["submit_proposal", "confirm_proposal", "reject_proposal",
                     "query_balance", "query_entries", "get_recon_report",
                     "run_reconciliation", "list_proposals"])
    found: list[str] = []
    _audit_one_blueprint(bp, "bp.json", found)
    assert found == []
    rogue = _blueprint(["拍板岗（owner）"], ["confirm_proposal", "drop_all_tables"])
    found2: list[str] = []
    _audit_one_blueprint(rogue, "bp.json", found2)
    assert any("drop_all_tables" in v for v in found2)


def test_real_fleet_blueprints_pass():
    """正例：两支真实舰队蓝图过白名单车道审计（拍板席位在编）。"""
    from fd_erp_vertical_templates.boundary import _audit_fleet_blueprints
    violations: list[str] = []
    for d in (REPO / "templates" / "crossborder",
              REPO / "templates" / "restaurant-chain"):
        r = _audit_fleet_blueprints(d, violations)
        assert r["ok"], violations
    assert violations == []

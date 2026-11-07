"""supply-chain 模板资产测试（add-supply-chain-vertical 任务 7.3 + 蓝图）。

- 三模板 manifest validate 全过（含扩展 pack 类型）
- 科目映射 pack 加载与呈现层重算
- 报表 preset：指标口径单一出处 + 四组报表 + 阈值规则
- 舰队蓝图格式 + 边界审计（回写白名单 / 确认门只在拍板席位 / 连接器只读）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fd_erp_vertical_templates.manifest import load_template_manifest
from fd_erp_vertical_templates.mapping import load_mapping_pack, apply_mapping
from fd_erp_vertical_templates.reports import load_report_preset
from fd_erp_vertical_templates.boundary import audit_template
from fd_erp_vertical_templates.errors import TemplateError

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates"


@pytest.mark.parametrize("name", ["crossborder", "restaurant-chain", "supply-chain"])
def test_three_templates_validate(name):
    manifest = load_template_manifest(TEMPLATES / name)
    assert manifest["vertical"] == name
    assert manifest["assets"]["fleet_blueprint"]["version"]


def test_supply_chain_manifest_has_extension_packs():
    manifest = load_template_manifest(TEMPLATES / "supply-chain")
    assets = manifest["assets"]
    # 四件套 + 扩展件：取价规则 pack / 页面集 pack
    for kind in ("connector_pack", "fleet_blueprint", "account_mapping_pack",
                 "report_preset", "pricing_rules_pack", "pagesets_pack"):
        assert kind in assets, kind
    assert assets["pricing_rules_pack"]["kind"] == "rules-pack"
    assert assets["pagesets_pack"]["kind"] == "pageset-pack"


def test_supply_mapping_pack_load_and_apply():
    body = load_template_manifest(TEMPLATES / "supply-chain")
    pack_path = TEMPLATES / "supply-chain" / body["assets"]["account_mapping_pack"]["path"]
    pack = load_mapping_pack(pack_path)
    assert pack["vertical"] == "supply-chain"
    # 呈现层重算：内核余额 → 行业口径（映射不改账）
    balances = [
        {"account_code": "4000", "debit_total": 0, "credit_total": 500,
         "balance": -500},
        {"account_code": "1200", "debit_total": 800, "credit_total": 300,
         "balance": 500},
        {"account_code": "9999", "debit_total": 1, "credit_total": 0,
         "balance": 1},  # 未映射科目如实列 unmapped
    ]
    out = apply_mapping(balances, pack)
    keys = {r["row_key"] for r in out["rows"]}
    assert "revenue" in keys and "working-capital" in keys
    assert any(u["account_code"] == "9999" for u in out["unmapped_balances"])
    assert "呈现层口径" in out["caliber_note"]


def test_supply_report_preset_calibers_and_reports():
    preset = load_report_preset(TEMPLATES / "supply-chain" / "report-preset"
                                / "preset.json")
    # 指标口径统一定义：每个口径带定义出处（单一出处字段）
    calibers = {c["key"]: c for c in preset["metric_calibers"]}
    for key in ("sales_amount", "gross_margin", "inventory_turnover", "ar_aging"):
        assert key in calibers, key
        assert calibers[key]["definition_source"]
    # 四组报表：采购与库存/销售与商品/收入成本毛利/结算与资金
    keys = [r["key"] for r in preset["reports"]]
    assert keys == ["sc-procurement-inventory", "sc-sales-product",
                    "sc-revenue-cost-margin", "sc-settlement-funds"]
    for rep in preset["reports"]:
        assert rep["caliber"]  # 每组报表带取数口径声明
    # 每行带行级口径标注
    for rep in preset["reports"]:
        for sec in rep["sections"]:
            assert sec.get("row_caliber"), (rep["key"], sec["key"])
    # 阈值预警规则：毛利异常/库存积压/逾期应收 → 处置任务
    metrics = {a["metric"] for a in preset["threshold_alerts"]}
    assert metrics == {"gross_margin", "inventory_stagnation",
                       "overdue_receivable"}
    assert all(a["disposal_task"] for a in preset["threshold_alerts"])


def test_supply_pricing_pack_rules_sourced():
    manifest = load_template_manifest(TEMPLATES / "supply-chain")
    pack_path = TEMPLATES / "supply-chain" / manifest["assets"]["pricing_rules_pack"]["path"]
    import json
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    rules = pack["pricing_rules"]["rules"]
    assert rules and all(r.get("rate_source") for r in rules)
    assert {r["layer"] for r in rules} <= {"customer_price", "promo", "coupon"}


def test_supply_pagesets_pack_four_ends():
    manifest = load_template_manifest(TEMPLATES / "supply-chain")
    pack_path = TEMPLATES / "supply-chain" / manifest["assets"]["pagesets_pack"]["path"]
    import json
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    ends = {p["target_end"] for p in pack["pagesets"]}
    assert ends == {"ue", "se", "sp", "cp"}


def test_supply_fleet_blueprint_boundary_audit():
    """蓝图格式 + 边界审计：回写白名单 / 确认门只在拍板席位。"""
    report = audit_template(
        REPO / "fd_erp_vertical_templates",
        asset_dirs=[TEMPLATES / "supply-chain"])
    assert report["ok"] is True, report["violations"]
    assert report["violations"] == []
    manifest = load_template_manifest(TEMPLATES / "supply-chain")
    import json
    blueprint = json.loads((TEMPLATES / "supply-chain"
                            / manifest["assets"]["fleet_blueprint"]["path"]
                            ).read_text(encoding="utf-8"))
    titles = [m["title"] for m in blueprint["fleet"]["members"]]
    assert any("拍板" in t for t in titles)  # 拍板席位存在
    cadences = [m["cadence"]["cron"] for m in blueprint["fleet"]["members"]]
    assert len(set(cadences)) == 3  # 错峰 cadence
    ledger_tools = blueprint["mcpServers"][0]["allowTools"]
    assert "confirm_proposal" in ledger_tools  # 拍板岗确认门
    assert "submit_proposal" in ledger_tools


def test_supply_template_customer_neutral():
    """模板资产客户中性：无具体客户名（锚点实例仅以中性词出现）。"""
    forbidden = ["万兴", "wanxing", "锚点客户A", "客户①"]
    for path in (TEMPLATES / "supply-chain").rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{path.name} 含非中性词：{word}"


def test_optional_pack_kinds_rejected_when_invalid(tmp_path):
    """扩展 pack 声明时须过面校验：无出处取价规则拒绝上架。"""
    import json
    import shutil
    bad_dir = tmp_path / "bad-template"
    shutil.copytree(TEMPLATES / "supply-chain", bad_dir)
    pack_path = bad_dir / "pricing-rules" / "pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["pricing_rules"]["rules"][0]["rate_source"] = ""
    pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TemplateError) as ei:
        load_template_manifest(bad_dir)
    assert "rate_source" in ei.value.message


def test_mcp_kernel_client_call_is_loop_safe():
    """回归：MCP 内核客户端必须能在**已运行的事件循环内**被同步调用。

    产上首现（2026-11-08）：REST 面是 FastAPI async 端点，`_call` 里裸调
    `asyncio.run()` 抛 `RuntimeError: asyncio.run() cannot be called from a
    running event loop`，凡碰内核的端点（对账单/下单信用/对账）全 500。
    这里用假 fastmcp Client 复现两种上下文：无循环（脚本/测试）与有循环
    （async 端点），断言两条路径都拿得到业务结果、且不串循环。
    """
    import asyncio
    import sys
    import types

    from fd_erp_vertical_templates.kernel_client import McpKernelClient

    class _FakeResult:
        def __init__(self, data):
            self.data = data

    class _FakeClient:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, tool, args):
            return _FakeResult({"tool": tool, "tenant": args.get("tenant_id")})

    fake = types.ModuleType("fastmcp")
    fake.Client = _FakeClient
    sys.modules["fastmcp"] = fake
    try:
        client = McpKernelClient("http://kernel.test/mcp", "acme")
        # ① 同步上下文
        assert client.list_proposals()["tool"] == "list_proposals"
        # ② 已在运行的事件循环内（async 端点形态）
        async def inside_loop():
            return client.list_proposals()

        got = asyncio.run(inside_loop())
        assert got == {"tool": "list_proposals", "tenant": "acme"}
    finally:
        sys.modules.pop("fastmcp", None)

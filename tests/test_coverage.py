"""功能点覆盖矩阵（optimize-erp-solution tasks 1.1-1.7，design D1/D2/D3）。

这些用例守的是矩阵的六条硬性质，而不是「函数能跑」：

1. 客户中性 —— 条目里出现客户名即拒绝并点名（矩阵只存脱敏编号）；
2. 证据为准 —— 解析到证据才落「直接命中/模式复用」，解析不到落「新建」，
   手写标注会被打红（D2：标注是自证，不连代码就会腐化）；
3. 计数可核对 —— 证据解析计数与实测一致（端点/健康探针/MCP 工具/模板资产）；
4. 重算幂等 —— 同一输入两次输出逐字节一致（不是「差不多」）；
5. 未归类阻断 —— 有未归类点即不能定稿、不能导出；
6. 声明由矩阵派生 —— supply-chain 四端域级计数复算为 7/3/4/4，入库矩阵
   与现算逐字节相等；导出带价格/工期数字必须有仓内可核验出处。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from fd_erp_vertical_templates import coverage as cov
from fd_erp_vertical_templates.errors import TemplateError

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates"
SUPPLY = TEMPLATES / "supply-chain"
EVIDENCE = REPO / "coverage" / "evidence.json"
#: 脱敏编号形态（与 coverage._ID_RE 同口径，测试不依赖私有名）。
ID_RE = re.compile(r"^FP-[0-9A-F]{10}$")
MATRIX = REPO / "coverage" / "matrices" / "supply-chain.json"
LAYERS = REPO / "coverage" / "layers"
#: 兄弟仓（CI 单独检出本仓时不存在：相关用例 skip，不是把门删掉）。
API_PKG = REPO.parent / "fd-erp-vertical-api" / "fd_erp_vertical_api"
TOOL_SURFACE = REPO.parent / "fd-erp-ledger-mcp" / "tool-surface.json"


def _point(point_id: str = "FP-0000000001", **kwargs) -> cov.MatrixPoint:
    base = dict(id=point_id, domain="示例域", subdomain="示例子域",
                classification=cov.CLASS_NEW,
                evidence=(cov.EvidenceRef(cov.EVIDENCE_ENDPOINT, "GET /nope"),))
    base.update(kwargs)
    return cov.MatrixPoint(**base)


# --------------------------------------------------------------------------
# 证据解析器（task 1.2）：四类证据源可枚举，计数与实测一致
# --------------------------------------------------------------------------


def test_mcp_tool_surface_enumerated():
    """MCP 工具面可枚举；20 是任务书基线，实测不得少于基线。

    基线之后内核侧还在加工具（本仓 2026-11-10 实测 21 个：日终快照计费
    `bill_period_snapshot` 由并行的计费 change 加入），所以这里断言「不少于
    基线 + 快照与实测一致」，而不是把 20 写死成永远的真。
    """
    snapshot = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    tools = snapshot["mcp_tools"]
    assert len(tools) == snapshot["counts"]["mcp_tools"]
    assert len(tools) >= 20, f"MCP 工具实测 {len(tools)} 个少于任务基线 20 个"
    assert "submit_proposal" in tools
    if TOOL_SURFACE.is_file():  # 兄弟仓在位时，解析器实测必须与快照一致
        assert cov.parse_mcp_tools(TOOL_SURFACE) == tools


def test_evidence_snapshot_health_probes_and_route_count():
    """入库快照：63 是任务书基线（含 2 条健康探针），实测不得少于基线。"""
    snapshot = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    counts = snapshot["counts"]
    assert counts["health_probes"] == ["/health", "/healthz"]  # 2 条健康探针
    assert counts["routes"] == len(snapshot["routes"])
    assert counts["routes"] >= 63, (
        f"端点实测 {counts['routes']} 条少于任务基线 63 条（含 2 条健康探针）")
    placeholders = sum(1 for r in snapshot["routes"] if r["placeholder"])
    assert counts["placeholder_routes"] == placeholders
    assert placeholders > 0, "501 占位端点计数为 0 与现状不符（开票/售后等仍是占位）"


@pytest.mark.skipif(not API_PKG.is_dir(), reason=f"兄弟仓不在位（{API_PKG}）")
def test_snapshot_matches_live_measurement():
    """快照 == 现测（端点集合与占位标记）。行号不计（会随别处改动漂移）。"""
    snapshot = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    live = cov.build_evidence_snapshot()
    snap_set = {(r["method"], r["path"], bool(r["placeholder"]))
                for r in snapshot["routes"]}
    live_set = {(r["method"], r["path"], bool(r["placeholder"]))
                for r in live["routes"]}
    missing = sorted(live_set - snap_set)
    stale = sorted(snap_set - live_set)
    assert live_set == snap_set, (
        "覆盖证据快照与实测不一致，先跑 `coverage evidence` 重算：\n"
        f"实测有而快照无：{missing}\n快照有而实测无：{stale}")
    assert live["counts"]["mcp_tools"] == snapshot["counts"]["mcp_tools"]
    assert live["counts"]["mcp_tools"] >= 20
    # 健康探针恰好 2 条（集合比对：与声明顺序无关）
    probes = sorted({p for _, p, _ in live_set if p in ("/health", "/healthz")})
    assert probes == ["/health", "/healthz"]


def test_three_templates_assets_enumerated():
    """三模板四件套资产齐 + supply-chain 扩展件与四端页面集齐。"""
    assets = cov.parse_template_assets(TEMPLATES)
    assert set(assets) == {"crossborder", "restaurant-chain", "supply-chain"}
    required = ("connector_pack", "fleet_blueprint",
                "account_mapping_pack", "report_preset")
    for name, info in assets.items():
        for kind in required:
            assert kind in info["assets"], f"{name} 缺四件套：{kind}"
            assert (TEMPLATES / name / info["assets"][kind]).is_file(), \
                f"{name}/{kind} 资产不存在：{info['assets'][kind]}"
    supply = assets["supply-chain"]
    assert {"pricing_rules_pack", "pagesets_pack"} <= set(supply["assets"])
    assert set(supply["pagesets"]) == {"ue", "se", "sp", "cp"}
    for surface, pages in supply["pagesets"].items():
        assert pages, f"supply-chain/{surface} 页面集没有页面"


def test_test_names_enumerated():
    names = cov.parse_test_names(REPO / "tests")
    assert "test_three_templates_validate" in names
    assert all(n.startswith("test_") for n in names)
    assert len(names) > 50, f"测试名枚举数量异常：{len(names)}"


def test_contract_endpoints_all_resolve_in_evidence():
    """契约里每个端点都要能在证据里解析到，否则归类会被静默降级成「新建」。"""
    index = cov.load_evidence(EVIDENCE)
    checked = 0
    for ps_dir in sorted(p for p in (SUPPLY / "pagesets").iterdir() if p.is_dir()):
        contract = cov.parse_pageset_contract(ps_dir / "api-contract.md")
        for ref in contract.endpoints():
            resolution = index.resolve(cov.EvidenceRef(cov.EVIDENCE_ENDPOINT, ref))
            assert resolution.resolved, f"{ps_dir.name}: 契约端点解析不到：{ref}"
            checked += 1
    assert checked > 40, f"契约端点枚举数量异常：{checked}"


# --------------------------------------------------------------------------
# 归类（task 1.3）：解析到证据才落「直接命中/模式复用」，解析不到落「新建」
# --------------------------------------------------------------------------


def test_classification_is_evidence_derived():
    index = cov.load_evidence(EVIDENCE)

    def classify(kind: str, ref: str) -> str:
        return cov.classify_evidence([cov.EvidenceRef(kind, ref)], index)

    # 实现就绪的端点 / 在工具面上的 MCP 工具 → 直接命中
    assert classify("endpoint", "GET /erp/supply-chain/v1/ue/products") == cov.CLASS_DIRECT
    assert classify("mcp_tool", "submit_proposal") == cov.CLASS_DIRECT
    # 501 占位端点 / 只有模板资产或测试名 → 模式复用
    # 例子里必须用**此刻仍是** 501 的端点：拿已实现的端点当占位范例，会在能力落地
    # 那天变成「实现让测试变红」（UE 开票两条就是这么变红的，已换成 SP 发票面）。
    assert classify("endpoint", "POST /erp/supply-chain/v1/sp/invoices") == cov.CLASS_PATTERN
    assert classify("template_asset",
                    "templates/supply-chain/pagesets/ue-commerce/manifest.json#page/ue-home") \
        == cov.CLASS_PATTERN
    assert classify("test", "test_three_templates_validate") == cov.CLASS_PATTERN
    # 一条都解析不到 → 新建
    assert classify("endpoint", "GET /erp/supply-chain/v1/ue/does-not-exist") == cov.CLASS_NEW
    assert classify("mcp_tool", "no_such_tool") == cov.CLASS_NEW
    assert cov.classify_evidence([], index) == cov.CLASS_NEW


def test_claimed_classification_must_match_evidence():
    """手写「直接命中」但证据只解析到 501 占位 → 打红（归类不靠人工标注）。"""
    index = cov.load_evidence(EVIDENCE)
    point = _point(domain="开票申请", subdomain="ue-invoice-apply", surface="ue",
                   classification=cov.CLASS_DIRECT,
                   evidence=(cov.EvidenceRef(
                       cov.EVIDENCE_ENDPOINT,
                       "POST /erp/supply-chain/v1/sp/invoices"),))
    with pytest.raises(TemplateError) as ei:
        cov.validate_matrix(cov.CoverageMatrix(vertical="supply-chain", points=[point]),
                            evidence=index, customer_markers=())
    assert "归类与证据不符" in ei.value.message
    assert "模式复用" in ei.value.message


def test_not_doing_requires_reason():
    point = _point(domain="退货", subdomain="退货申请",
                   classification=cov.CLASS_NOT_DOING)
    with pytest.raises(TemplateError) as ei:
        cov.validate_matrix(cov.CoverageMatrix(vertical="demo", points=[point]),
                            customer_markers=())
    assert "不做" in ei.value.message
    assert "not_doing_reason" in ei.value.message
    # 带理由放行
    ok = replace(point, not_doing_reason="一期不做：上游接口 schema 缺失")
    cov.validate_matrix(cov.CoverageMatrix(vertical="demo", points=[ok]),
                        customer_markers=())


def test_not_doing_domain_counts_as_pending_trim():
    point = _point(domain="退货", subdomain="退货申请", surface="ue", anchor=True,
                   classification=cov.CLASS_NOT_DOING,
                   not_doing_reason="一期不做：上游接口 schema 缺失")
    counts = cov.coverage_counts(cov.CoverageMatrix(vertical="demo", points=[point]))
    assert counts["surfaces"]["ue"]["pendingTrimAreas"] == 1
    assert counts["surfaces"]["ue"]["coveredAreas"] == 0
    assert counts["layer_counts"][cov.CLASS_NOT_DOING] == 1


# --------------------------------------------------------------------------
# 客户中性（task 1.1 / D1）：出现客户名即拒绝并点名
# --------------------------------------------------------------------------


def test_matrix_rejects_customer_name_and_names_the_point():
    point = _point("FP-00000000AA", domain="全日供应链采购对账", subdomain="采购单")
    with pytest.raises(TemplateError) as ei:
        cov.validate_matrix(cov.CoverageMatrix(vertical="demo", points=[point]),
                            customer_markers=("全日供应链",))
    assert "FP-00000000AA" in ei.value.message  # 点名
    assert "全日供应链" in ei.value.message
    assert ei.value.detail["violations"]


def test_matrix_rejects_company_legal_suffix_without_denylist():
    """通用规则：公司全称后缀，不依赖名单也能拦住客户名。"""
    point = _point(domain="华东测试有限公司采购", subdomain="采购单")
    with pytest.raises(TemplateError) as ei:
        cov.validate_matrix(cov.CoverageMatrix(vertical="demo", points=[point]),
                            customer_markers=())
    assert "有限公司" in ei.value.message


def test_customer_markers_file_loads():
    markers = cov.load_customer_markers()
    assert markers, "缺 coverage/customer-markers.json（客户名 denylist）"


# --------------------------------------------------------------------------
# 受控导入 + 脱敏（task 1.4）
# --------------------------------------------------------------------------

_EXTERNAL = {
    "points": [
        {"text": "半月度采购对账单核对（客户内部口径）",
         "domain": "采购对账", "subdomain": "对账单核对",
         "evidence": [{"type": "endpoint",
                       "ref": "GET /erp/supply-chain/v1/sp/statements"}]},
        {"text": "门店要货申请与审批流转",
         "domain": "要货申请", "subdomain": "要货单",
         "evidence": []},
    ]
}


def _write_external(tmp_path: Path, body: dict) -> Path:
    external = tmp_path / "external" / "client-list.json"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return external


def test_import_masks_customer_text_and_keeps_mapping_external(tmp_path):
    external = _write_external(tmp_path, _EXTERNAL)
    out_matrix = tmp_path / "out" / "matrix.json"
    out_mapping = tmp_path / "out" / "mapping.json"
    result = cov.import_external_list(external, out_matrix=out_matrix,
                                      out_mapping=out_mapping, salt="test-salt",
                                      repo_root=REPO)
    assert result["points"] == 2

    matrix_text = out_matrix.read_text(encoding="utf-8")
    for entry in _EXTERNAL["points"]:
        assert entry["text"] not in matrix_text, "矩阵里出现了清单原文（脱敏失败）"
    body = json.loads(matrix_text)
    ids = [p["id"] for p in body["points"]]
    assert all(ID_RE.match(i) for i in ids), ids
    assert body["finalized"] is True
    assert {p["domain"] for p in body["points"]} == {"采购对账", "要货申请"}

    # 原文只落仓外映射
    mapping = json.loads(out_mapping.read_text(encoding="utf-8"))
    assert mapping["salt"] == "test-salt"
    assert {v["text"] for v in mapping["entries"].values()} == \
        {e["text"] for e in _EXTERNAL["points"]}

    # 仓库内任何地方都不该出现原文
    for path in list((REPO / "coverage").rglob("*.json")) + \
            list(SUPPLY.rglob("*.md")) + list(SUPPLY.rglob("*.json")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for entry in _EXTERNAL["points"]:
            assert entry["text"] not in text, f"{path} 出现客户清单原文"


def test_import_is_byte_identical_on_reimport(tmp_path):
    external = _write_external(tmp_path, _EXTERNAL)
    out_matrix = tmp_path / "out" / "matrix.json"
    out_mapping = tmp_path / "out" / "mapping.json"
    first = cov.import_external_list(external, out_matrix=out_matrix,
                                     out_mapping=out_mapping, salt="stable",
                                     repo_root=REPO)
    bytes_first = out_matrix.read_bytes()
    second = cov.import_external_list(external, out_matrix=out_matrix,
                                      out_mapping=out_mapping, repo_root=REPO)
    assert first["fingerprint"] == second["fingerprint"]
    assert out_matrix.read_bytes() == bytes_first


def test_import_refuses_source_inside_repo():
    with pytest.raises(TemplateError) as ei:
        cov.import_external_list(REPO / "README.md",
                                 out_matrix=Path("/tmp/fd-cov-matrix.json"),
                                 out_mapping=Path("/tmp/fd-cov-mapping.json"),
                                 repo_root=REPO)
    assert "不得位于仓内" in ei.value.message


def test_import_refuses_mapping_inside_repo(tmp_path):
    external = _write_external(tmp_path, _EXTERNAL)
    with pytest.raises(TemplateError) as ei:
        cov.import_external_list(external,
                                 out_matrix=tmp_path / "matrix.json",
                                 out_mapping=REPO / "coverage" / "leak.json",
                                 repo_root=REPO)
    assert "必须落仓外" in ei.value.message


def test_import_rejects_customer_name_point_and_names_it(tmp_path):
    text = "全日供应链门店要货审批"
    body = {"points": [{"text": text, "domain": "全日供应链要货", "subdomain": "要货单"}]}
    external = _write_external(tmp_path, body)
    out_matrix = tmp_path / "out" / "matrix.json"
    expected_id = "FP-" + hashlib.sha256(
        f"salt-1\x1f{text}".encode("utf-8")).hexdigest()[:10].upper()
    with pytest.raises(TemplateError) as ei:
        cov.import_external_list(external, out_matrix=out_matrix,
                                 out_mapping=tmp_path / "out" / "mapping.json",
                                 salt="salt-1", repo_root=REPO)
    assert expected_id in ei.value.message, ei.value.message
    assert "全日供应链" in ei.value.message
    assert not out_matrix.exists(), "校验失败时不得写出矩阵"


# --------------------------------------------------------------------------
# 重算幂等（task 1.5）
# --------------------------------------------------------------------------


def test_recompute_is_byte_identical():
    index = cov.load_evidence(EVIDENCE)
    first = cov.build_template_matrix(SUPPLY, index)
    second = cov.build_template_matrix(SUPPLY, index)
    dump_first = cov.matrix_dumps(first).encode("utf-8")
    dump_second = cov.matrix_dumps(second).encode("utf-8")
    assert dump_first == dump_second  # 字节相等，不是「差不多」
    assert hashlib.sha256(dump_first).hexdigest() == \
        hashlib.sha256(dump_second).hexdigest()
    assert cov.matrix_fingerprint(first) == cov.matrix_fingerprint(second)


def test_matrix_has_no_timestamp_fields():
    body = json.loads(MATRIX.read_text(encoding="utf-8"))
    blob = json.dumps(body, ensure_ascii=False)

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    for key in keys(body):
        low = key.lower()
        assert "time" not in low and "date" not in low and "updated" not in low, \
            f"矩阵含时间字段 {key}（重算幂等会被打破）"
    assert "2026-" not in blob, "矩阵含日期串（重算幂等会被打破）"


def test_committed_matrix_equals_fresh_derivation():
    index = cov.load_evidence(EVIDENCE)
    fresh = cov.build_template_matrix(SUPPLY, index)
    assert cov.matrix_dumps(fresh).encode("utf-8") == MATRIX.read_bytes(), (
        "入库矩阵与现算不一致：跑 `coverage build --template templates/supply-chain` 重算")


def test_committed_matrix_passes_validation():
    matrix = cov.load_matrix(MATRIX)
    assert matrix.finalized is True
    cov.validate_matrix(matrix, evidence=cov.load_evidence(EVIDENCE))
    assert cov.unclassified_ids(matrix) == []


# --------------------------------------------------------------------------
# 未归类阻断（task 1.5）：不能定稿、不能导出
# --------------------------------------------------------------------------


def test_unclassified_blocks_finalize_and_export(tmp_path):
    pending = _point("FP-0000000007", domain="退货", subdomain="退货申请",
                     classification="")  # 未归类
    matrix = cov.CoverageMatrix(vertical="demo", points=[pending])
    assert cov.unclassified_ids(matrix) == ["FP-0000000007"]

    with pytest.raises(TemplateError) as ei:
        cov.finalize_matrix(matrix)
    assert "FP-0000000007" in ei.value.message
    assert matrix.finalized is False

    with pytest.raises(TemplateError) as ei:
        cov.export_layers(matrix, tmp_path / "layers", repo_root=REPO,
                          customer_markers=())
    assert "FP-0000000007" in ei.value.message
    assert ei.value.detail["unclassified"] == ["FP-0000000007"]


def test_finalized_flag_with_unclassified_point_rejected():
    matrix = cov.CoverageMatrix(vertical="demo",
                                points=[_point("FP-0000000008", classification="")],
                                finalized=True)
    with pytest.raises(TemplateError) as ei:
        cov.validate_matrix(matrix, customer_markers=())
    assert "定稿" in ei.value.message


def test_export_requires_finalized_matrix(tmp_path):
    matrix = cov.CoverageMatrix(vertical="demo", points=[_point()], finalized=False)
    with pytest.raises(TemplateError) as ei:
        cov.export_layers(matrix, tmp_path / "layers", repo_root=REPO,
                          customer_markers=())
    assert "未定稿" in ei.value.message


# --------------------------------------------------------------------------
# 分层导出（task 1.7）+ 无出处数字拒绝
# --------------------------------------------------------------------------


def test_export_four_layers_and_is_byte_identical(tmp_path):
    matrix = cov.load_matrix(MATRIX)
    out_dir = tmp_path / "layers"
    index_first = cov.export_layers(matrix, out_dir, repo_root=REPO)
    assert {entry["layer"] for entry in index_first["layers"]} == set(cov.CLASSIFICATIONS)
    counts = cov.coverage_counts(matrix)["layer_counts"]
    assert index_first["totals"] == counts
    for entry in index_first["layers"]:
        path = out_dir / entry["file"]
        assert path.is_file(), entry["file"]
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["count"] == entry["count"] == len(body["points"])
        for record in body["points"]:
            # 导出只含功能点编号与证据（不含客户可识别信息）
            assert set(record) == {"id", "evidence"}
            assert ID_RE.match(record["id"])

    bytes_first = {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.json"))}
    cov.export_layers(matrix, out_dir, repo_root=REPO)
    bytes_second = {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.json"))}
    assert bytes_first == bytes_second


def test_export_rejects_number_without_source(tmp_path):
    matrix = cov.load_matrix(MATRIX)
    claims = [{"field": "一期报价", "value": 128000, "source": ""},
              {"field": "交付工期", "value": 45}]
    with pytest.raises(TemplateError) as ei:
        cov.export_layers(matrix, tmp_path / "layers", repo_root=tmp_path,
                          commercial_claims=claims)
    assert "一期报价" in ei.value.message
    assert "交付工期" in ei.value.message
    assert len(ei.value.detail["missing_source_fields"]) == 2


def test_export_rejects_number_not_found_in_source(tmp_path):
    source = tmp_path / "docs" / "quote.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 报价\n\n一期实施 30 人日。\n", encoding="utf-8")
    claims = [{"field": "一期报价", "value": 128000, "source": "docs/quote.md"}]
    with pytest.raises(TemplateError) as ei:
        cov.validate_numeric_claims(claims, repo_root=tmp_path)
    assert "出处不可核验" in ei.value.message


def test_export_accepts_number_with_verifiable_source(tmp_path):
    source = tmp_path / "docs" / "quote.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 报价\n\n一期实施 128,000 元，工期 45 天。\n", encoding="utf-8")
    claims = [{"field": "一期报价", "value": 128000, "source": "docs/quote.md"},
              {"field": "交付工期", "value": 45, "source": "docs/quote.md"}]
    cov.validate_numeric_claims(claims, repo_root=tmp_path)
    matrix = cov.load_matrix(MATRIX)
    index = cov.export_layers(matrix, tmp_path / "layers", repo_root=tmp_path,
                              commercial_claims=claims)
    assert index["commercial_claims"] == claims


def test_export_rejects_source_outside_repo(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("128000 元", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(TemplateError) as ei:
        cov.validate_numeric_claims(
            [{"field": "一期报价", "value": 128000, "source": "../outside.md"}],
            repo_root=repo)
    assert "不在仓内" in ei.value.message


# --------------------------------------------------------------------------
# 模板覆盖声明由矩阵派生（task 1.6 / D3）
# --------------------------------------------------------------------------


def test_supply_chain_domain_counts_are_7_3_4_4():
    index = cov.load_evidence(EVIDENCE)
    matrix = cov.build_template_matrix(SUPPLY, index)
    counts = cov.coverage_counts(matrix)
    got = {surface: counts["surfaces"][surface]["coveredAreas"]
           for surface in ("ue", "se", "sp", "cp")}
    assert got == {"ue": 7, "se": 3, "sp": 4, "cp": 4}, got
    assert counts["totals"]["pendingTrimAreas"] == 0
    assert counts["totals"]["unclassified"] == 0
    assert matrix.finalized is True
    # 非锚点域（spec 附加 / 别的 tasks 关联）不计入蓝图覆盖计数
    non_anchor = {(p.surface, p.domain) for p in matrix.points if not p.anchor}
    assert non_anchor == {("ue", "AI 助手入口"), ("se", "AI 助手入口"),
                          ("sp", "AI 助手入口"), ("cp", "AI 助手入口"),
                          ("cp", "对账"), ("se", "开票审核")}


def test_declaration_check_passes_for_supply_chain_template():
    assert cov.check_template_coverage_declaration(SUPPLY) == []


def test_declaration_check_names_unbackfilled_domain(tmp_path):
    """页面集新增功能域但没在对账表回填 → 校验点名该域。"""
    tpl = tmp_path / "supply-chain"
    shutil.copytree(SUPPLY, tpl)
    contract = tpl / "pagesets" / "ue-commerce" / "api-contract.md"
    text = contract.read_text(encoding="utf-8")
    text += ("\n## 9. 退货域（功能点：退货申请）\n\n"
             "| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |\n"
             "|---|---|---|---|---|\n"
             "| POST | `/erp/supply-chain/v1/ue/return-requests` | 行引用 | 申请单号 | `order.write` |\n")
    contract.write_text(text, encoding="utf-8")
    violations = cov.check_template_coverage_declaration(tpl)
    assert any("退货申请" in v and "未在 api-contract.md" in v for v in violations), violations
    assert any("return-requests" in v for v in violations), violations


def test_template_without_pagesets_is_not_checked():
    """crossborder / restaurant-chain 未声明页面集 → 覆盖钩子原样放行。"""
    assert cov.check_template_coverage_declaration(TEMPLATES / "crossborder") == []
    assert cov.check_template_coverage_declaration(TEMPLATES / "restaurant-chain") == []


def test_build_refuses_to_overwrite_nonempty_matrix_with_empty_result(tmp_path):
    """`coverage build --template` 写错路径时不得用空矩阵覆盖真矩阵。

    2026-11-12 实测：把 `templates/supply-chain` 写成 `supply-chain`，派生出 0 个点，
    `save_matrix` 照写不误，入库那份 35 点的矩阵被换成一个空壳。空结果覆盖真结果
    是最贵的一类失败——文件还在、格式还对、内容是「什么都没读到」。
    """
    import subprocess
    import sys as _sys

    REPO = Path(__file__).resolve().parent.parent

    # 目标文件用手工造的 `--out`，不碰入库矩阵
    out = tmp_path / "supply-chain.json"
    matrix = cov.load_matrix(MATRIX)
    cov.save_matrix(matrix, out)
    before = out.read_bytes()
    assert matrix.points, "前提：入库矩阵非空"

    proc = subprocess.run(
        [_sys.executable, "-m", "fd_erp_vertical_templates.cli", "coverage",
         "build", "--template", "supply-chain", "--out", str(out)],
        capture_output=True, text=True, cwd=str(REPO))
    code = proc.returncode
    assert code == 2, f"空结果覆盖非空矩阵必须被拒（退出码 2），实得 {code}：{proc.stderr}"
    assert out.read_bytes() == before, "拒绝后文件必须一字未动"

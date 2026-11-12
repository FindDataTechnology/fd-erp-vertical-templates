"""对外能力声明与连接器缺口门（optimize-erp-solution tasks 3.6 / 3.8 / 4.1-4.6）。

和 test_foundry_gates.py 同一纪律：正例证明现状绿，负例证明篡改必红且点名
（端点、声明、缺口、块名）。负例在 tmp 副本/构造数据上做，真仓文件只读。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from fd_erp_vertical_templates.gaps import REQUIRED_GAPS, check as gaps_check
from fd_erp_vertical_templates.gaps import load_gaps

REPO = Path(__file__).resolve().parent.parent
FINDDATA = REPO.parent
BOARD_DOC = FINDDATA / "docs" / "erp" / "README.md"
#: 机制用例用的板块文档快照：CI 只有本仓 checkout，读不到 finddata 根的 docs/。
#: 漂移探测由「生产材料」那条用例负责（需要真文档，缺则 skip）。
BOARD_DOC_FIXTURE = REPO / "tests" / "fixtures" / "board-doc-erp-README.md"
API_PLANNED = FINDDATA / "fd-erp-vertical-api" / "planned-surfaces.json"
API_PACKAGE = FINDDATA / "fd-erp-vertical-api" / "fd_erp_vertical_api"
TOOL_SURFACE = FINDDATA / "fd-erp-ledger-mcp" / "tool-surface.json"
CONNECTOR_CATALOG = FINDDATA / "open-connector-mt" / "catalog" / "apps"
CONNECTOR_PROVIDERS = FINDDATA / "open-connector-mt" / "src" / "providers"


def _require(*paths: Path) -> None:
    """跨仓证据源不在位 → 如实 skip（本仓单独检出时 CI 就是这样，不假绿）。"""
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        pytest.skip("跨仓证据源不在位，无法核对：" + "、".join(missing))


def _load_script(name: str):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def payment_gate():
    return _load_script("payment_direct_call_gate")


@pytest.fixture(scope="module")
def claim_gate():
    return _load_script("claim_gate")


# ---------------------------------------------------------------- 3.6 支付直连


def test_payment_gate_passes_on_current_package(payment_gate):
    """3.6 正例：现状零直连调用（generic HTTP 结算适配已撤）。"""
    assert payment_gate.scan() == []
    assert payment_gate.main([]) == 0


def test_payment_gate_red_on_new_direct_payment_call(tmp_path, payment_gate):
    """3.6 负例：新增一条直连支付 HTTP 调用 → 红，并指名文件/行/载体。"""
    pkg = tmp_path / "pkg"
    (pkg / "connectors").mkdir(parents=True)
    (pkg / "connectors" / "payments.py").write_text(
        '"""伪造的直连适配（负向样本）。"""\n'
        "import httpx\n\n"
        "def pull(store_id):\n"
        '    return httpx.post("https://api.mch.weixin.qq.com/v3/bill/tradebill",\n'
        '                      json={"store": store_id}).json()\n',
        encoding="utf-8")
    violations = payment_gate.scan(pkg)
    codes = {v["code"] for v in violations}
    assert {"PAYMENT_DIRECT_HTTP_IMPORT", "PAYMENT_DIRECT_CALL",
            "PAYMENT_CHANNEL_HOST"} <= codes
    assert all(v["path"] == "connectors/payments.py" for v in violations)
    assert all(v["line"] > 0 for v in violations)
    assert any("api.mch.weixin.qq.com" in v["snippet"] for v in violations)
    assert payment_gate.main(["--root", str(pkg)]) == 1


def test_payment_gate_allowlist_only_runtime_client(tmp_path, payment_gate):
    """出网单口就是 runtime_client.py：同一写法换个文件名即红。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "runtime_client.py").write_text(
        "import httpx\n\ndef send():\n    return httpx.post('http://x')\n",
        encoding="utf-8")
    assert payment_gate.scan(pkg) == []      # 唯一允许面
    (pkg / "other.py").write_text("import requests\n", encoding="utf-8")
    assert {v["code"] for v in payment_gate.scan(pkg)} == {"PAYMENT_DIRECT_HTTP_IMPORT"}


# ---------------------------------------------------------------- 3.8 缺口登记


def test_gap_register_has_four_required_items():
    """3.8 四项缺口齐全（单一出处 gaps.json）。

    `== 4` 曾写成活跃上限，但缺口台账会随新事实源长（2026-11-12 合并营销线时
    加了 Chatwoot 与渠道订单两条）。上限存在的意义是别把必填项删掉，而这件事
    由下面的 `set(REQUIRED_GAPS) <= ids` 判；再钉一个总数只会让「如实登记一条
    新缺口」变成改测试，最后没人敢登记。
    """
    body = load_gaps()
    ids = {g["id"] for g in body["gaps"]}
    assert set(REQUIRED_GAPS) <= ids
    assert len(ids) == len(body["gaps"]), "缺口 id 重复"
    for gap in body["gaps"]:
        assert gap["detect"] and gap["responsible_repo"] and gap["blocking"]
        assert gap["status"] in ("open", "resolved")


def test_gap_register_matches_cross_repo_measurement():
    """3.8 正例：与跨仓实测一致（银联/WMS/金蝶 open，支付接线 resolved）。"""
    _require(CONNECTOR_CATALOG, CONNECTOR_PROVIDERS)
    result = gaps_check()
    assert result["ok"] is True, result["violations"]
    verdicts = {g["id"]: g["verdict"] for g in result["gaps"]}
    assert verdicts["unionpay-no-provider"] == "open"
    assert verdicts["wms-no-provider"] == "open"
    assert verdicts["payments-not-wired"] == "resolved"
    assert verdicts["kingdee-no-executor-test"] == "open"


def test_gap_backfill_required_when_cross_repo_already_fixed(tmp_path):
    """3.8 负例：连接器仓已新增 unionpay provider 而本仓仍写 open → 红。"""
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "unionpay.json").write_text("{}", encoding="utf-8")
    result = gaps_check(catalog_dir=catalog)
    assert result["ok"] is False
    codes = {v["code"] for v in result["violations"]}
    assert "GAP_BACKFILL_REQUIRED" in codes
    item = next(v for v in result["violations"] if v["gap"] == "unionpay-no-provider")
    assert "unionpay" in item["message"] and "回填" in item["message"]

    # 回填后转绿：同一条改为 resolved
    body = load_gaps()
    for gap in body["gaps"]:
        if gap["id"] == "unionpay-no-provider":
            gap["status"] = "resolved"
    assert gaps_check(gaps=body, catalog_dir=catalog)["ok"] is True


def test_gap_stale_resolution_is_red(tmp_path):
    """3.8 负例：登记 resolved 而实测仍未解除 → 红（虚报修好）。"""
    body = load_gaps()
    for gap in body["gaps"]:
        if gap["id"] == "unionpay-no-provider":
            gap["status"] = "resolved"
    empty_catalog = tmp_path / "empty"
    empty_catalog.mkdir()
    result = gaps_check(gaps=body, catalog_dir=empty_catalog)
    assert result["ok"] is False
    assert {v["code"] for v in result["violations"]} == {"GAP_STALE_RESOLUTION"}


def test_gap_missing_required_item_is_rejected():
    """3.8 负例：删一项必填缺口 → 结构校验直接拒绝。"""
    body = load_gaps()
    body["gaps"] = [g for g in body["gaps"] if g["id"] != "kingdee-no-executor-test"]
    from fd_erp_vertical_templates.errors import TemplateError
    with pytest.raises(TemplateError) as ei:
        gaps_check(gaps=body)
    assert "kingdee-no-executor-test" in ei.value.message


# ---------------------------------------------------------------- 3.8 缺口进能力清单


def test_open_gaps_appear_in_capability_list(claim_gate):
    """3.8/4.4 缺口在对外能力清单里可见（银联/WMS/金蝶标注不可用）。"""
    claims = claim_gate.load_claims()
    mirror = claim_gate.load_mirror()
    from fd_erp_vertical_templates.publish import load_record
    text = claim_gate.render_capability_list(claims, mirror, load_record(),
                                             gaps_check())
    assert "当前不可用" in text
    for capability in ("银联", "客户 WMS", "金蝶云星空"):
        assert capability in text
    # 已解除的支付接线不该出现在「不可用」段
    unavailable = text.split("## 三、当前不可用", 1)[1]
    assert "支付渠道结算读取" not in unavailable


# ---------------------------------------------------------------- 4.2 声明必须带证据


def _prepare_doc(tmp_path, claim_gate) -> Path:
    """复制板块文档并把生成块重写一遍（保证只有我要篡改的地方不同）。

    用入库夹具而不是 finddata 根的真文档：CI 只有本仓 checkout。这里验的是
    「生成块与生成器输出对拍」这个机制，与生产材料是否最新无关。
    """
    doc = tmp_path / "erp-README.md"
    shutil.copyfile(BOARD_DOC_FIXTURE, doc)
    claims = claim_gate.load_claims()
    mirror = claim_gate.load_mirror()
    from fd_erp_vertical_templates.publish import load_record
    claim_gate.render(claims, load_record(), doc=doc,
                      out_list=tmp_path / "list.md", mirror=mirror)
    return doc


def test_claim_gate_passes_on_current_materials(claim_gate):
    """4.2 正例：现有三处对外材料（文档 + 官网两页）零无证据声明。

    验的是**生产材料**，所以必须读真文档与兄弟仓；CI 单仓 checkout 下如实 skip
    （机制面由夹具用例覆盖，不因此放弃覆盖）。
    """
    _require(BOARD_DOC, API_PACKAGE, TOOL_SURFACE, FINDDATA / "fd-official-web")
    result = claim_gate.check()
    assert result["ok"] is True, result["violations"]
    # 不钉 20：挂账数会随能力落地变小（售后三条已实现）。要守的是「计数=镜像端点数」。
    assert result["counts"]["planned"] == claim_gate.load_mirror()["count"]


def test_unsupported_invoice_claim_is_red(tmp_path, claim_gate):
    """4.2 负例：写「支持开票」而端点仍 501 → 红，点名 claim 与其声称的能力。"""
    doc = _prepare_doc(tmp_path, claim_gate)
    doc.write_text(doc.read_text(encoding="utf-8")
                   + "\n支持开票，票据自动开具。\n", encoding="utf-8")

    result = claim_gate.check(board_doc=doc)
    assert result["ok"] is False
    hits = [v for v in result["violations"]
            if v["code"] in ("CLAIM_FORBIDDEN_ASSERTION", "CLAIM_UNDECLARED")
            and "开票" in v.get("statement", "")]
    assert hits, result["violations"]
    assert hits[0]["claim"] == "invoice.self-built"
    assert "开票" in hits[0]["statement"]
    assert str(doc) in hits[0]["path"]


def test_unregistered_capability_claim_is_red(tmp_path, claim_gate):
    """4.2 负例：声明一条没登记过的能力（「…已上线」）→ CLAIM_UNDECLARED。"""
    doc = _prepare_doc(tmp_path, claim_gate)
    doc.write_text(doc.read_text(encoding="utf-8")
                   + "\n订单自动对账已上线。\n", encoding="utf-8")

    result = claim_gate.check(board_doc=doc)
    assert result["ok"] is False
    undeclared = [v for v in result["violations"] if v["code"] == "CLAIM_UNDECLARED"]
    assert undeclared, result["violations"]
    assert undeclared[0]["assertion"] == "live"
    assert "订单自动对账已上线" in undeclared[0]["statement"]


def test_evidence_backed_statement_is_green(tmp_path, claim_gate):
    """4.2 正例：改成有证据的声明（对账四类差异）→ 绿。"""
    doc = _prepare_doc(tmp_path, claim_gate)
    text = doc.read_text(encoding="utf-8").replace(
        "支持开票，票据自动开具。",
        "账内可记录发票类事件并参与对账，票据本体由客户既有合规渠道处理。")
    doc.write_text(text, encoding="utf-8")

    result = claim_gate.check(board_doc=doc)
    assert result["ok"] is True, result["violations"]


def test_negated_admonition_is_not_a_claim(tmp_path, claim_gate):
    """否定语境不是声明：「不自研、当前不提供（挂账）」不判红。"""
    doc = _prepare_doc(tmp_path, claim_gate)
    doc.write_text(doc.read_text(encoding="utf-8")
                   + "\n开票自研：不自研、当前不提供（挂账）。\n", encoding="utf-8")
    assert claim_gate.check(board_doc=doc)["ok"] is True


# ---------------------------------------------------------------- 4.3 挂账清单


def test_planned_mirror_count_matches_source(claim_gate):
    """4.3 挂账清单与 fd-erp-vertical-api/planned-surfaces.json 逐条一致。

    条数不写死：能力落地会让清单变短（售后三条即如此），写死等于惩罚实现。
    """
    mirror = claim_gate.load_mirror()
    assert mirror["count"] == len(mirror["endpoints"]) > 0
    if API_PLANNED.is_file():
        source = json.loads(API_PLANNED.read_text(encoding="utf-8"))
        assert mirror["count"] == source["count"]
        assert mirror["by_capability"] == source["by_capability"]
        assert {(e["method"], e["path"]) for e in mirror["endpoints"]} == \
            {(e["method"], e["path"]) for e in source["endpoints"]}
        assert all(e["planned_lane"] for e in mirror["endpoints"])


def test_unregistered_501_endpoint_is_red(tmp_path, claim_gate):
    """4.3 负例：人为漏登记一个 501 端点 → 红并点名该端点；补登记转绿。"""
    mirror = claim_gate.load_mirror()
    dropped = mirror["endpoints"].pop()
    try:
        result = claim_gate.check(mirror_path=mirror)
        assert result["ok"] is False
        codes = {v["code"] for v in result["violations"]}
        assert "PLANNED_SURFACE_UNREGISTERED" in codes
        missing = [v for v in result["violations"]
                   if v["code"] == "PLANNED_SURFACE_UNREGISTERED"]
        assert any(dropped["path"] in v["message"] for v in missing)
    finally:
        mirror["endpoints"].append(dropped)          # 登记回来

    # 登记后转绿：同一份镜像补回该端点即一致
    again = claim_gate.check(mirror_path=mirror)
    assert again["ok"] is True, again["violations"]


# ---------------------------------------------------------------- 4.4 能力清单


def test_capability_list_splits_delivered_and_planned(claim_gate):
    """4.4 两段清单：501 全在「规划中」，不与已交付混排（条数取镜像真值）。"""
    claims = claim_gate.load_claims()
    mirror = claim_gate.load_mirror()
    from fd_erp_vertical_templates.publish import load_record
    text = claim_gate.render_capability_list(claims, mirror, load_record(),
                                             gaps_check())
    assert "## 一、已交付" in text
    assert f"## 二、规划中（{mirror['count']} 条" in text

    delivered = text.split("## 二、规划中", 1)[0]
    planned = text.split("## 二、规划中", 1)[1].split("## 三、", 1)[0]
    for endpoint in mirror["endpoints"]:
        assert f"`{endpoint['method']} {endpoint['path']}`" in planned
        assert endpoint["path"] not in delivered
    # 已交付段只列有证据的 claim，条数与 status=delivered 的声明数相等
    assert delivered.count("证据：") == sum(
        1 for c in claims["claims"] if c["status"] == "delivered")


def test_capability_list_is_regenerated_from_data(claim_gate):
    """4.4 清单不是手改文件：仓内文件 == 生成器输出。"""
    claims = claim_gate.load_claims()
    mirror = claim_gate.load_mirror()
    from fd_erp_vertical_templates.publish import load_record
    expected = claim_gate.render_capability_list(claims, mirror, load_record(),
                                                 gaps_check())
    actual = (REPO / "claims" / "capability-list.md").read_text(encoding="utf-8")
    assert actual == expected


# ---------------------------------------------------------------- 4.5 发布口径


def _record_with_empty_packs() -> dict:
    return {
        "schema": "erp-template-publish-record/v1",
        "updated_at": "2026-11-10",
        "surfaces": {
            "official-web.deploy": {"live": False, "evidence": "未 deploy"},
            "gateway.rollout": {"live": False, "evidence": "无集群变更"},
            "registry.snapshot": {"live": False, "evidence": "未写入"},
            "market.connector-packs": {"live": True, "evidence": "记录为空"},
        },
        "packs": {
            "supply-chain/connector-pack": {
                "asset": "connector-pack/pack.json",
                "published": False,
                "notes": {"notes": "未上架", "notes_en": "not published",
                          "casting": "未上架"},
            }
        },
    }


def _claims_asserting_live() -> dict:
    return {
        "schema": "erp-capability-claims/v1",
        "surfaces": {},
        "claims": [{
            "id": "market.claimed-live",
            "capability": "三个连接器包已在市场",
            "status": "delivered",
            "patterns": ["已在市场"],
            "asserts_live": "market.connector-packs",
            "evidence": [{"kind": "release", "ref": "supply-chain/connector-pack"}],
            "targets": [],
        }],
    }


def test_live_claim_with_empty_record_is_red(claim_gate):
    """4.5 负例：宣称已 live 而发布记录为空/未上架 → 红并点名宣称。"""
    result = claim_gate.check(claims_path=_claims_asserting_live(),
                              record_path=_record_with_empty_packs())
    assert result["ok"] is False
    codes = {v["code"] for v in result["violations"]}
    assert {"CLAIM_RELEASE_UNPUBLISHED", "CLAIM_LIVE_UNRECORDED"} <= codes
    # 本用例拿**合成输入**（只有一条 claim、空记录）去跑**真文档与真矩阵**：材料级违规
    # （生成块漂移、矩阵引用了这份合成里当然不存在的宣称）会一并出现，那不是本用例要验
    # 的东西。判据保留原意——两条红都必须**点名**这一条宣称，且不得牵连别的在册宣称。
    for code in ("CLAIM_RELEASE_UNPUBLISHED", "CLAIM_LIVE_UNRECORDED"):
        assert any(v["code"] == code and v.get("claim") == "market.claimed-live"
                   for v in result["violations"]), code
    in_scope = {v["claim"] for v in result["violations"]
                if v.get("claim") == "market.claimed-live"}
    assert in_scope == {"market.claimed-live"}


def test_publish_record_matches_market_ledger_and_docs():
    """4.5 正例：记录 ↔ pack 三处注记 ↔ 文档两处表格 ↔ 兄弟仓市场台账一致。"""
    from fd_erp_vertical_templates.publish import (
        check_docs, check_fleet_ledger, check_notes, load_record)
    record = load_record()
    for key in record["packs"]:
        tpl = REPO / "templates" / key.split("/")[0]
        drift = [v for v in check_notes(tpl, record=record)
                 if v.get("severity", "error") == "error"]
        assert drift == [], (key, drift)
    doc_violations = check_docs(
        REPO / "docs" / "connector-onboarding-supply-chain.md", record=record)
    assert doc_violations == []
    ledger = check_fleet_ledger(record=record)
    if ledger["verified"]:
        assert ledger["violations"] == []


def test_doc_table_drift_is_red(tmp_path):
    """4.5 负例：把文档 §5.1 的市场版本改错 → 红并报两值。"""
    from fd_erp_vertical_templates.publish import check_docs, load_record
    doc = tmp_path / "onboarding.md"
    text = (REPO / "docs" / "connector-onboarding-supply-chain.md").read_text(
        encoding="utf-8")
    doc.write_text(text.replace("| v1 → 现 v3 |", "| v1 → 现 v9 |"), encoding="utf-8")
    violations = check_docs(doc, record=load_record())
    drift = [v for v in violations if v["code"] == "DOC_MARKET_VERSION_DRIFT"]
    assert drift
    assert drift[0]["detail"]["doc_version"] == 9
    assert drift[0]["detail"]["record_version"] == 3


# ---------------------------------------------------------------- 4.6 不可宣称清单


def test_not_claimable_block_equals_generator(claim_gate):
    """4.6 生成块的不可宣称清单 == 生成器输出（手写清单已删除）。

    读入库夹具：验的是「块内容由生成器决定」这条机制，CI 单仓也能跑。
    生产文档是否已刷新由 test_claim_gate_passes_on_current_materials 负责。
    """
    claims = claim_gate.load_claims()
    text = BOARD_DOC_FIXTURE.read_text(encoding="utf-8")
    pair = claims["scan"]["generated_block_markers"]["not-claimable"]
    assert pair[0] in text and pair[1] in text
    block = text.split(pair[0], 1)[1].split(pair[1], 1)[0].strip()
    assert block == claim_gate.render_not_claimable(claims).strip()
    # 旧的手写列表必须已删除，不得以注释形式留一半
    assert "DO NOT claim" not in text
    assert "谦面市场已 live 上架、registry 快照已 live 写入" not in text


def test_hand_edited_block_is_red(tmp_path, claim_gate):
    """4.6 负例：在生成块里手改一行 → 红（块漂移）。"""
    doc = _prepare_doc(tmp_path, claim_gate)
    pair = claim_gate.load_claims()["scan"]["generated_block_markers"]["not-claimable"]
    text = doc.read_text(encoding="utf-8")
    edited = text.replace(pair[0], pair[0] + "\n- 手写的额外不可宣称项", 1)
    doc.write_text(edited, encoding="utf-8")

    result = claim_gate.check(board_doc=doc)
    assert result["ok"] is False
    assert {v["code"] for v in result["violations"]} == {"CLAIM_BLOCK_DRIFT"}


def test_release_status_block_is_derived(claim_gate):
    """4.6 发布状态块同样由记录派生（含「未上线（挂账）」三项）。读入库夹具。"""
    claims = claim_gate.load_claims()
    from fd_erp_vertical_templates.publish import load_record
    text = BOARD_DOC_FIXTURE.read_text(encoding="utf-8")
    pair = claims["scan"]["generated_block_markers"]["release-status"]
    block = text.split(pair[0], 1)[1].split(pair[1], 1)[0]
    record = load_record()
    for key, entry in record["packs"].items():
        assert f"v{entry['market_version']}" in block
        assert entry["pack_id"] in block
    for surface in ("official-web.deploy", "gateway.rollout", "registry.snapshot"):
        assert f"`{surface}`：未上线（挂账）" in block

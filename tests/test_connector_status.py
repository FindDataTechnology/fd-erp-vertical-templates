"""连接器三态状态面：证据链与派生的可测断言。

覆盖 openspec/changes/optimize-erp-solution/specs/erp-connector-onboarding/spec.md：
- 三态证据链（catalog / executor_tests / live），缺一态即 not_connected 并点出缺哪态
- 未接通不得冒充接通（live 无证据 → 派生失败）
- 角色绑定候选集从目录实测派生；空候选集必须阻断安装
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "scripts" / "build_connector_status.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("build_connector_status", MODULE_PATH)
    m = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def derived(mod):
    return mod.derive()


def _find(derived, provider, template=None):
    for item in derived["integrations"]:
        if item["provider"] == provider and (template is None or item["template"] == template):
            return item
    raise AssertionError(f"未找到 provider={provider} template={template}")


def test_kingdee_has_catalog_but_no_executor_test(derived):
    """金蝶：目录有 provider，但执行器无测试 → 不得声明已接通。"""
    item = _find(derived, "kingdee")
    assert item["states"]["catalog"]["ok"] is True
    assert item["states"]["executor_tests"]["ok"] is False, "金蝶执行器一旦补测试，这条断言会红——届时改成断言 live 才是缺口"
    assert item["states"]["live"]["ok"] is False
    assert item["state"] == "not_connected"
    assert item["missing"] == ["executor_tests", "live"]


def test_payment_provider_has_catalog_and_tests_but_no_live(derived):
    """微信支付：目录 + 执行器测试都有，缺 live 验收 → 仍是未接通。"""
    item = _find(derived, "wechatpay")
    assert item["states"]["catalog"]["ok"] is True
    assert item["states"]["executor_tests"]["ok"] is True
    assert item["states"]["live"]["ok"] is False
    assert item["missing"] == ["live"]


@pytest.mark.parametrize("role", ["wms", "pos", "cn-commerce", "supplier-portal", "carrier-portal"])
def test_role_binding_has_no_catalog_candidate_and_blocks_install(derived, role):
    """角色绑定：目录无同名 provider → 候选集空且安装被阻断（不做品牌猜测）。"""
    entry = next(r for r in derived["role_bindings"] if r["role"] == role)
    assert entry["candidates"] == []
    assert entry["install_blocked"] is True
    assert entry["reason"]


def test_live_claim_without_evidence_is_rejected(mod, tmp_path, monkeypatch):
    """Live 只能由带证据的验收记录成立：写 live 但不给证据 → 派生失败。"""
    fake = tmp_path / "acceptance.json"
    fake.write_text(
        json.dumps({"accepted": {"wechatpay": {"accepted_at": "2026-11-10", "evidence": ""}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "ACCEPTANCE_PATH", fake)
    d = mod.derive()
    problems = " ".join(d["verdict"]["problems"])
    assert "live 记录缺证据" in problems
    assert _find(d, "wechatpay")["state"] == "not_connected"


def test_live_claim_with_missing_evidence_file_is_rejected(mod, tmp_path, monkeypatch):
    """声称 live 但证据文件不存在 → 派生失败。"""
    fake = tmp_path / "acceptance.json"
    fake.write_text(
        json.dumps(
            {"accepted": {"wechatpay": {"accepted_at": "2026-11-10", "evidence": "no/such/file.txt"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "ACCEPTANCE_PATH", fake)
    d = mod.derive()
    assert any("不存在" in p for p in d["verdict"]["problems"])


def test_live_claim_with_real_evidence_marks_connected(mod, tmp_path, monkeypatch):
    """三态齐备（目录 + 测试 + 带证据的 live）才允许 connected。"""
    evidence = MODULE_PATH  # 用真实存在的文件充当证据
    fake = tmp_path / "acceptance.json"
    fake.write_text(
        json.dumps(
            {
                "accepted": {
                    "wechatpay": {
                        "accepted_at": "2026-11-10",
                        "evidence": str(evidence.relative_to(REPO.parent)),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "ACCEPTANCE_PATH", fake)
    d = mod.derive()
    item = _find(d, "wechatpay")
    assert item["state"] == "connected"
    assert item["missing"] == []


def test_planned_surface_counts_match_api_repo(derived):
    """3.7 跨产物对拍：501 挂账计数必须等于「同一真源的入库镜像」。

    CI 只有本仓 checkout，读不到纵向 API 仓；镜像 `claims/planned-surfaces.json`
    由 `claim_gate --update-planned` 从 API 仓真源回填，所以拿它做同一断言。
    兄弟仓在位时额外核验镜像与真源一致（这条在 CI 会 skip，属跨仓核验）。
    """
    api_planned = REPO.parent / "fd-erp-vertical-api" / "planned-surfaces.json"
    mirror = REPO / "claims" / "planned-surfaces.json"
    assert mirror.is_file(), "缺少挂账镜像 claims/planned-surfaces.json"

    mirror_items = json.loads(mirror.read_text(encoding="utf-8")).get("endpoints") or []
    assert derived["planned_surfaces"]["available"] is True
    assert derived["planned_surfaces"]["count"] == len(mirror_items)

    if not api_planned.is_file():
        pytest.skip("纵向 API 仓不在位：镜像与真源的跨仓一致性本机核验，CI 跳过")

    source_items = json.loads(api_planned.read_text(encoding="utf-8"))
    source_items = source_items.get("surfaces") or source_items.get("endpoints") or []
    assert len(source_items) == len(mirror_items), "镜像与 API 仓真源条数不一致，重跑 claim_gate --update-planned"


def test_committed_status_is_in_sync_with_derivation(mod, tmp_path, monkeypatch):
    """提交的 connector-status.json 必须等于派生结果（手改即红）。"""
    status = REPO / "connector-status.json"
    assert status.is_file()
    committed = json.loads(status.read_text(encoding="utf-8"))
    assert committed == mod.derive()

    tampered = tmp_path / "status.json"
    broken = json.loads(json.dumps(committed))
    broken["integrations"][0]["state"] = "connected"
    tampered.write_text(json.dumps(broken), encoding="utf-8")
    monkeypatch.setattr(mod, "STATUS_PATH", tampered)
    assert mod.main(["--check"]) == 1


# ---- 事实快照的覆盖面：模板引用了什么，快照就必须有什么 ------------------


def test_snapshot_covers_every_provider_the_templates_reference(mod):
    """现行数据：模板引用的每个 provider 都在快照里。

    这条是正向基线——它同时说明了「引用集」与「快照集」的口径来自哪里
    （`bindings()` 与 fixtures），不是另抄一份名单。
    """
    referenced = {b["provider"] for b in mod.bindings() if b.get("provider")}
    assert referenced, "模板未引用任何 provider？bindings() 可能读错了路径"
    assert mod.uncovered_bindings() == [], (
        f"快照缺模板已引用的 provider：{mod.uncovered_bindings()}——"
        "重跑 scripts/build_connector_status.py --refresh-fixture")


def test_missing_snapshot_coverage_breaks_the_gate(mod, tmp_path, monkeypatch):
    """机制：快照里少一个被引用的 provider → 派生必须报 problem（门红）。

    为什么要这条：有连接器仓 checkout 时派生走实时分支，快照缺项在本地完全看不见；
    CI 只有本仓、只能读快照，缺一项就把「有」写成「没有」，而门照绿。所以
    「快照够不够全」必须是断言，不能靠刷新时恰好带上。
    """
    full = json.loads((REPO / "tests" / "fixtures" / "connector-provider-facts.json")
                      .read_text(encoding="utf-8"))
    referenced = sorted({b["provider"] for b in mod.bindings() if b.get("provider")})
    assert referenced, "没有可删的引用项，用例失去意义"
    victim = referenced[0]
    trimmed = {"providers": {k: v for k, v in full["providers"].items() if k != victim}}
    fixture = tmp_path / "facts.json"
    fixture.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mod, "FACTS_FIXTURE", fixture)

    assert mod.uncovered_bindings() == [victim]

    problems = mod.derive()["verdict"]["problems"]
    assert any(victim in p and "事实快照缺" in p for p in problems), problems


def test_missing_snapshot_entirely_reports_unavailable_not_missing_coverage(mod, tmp_path, monkeypatch):
    """快照整体缺失时只报「事实不可得」，不额外刷一条覆盖面问题（避免噪音双报）。"""
    monkeypatch.setattr(mod, "FACTS_FIXTURE", tmp_path / "nope.json")
    monkeypatch.setattr(mod, "CONNECTOR_REPO", tmp_path / "no-connector-repo")
    assert mod.uncovered_bindings() == []
    problems = mod.derive()["verdict"]["problems"]
    assert any("事实不可得" in p for p in problems)
    assert not any("事实快照缺" in p for p in problems)

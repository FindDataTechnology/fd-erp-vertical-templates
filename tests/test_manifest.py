"""模板四件套清单 schema（任务 1.3）：加载、资产存在性、约束硬闸、pack v2 兼容。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.manifest import load_template_manifest
from fd_erp_vertical_templates.mapping import load_mapping_pack

REPO = Path(__file__).resolve().parent.parent
CROSSBORDER = REPO / "templates" / "crossborder"
RESTAURANT = REPO / "templates" / "restaurant-chain"


def test_both_manifests_load_with_assets():
    for root in (CROSSBORDER, RESTAURANT):
        m = load_template_manifest(root / "manifest.json")
        assert set(m["assets"]) == {"connector_pack", "fleet_blueprint",
                                    "account_mapping_pack", "report_preset"}
        assert m["constraints"]["bypass_ledger_forbidden"] is True


def test_constraint_lock_is_hard():
    """模板硬边界不可协商：篡改 constraints 任一键即拒。"""
    body = json.loads((CROSSBORDER / "manifest.json").read_text(encoding="utf-8"))
    tampered = copy.deepcopy(body)
    tampered["constraints"]["external_write"] = "read-and-write"
    with pytest.raises(TemplateError) as ei:
        load_template_manifest(tampered)
    assert "不可协商" in ei.value.message


def test_missing_asset_rejected():
    body = json.loads((RESTAURANT / "manifest.json").read_text(encoding="utf-8"))
    tampered = copy.deepcopy(body)
    del tampered["assets"]["report_preset"]
    with pytest.raises(TemplateError) as ei:
        load_template_manifest(tampered)
    assert "report_preset" in ei.value.message


def test_pack_v2_fields_enforced(tmp_path):
    """连接器/映射 pack 缺 pack v2 关键段（skills/mcpServers/_casting）即拒。"""
    body = json.loads((RESTAURANT / "connector-pack" / "pack.json")
                      .read_text(encoding="utf-8"))
    broken = copy.deepcopy(body)
    del broken["skills"]
    (tmp_path / "connector-pack").mkdir()
    (tmp_path / "connector-pack" / "pack.json").write_text(
        json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    manifest = json.loads((RESTAURANT / "manifest.json").read_text(encoding="utf-8"))
    manifest = copy.deepcopy(manifest)
    # 复制其余资产到临时目录（只换 connector pack 指向）
    import shutil
    for kind, asset in manifest["assets"].items():
        if kind == "connector_pack":
            continue
        src = RESTAURANT / asset["path"]
        dst = tmp_path / asset["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TemplateError) as ei:
        load_template_manifest(tmp_path / "manifest.json")
    assert "skills" in ei.value.message


def test_mapping_payloads_load_from_packs():
    xb = load_mapping_pack(CROSSBORDER / "account-mapping" / "pack.json")
    rest = load_mapping_pack(RESTAURANT / "account-mapping" / "pack.json")
    assert xb["version"] == "1.0.0" and rest["version"] == "1.0.0"
    names = {a["name_zh"] for a in rest["industry_accounts"]}
    assert "门店营业收入" in names and "平台佣金" in names


def test_fleet_blueprints_have_escalation():
    """两支舰队蓝图必须带 escalate 拍板面 + 三岗编制。"""
    for root, fleet_dir in ((CROSSBORDER, "recon-fleet"),
                            (RESTAURANT, "dailyclose-fleet")):
        bp = json.loads((root / fleet_dir / "template.json").read_text(encoding="utf-8"))
        assert bp["monitoring"]["escalate"]["threshold"]
        refs = [m["ref"] for m in bp["fleet"]["members"]]
        assert len(refs) == 3
        # 错峰 ≥30min
        crons = [m["cadence"]["cron"] for m in bp["fleet"]["members"]]
        assert len(set(crons)) == 3


# --------------------------------------------------------------------------
# 通用化纪律的机器校验（add-supply-chain-vertical tasks 8.4/8.5）
# 客户中性与中英双语不是一次性人工审计，而是每次跑测试都验的门。
# --------------------------------------------------------------------------

SUPPLY_CHAIN = REPO / "templates" / "supply-chain"

#: 锚点客户名（只允许出现在提案/design 语境，MUST NOT 出现在模板资产里）
ANCHOR_CUSTOMER_MARKERS = ("全日", "全日供应链", "全日数字")


def _supply_chain_json_manifests() -> list[Path]:
    return sorted(SUPPLY_CHAIN.glob("manifest.json")) + \
        sorted(SUPPLY_CHAIN.glob("*/manifest.json")) + \
        sorted(SUPPLY_CHAIN.glob("pagesets/*/manifest.json"))


def test_supply_chain_packs_are_bilingual():
    """每个模板/pack 清单都要有中文名 + 英文名 + i18n.en（8.4 多语言 zh/en 全过）。"""
    manifests = _supply_chain_json_manifests()
    assert len(manifests) >= 5, f"清单数量异常：{len(manifests)}"
    for path in manifests:
        body = json.loads(path.read_text(encoding="utf-8"))
        name_zh = body.get("name_zh") or body.get("name")
        assert name_zh, f"{path} 缺中文名"
        assert body.get("name_en"), f"{path} 缺 name_en"
        i18n = body.get("i18n") or {}
        assert isinstance(i18n, dict) and i18n.get("en"), f"{path} 缺 i18n.en"
        assert i18n["en"].get("name"), f"{path} 的 i18n.en 缺 name"


def test_supply_chain_assets_are_customer_neutral():
    """模板资产（spec 与资产名）MUST NOT 出现锚点客户名（design D1）。

    锚点客户信息只允许存在于提案与实施语境（proposal.md/design.md），
    因此这里扫的是 templates/supply-chain 全树文本。
    """
    offenders: list[str] = []
    for path in SUPPLY_CHAIN.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".md", ".py", ".ts", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in ANCHOR_CUSTOMER_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO)}: {marker}")
    assert offenders == [], f"模板资产出现客户名（须移到提案/design）：{offenders}"


def test_supply_chain_specs_are_customer_neutral():
    """spec delta 同样客户中性（design D1：清单不进任何 spec）。"""
    specs = Path("/Users/chengsishi/finddata/openspec/changes/add-supply-chain-vertical/specs")
    if not specs.exists():  # 归档后路径消失则跳过（归档进 openspec/specs/ 另行治理）
        pytest.skip("change specs 目录不存在（可能已归档）")
    offenders: list[str] = []
    for path in specs.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in ANCHOR_CUSTOMER_MARKERS:
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert offenders == [], f"spec 出现客户名（D1 违规）：{offenders}"



# --------------------------------------------------------------------------
# 蓝图可铸造性：成员 target 必须在家门口解析得到
# （add-supply-chain-vertical tasks 5.4；2026-11-08 修：三岗原先都指向不存在的
# presetId=erp-supply-recon-discipline，铸造必空岗。此处把「解析得到」变成门。）
# --------------------------------------------------------------------------

_PACK_PLACEHOLDER = "{{PACK_ID:"

#: 发布名 → 本仓目录名。key 取 fleet-packs 上架时的源目录名（跨仓一致），
#: 本仓目录只是模板资产落位；二者不同名时在此显式登记，不靠猜。
_PUBLISH_TO_LOCAL = {
    "supply-chain-connector-pack": "connector-pack",
}

#: 跨仓岗位包源（fleet-packs 是铸造源仓；模板的 target 可以指向那里的包）。
#: 可用 ERP_LEDGER_FLEET_PACKS_ERP 覆盖——CI 不检出该仓，本地用它复现 CI 的 skip 面。
FLEET_PACKS_ERP = Path(os.environ.get("ERP_LEDGER_FLEET_PACKS_ERP")
                       or REPO.parent / "fleet-packs" / "erp")


def _local_packs() -> dict[str, Path]:
    """可解析的 pack：发布名（或目录名）→ manifest/pack 路径。

    两个来源：本仓模板资产（templates/supply-chain/**/pack.json）+ 跨仓铸造源
    （fleet-packs/erp/*/manifest.json）。岗位包放铸造源仓而不是模板资产，理由：
    模板资产受边界审计（确认门不许离开拍板席位）与 appliance 打包摘要约束，
    而岗位包是运行时供给物。
    """
    out: dict[str, Path] = {}
    for path in SUPPLY_CHAIN.rglob("pack.json"):
        out[path.parent.name] = path
        for pub, local in _PUBLISH_TO_LOCAL.items():
            if local == path.parent.name:
                out[pub] = path
    if FLEET_PACKS_ERP.exists():
        for path in FLEET_PACKS_ERP.glob("*/manifest.json"):
            out[path.parent.name] = path
    return out


def _agent_ids(pack_path: Path) -> set[str]:
    body = json.loads(pack_path.read_text(encoding="utf-8"))
    return {a["id"] for a in body.get("agents", []) if isinstance(a, dict) and a.get("id")}


def _fleet_blueprints() -> list[Path]:
    return sorted(SUPPLY_CHAIN.glob("*/template.json"))


def test_blueprint_pack_presets_resolve_to_local_agents():
    """target.packId 用 {{PACK_ID:<dir>}} 时，presetId 必须是该包声明的 agent id。

    指向在册外仓包（真实 packId 字面量，如 Mo3_…）的不在此门范围——它们的
    preset 不在本仓，靠 registry 校验。占位符形态必须能就地解析，否则铸造
    会拿到空岗而不报错。
    """
    packs = _local_packs()
    assert packs, "未找到任何 pack.json"
    checked = 0
    for bp_path in _fleet_blueprints():
        bp = json.loads(bp_path.read_text(encoding="utf-8"))
        for member in bp.get("fleet", {}).get("members", []):
            target = member.get("target") or {}
            if target.get("kind") != "pack-preset":
                continue
            pack_id = target.get("packId", "")
            if not pack_id.startswith(_PACK_PLACEHOLDER):
                continue
            name = pack_id[len(_PACK_PLACEHOLDER):].rstrip("}")
            if name not in packs and not FLEET_PACKS_ERP.exists():
                # 跨仓铸造源（fleet-packs）不在位：这条我们**看不到**，不是它错了。
                # 如实跳过，而不是让 CI 因为少检出一个兄弟仓而红（2026-11-08 首次上 CI 即撞）。
                pytest.skip(f"跨仓铸造源不在位（{FLEET_PACKS_ERP}），无法核对 {{PACK_ID:{name}}}")
            assert name in packs, f"{bp_path.name}: {member.get('ref')} 引用了本仓无源的包 {name}"
            ids = _agent_ids(packs[name])
            assert target.get("presetId") in ids, (
                f"{bp_path.name}: {member.get('ref')} 的 presetId={target.get('presetId')} "
                f"不在 {name} 的 agents 里（{sorted(ids)}）——铸造会空岗"
            )
            checked += 1
    assert checked >= 3, f"可铸造成员数量异常：{checked}"


# --------------------------------------------------------------------------
# appliance 副本 ↔ 模板源同步门（2026-11-08）
# 起因：appliance 的舰队蓝图副本悄悄落后于模板，包里躺着一支无法铸造的舰队
# （三成员指向不存在的岗）。副本必须是逐字段拷贝，否则打包发出去的是旧世界。
# --------------------------------------------------------------------------

#: appliance 打包副本源；CI 不检出该仓，可用 ERP_LEDGER_APPLIANCE_TENANTS 覆盖以复现。
APPLIANCE_TENANTS = Path(os.environ.get("ERP_LEDGER_APPLIANCE_TENANTS")
                         or REPO.parent / "fd-appliance" / "tenants")
APPLIANCE_TENANT = APPLIANCE_TENANTS / "supply-chain" / "content"

# 三个行业租户各自的副本清单。只守 supply-chain 时，crossborder 与 restaurant-chain
# 的连接器包副本会悄悄落后（2026-11-08 实测：两个租户的 connector-pack 都不是逐字节
# 拷贝，而门只扫一个租户，于是门是绿的、打包出去的却是旧世界）。
_APPLIANCE_COPIES = {
    "supply-chain": {
        "erp-templates/supply-chain-connector-pack.json": "connector-pack/pack.json",
        "erp-templates/supply-chain-account-mapping-pack.json": "account-mapping/pack.json",
        "erp-templates/supply-chain-report-preset.json": "report-preset/preset.json",
        "erp-templates/supply-chain-pricing-rules-pack.json": "pricing-rules/pack.json",
        "fleet/warehousing-fleet.json": "warehousing-fleet/template.json",
    },
    "crossborder": {
        "erp-templates/crossborder-connector-pack.json": "connector-pack/pack.json",
        "erp-templates/crossborder-account-mapping-pack.json": "account-mapping/pack.json",
        "erp-templates/crossborder-report-preset.json": "report-preset/preset.json",
    },
    "restaurant-chain": {
        "erp-templates/restaurant-connector-pack.json": "connector-pack/pack.json",
        "erp-templates/restaurant-account-mapping-pack.json": "account-mapping/pack.json",
        "erp-templates/restaurant-report-preset.json": "report-preset/preset.json",
    },
}


def test_appliance_copies_match_template_sources():
    """appliance 内的副本必须与模板源逐字段相等（不含格式差异）。"""
    if not APPLIANCE_TENANTS.exists():
        pytest.skip(f"appliance 副本目录不存在（{APPLIANCE_TENANTS}）——本仓单独检出时跳过")
    drifted: list[str] = []
    for tenant, copies in _APPLIANCE_COPIES.items():
        tenant_dir = APPLIANCE_TENANTS / tenant / "content"
        template_dir = REPO / "templates" / tenant
        if not tenant_dir.exists():
            drifted.append(f"{tenant}: appliance 租户目录缺失")
            continue
        for rel, src_rel in copies.items():
            copy_path = tenant_dir / rel
            src_path = template_dir / src_rel
            assert src_path.exists(), f"模板源缺失：{src_path}"
            if not copy_path.exists():
                drifted.append(f"{tenant}/{rel}: appliance 副本缺失")
                continue
            a = json.loads(copy_path.read_text(encoding="utf-8"))
            b = json.loads(src_path.read_text(encoding="utf-8"))
            if a != b:
                extra_a, extra_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
                drifted.append(
                    f"{tenant}/{rel}: 与 {src_rel} 不一致（appliance 独有键 {extra_a} / 模板独有键 {extra_b}）"
                )
    assert drifted == [], f"appliance 副本落后于模板：{drifted}"

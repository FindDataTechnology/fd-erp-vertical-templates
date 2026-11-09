"""模板铸造与发布状态门（optimize-erp-solution tasks 2.1-2.6 / 3.5）。

每条验收判据都是「篡改即红」：正例证明当前仓绿，负例证明红的时候点名到资产、
两值、角色或文件位置。负例全部在 tmp 副本上做——真模板资产只读。
"""
from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest

from fd_erp_vertical_templates.bindings import check_template_role_bindings
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.foundry import scaffold_template
from fd_erp_vertical_templates.manifest import load_template_manifest
from fd_erp_vertical_templates.publish import (
    apply_notes,
    check_notes,
    derive_notes,
    load_record,
)
from fd_erp_vertical_templates.validation import validate_template

REPO = Path(__file__).resolve().parent.parent
SUPPLY = REPO / "templates" / "supply-chain"
RESTAURANT = REPO / "templates" / "restaurant-chain"

SCAFFOLD_ARGS = dict(vertical="cold-chain", name_zh="冷链仓储模板",
                     name_en="Cold Chain Template",
                     description_zh="冷链仓储行业的对账与配货模板骨架。",
                     description_en="Cold-chain warehousing template skeleton.")


def _copy_template(tmp_path: Path, name: str = "supply-chain") -> Path:
    target = tmp_path / name
    shutil.copytree(REPO / "templates" / name, target)
    return target


# ---------------------------------------------------------------- 2.1 骨架


def test_new_vertical_scaffold_passes_validation(tmp_path):
    """2.1 生成即通过校验：四件套齐全、清单可加载、机器可读结果绿。"""
    dest = tmp_path / "cold-chain"
    result = scaffold_template(dest, **SCAFFOLD_ARGS)

    assert result["validated"] is True
    for rel in ("manifest.json", "connector-pack/pack.json", "fleet/template.json",
                "account-mapping/pack.json", "report-preset/preset.json"):
        assert (dest / rel).is_file(), f"缺少骨架文件 {rel}"
    manifest = load_template_manifest(dest)
    assert manifest["vertical"] == "cold-chain"
    assert set(manifest["assets"]) == {"connector_pack", "fleet_blueprint",
                                       "account_mapping_pack", "report_preset"}

    v = validate_template(dest)
    assert v["ok"] is True, v["violations"]
    assert v["violations"] == []


def test_new_vertical_missing_asset_is_named(tmp_path):
    """2.1 负例：删掉报表 preset 目录 → 失败并点名资产种类与期望路径。"""
    dest = tmp_path / "cold-chain"
    scaffold_template(dest, **SCAFFOLD_ARGS)
    shutil.rmtree(dest / "report-preset")

    with pytest.raises(TemplateError) as ei:
        load_template_manifest(dest)
    assert "report_preset" in ei.value.message
    assert "report-preset/preset.json" in ei.value.message

    v = validate_template(dest)
    assert v["ok"] is False
    codes = {(x["code"], x.get("asset")) for x in v["violations"]}
    assert ("ASSET_MISSING", "report_preset") in codes
    assert any("report-preset/preset.json" in x["message"] for x in v["violations"])


def test_validate_cli_reports_missing_asset_instead_of_traceback(tmp_path, capsys):
    """2.1 CLI 路径：删资产后 `validate` 出机器可读结果并退出 1（不是 traceback）。"""
    from fd_erp_vertical_templates.cli import _run_validate

    dest = tmp_path / "cold-chain"
    scaffold_template(dest, **SCAFFOLD_ARGS)
    shutil.rmtree(dest / "report-preset")

    assert _run_validate(str(dest)) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["vertical"] == "cold-chain"
    violation = out["violations"][0]
    assert violation["code"] == "ASSET_MISSING"
    assert violation["asset"] == "report_preset"
    assert violation["path"] == "report-preset/preset.json"
    assert "report-preset/preset.json" in violation["message"]


def test_new_vertical_optional_packs(tmp_path):
    """2.2 带扩展件生成仍通过校验；不带时骨架不含空目录。"""
    plain = tmp_path / "plain"
    scaffold_template(plain, **{**SCAFFOLD_ARGS, "vertical": "plain-vertical"})
    assert not (plain / "pricing-rules").exists()
    assert not (plain / "pagesets").exists()
    assert validate_template(plain)["ok"] is True

    with_ext = tmp_path / "with-ext"
    scaffold_template(with_ext, with_pricing_rules=True, with_pagesets=True,
                      **{**SCAFFOLD_ARGS, "vertical": "ext-vertical"})
    assert (with_ext / "pricing-rules/pack.json").is_file()
    assert (with_ext / "pagesets/pack.json").is_file()
    v = validate_template(with_ext)
    assert v["ok"] is True, v["violations"]
    assert v["assets"]["pricing_rules_pack"]["file"] == \
        v["assets"]["pricing_rules_pack"]["declared"]
    assert v["assets"]["pagesets_pack"]["file"] == \
        v["assets"]["pagesets_pack"]["declared"]


def test_new_vertical_requires_source_description(tmp_path):
    """铸造纪律：没填来源说明的骨架不进仓（design Risks）。"""
    with pytest.raises(TemplateError) as ei:
        scaffold_template(tmp_path / "x", **{**SCAFFOLD_ARGS, "description_zh": "  "})
    assert "description_zh" in ei.value.message


# ---------------------------------------------------------------- 2.3 版本对拍


def test_version_drift_reports_both_values_and_paths(tmp_path):
    """2.3 清单 1.0.0 vs pack 1.1.0 → 红，同时报两值与两个文件路径。"""
    tpl = _copy_template(tmp_path)
    manifest_path = tpl / "manifest.json"
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    body["assets"]["connector_pack"]["version"] = "1.0.0"
    manifest_path.write_text(json.dumps(body, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    with pytest.raises(TemplateError) as ei:
        load_template_manifest(tpl)
    message = ei.value.message
    assert "1.0.0" in message and "1.1.0" in message
    detail = ei.value.detail
    assert detail["violation"] == "ASSET_VERSION_MISMATCH"
    item = detail["violations"][0]
    assert item["asset"] == "connector_pack"
    assert item["detail"]["declared"] == "1.0.0"
    assert item["detail"]["asset_version"] == "1.1.0"
    # 两个路径都要给：只说「不一致」等于让人自己去找
    assert item["detail"]["manifest"].endswith("manifest.json")
    assert item["detail"]["asset_path"].endswith("connector-pack/pack.json")

    v = validate_template(tpl)
    assert v["ok"] is False
    assert v["violations"][0]["code"] == "ASSET_VERSION_MISMATCH"
    assert v["violations"][0]["path"] == "connector-pack/pack.json"


def test_non_semver_version_rejected(tmp_path):
    """2.3 负例：任一资产版本不是 semver → 红并点名该资产。

    两条路径都覆盖：清单声明值非法（结构层拒绝）与资产文件自带值非法（资产层拒绝）。
    """
    tpl = _copy_template(tmp_path)
    manifest_path = tpl / "manifest.json"
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    body["assets"]["report_preset"]["version"] = "1.0"
    manifest_path.write_text(json.dumps(body, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    with pytest.raises(TemplateError) as ei:
        load_template_manifest(tpl)
    assert "report_preset" in ei.value.message and "semver" in ei.value.message

    v = validate_template(tpl)
    assert v["ok"] is False
    assert any("report_preset" in x["message"] and "semver" in x["message"]
               for x in v["violations"])

    # 资产文件自带版本非法（清单仍合法）→ 资产层违规码 + 资产名定位
    tpl2 = _copy_template(tmp_path / "second", name="supply-chain")
    preset_path = tpl2 / "report-preset" / "preset.json"
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    preset["version"] = "1.0"
    preset_path.write_text(json.dumps(preset, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    v2 = validate_template(tpl2)
    codes = {(x["code"], x.get("asset")) for x in v2["violations"]}
    assert ("ASSET_VERSION_INVALID", "report_preset") in codes


def test_current_templates_have_no_version_drift():
    """2.4 现状：supply-chain 与 restaurant-chain 清单与资产逐项相等。"""
    for tpl in (SUPPLY, RESTAURANT):
        v = validate_template(tpl)
        assert not [x for x in v["violations"]
                    if x["code"].startswith("ASSET_VERSION")], v["violations"]
        for kind, info in v["assets"].items():
            if kind == "capability_matrix":
                continue
            assert info["declared"] == info["file"], (tpl.name, kind, info)


# ---------------------------------------------------------------- 2.5 机器可读


def test_machine_readable_violation_fields(tmp_path):
    """2.5 违规项带模板名/资产名/违规码/定位，可据码定位到文件。"""
    tpl = _copy_template(tmp_path)
    (tpl / "report-preset" / "preset.json").unlink()
    manifest_path = tpl / "manifest.json"
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    body["assets"]["connector_pack"]["version"] = "1.0.0"
    manifest_path.write_text(json.dumps(body, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    v = validate_template(tpl)
    assert v["schema"] == "erp-vertical-validation/v1"
    assert v["ok"] is False
    by_code = {x["code"]: x for x in v["violations"]}
    assert {"ASSET_MISSING", "ASSET_VERSION_MISMATCH"} <= set(by_code)
    for item in v["violations"]:
        assert item["severity"] == "error"
        assert item["asset"] and item["path"] and item["message"]
        assert (tpl / item["path"]).exists() or item["code"] == "ASSET_MISSING"
    assert by_code["ASSET_MISSING"]["asset"] == "report_preset"
    assert by_code["ASSET_VERSION_MISMATCH"]["asset"] == "connector_pack"


def test_validation_is_deterministic_and_offline(tmp_path, monkeypatch):
    """2.5 同一输入两次结果逐字一致；不依赖网络（连接被禁也照跑）。"""
    tpl = _copy_template(tmp_path)
    body = json.loads((tpl / "manifest.json").read_text(encoding="utf-8"))
    body["assets"]["connector_pack"]["version"] = "1.0.0"
    (tpl / "manifest.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")

    first = validate_template(tpl)

    def _no_network(*a, **kw):  # 任何出网尝试都当失败
        raise AssertionError("校验器不得联网")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    second = validate_template(tpl)
    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 2.6 发布状态


def _make_contradictory(pack_path: Path, *, record: dict) -> None:
    """三处自述互相打脸：顶层/本地化说「未上架」，casting 说「已上架 v3」。"""
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["notes"] = ("publish-ready 源清单（未上架，packId 未铸造）。只声明外部系统"
                     "绑定契约与连接器纪律卡。")
    pack.setdefault("i18n", {}).setdefault("en", {})["notes"] = (
        "Publish-ready source (not yet published; packId not minted). "
        "Declares external-system bindings only.")
    pack["_casting"]["note"] = ("连接器包只声明外部系统绑定契约（connector_bindings）"
                                "与纪律卡。已上架：市场 v3（packId "
                                "y8gnMHFMze-Mylsd6JXpkA），无 MCP 工具面。")
    pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")


def test_publish_state_contradiction_is_red_then_derived_green(tmp_path):
    """2.6 顶层 notes 与 casting 互相矛盾 → 红；从发布记录重派生 → 绿。"""
    tpl = _copy_template(tmp_path)
    record = load_record()
    pack_path = tpl / "connector-pack" / "pack.json"
    _make_contradictory(pack_path, record=record)

    violations = check_notes(tpl, record=record)
    assert violations, "矛盾注记必须判红"
    places = {x.get("place") for x in violations}
    assert {"notes", "casting"} <= places
    assert all(x["code"] == "PUBLISH_NOTE_DRIFT" for x in violations)
    notes_v = next(x for x in violations if x["place"] == "notes")
    assert "已上架：市场 v3" in notes_v["message"]          # 记录值
    assert "未上架" in notes_v["message"]                   # 文件值
    assert notes_v["detail"]["published"] is True
    assert notes_v["detail"]["market_version"] == 3

    # 校验器入口同样红（不是只有 check_notes 红）
    assert validate_template(tpl)["ok"] is False

    # 由记录重写三处 → 全绿，且三处与派生值逐字相等
    applied = apply_notes(tpl, record=record)
    assert set(applied["changed"]) == {"notes", "notes_en", "casting"}
    assert check_notes(tpl, record=record) == []
    entry = record["packs"]["supply-chain/connector-pack"]
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    wanted = derive_notes(entry)
    assert pack["notes"] == wanted["notes"]
    assert pack["i18n"]["en"]["notes"] == wanted["notes_en"]
    assert pack["_casting"]["note"] == wanted["casting"]
    assert validate_template(tpl)["ok"] is True


def test_unpublished_pack_without_record_entry_is_not_a_claim(tmp_path):
    """未上架的 honest 自述不是「已上架声明」：无记录条目不判红（铸造骨架即此形）。"""
    dest = tmp_path / "draft"
    scaffold_template(dest, **SCAFFOLD_ARGS)
    assert check_notes(dest) == []
    assert validate_template(dest)["ok"] is True


# ---------------------------------------------------------------- 3.5 角色绑定


def test_wms_role_install_blocked_and_named():
    """3.5 候选集来自 connector-status.json；wms 空 → 阻断并点名角色与原因。"""
    blocked = check_template_role_bindings(SUPPLY)
    wms = next(item for item in blocked if item["role"] == "wms")
    assert wms["code"] == "ROLE_NO_CANDIDATE"
    assert wms["binding"] == "wms-snapshot"
    assert wms["candidates"] == []
    assert "wms" in wms["message"] and "候选集为空" in wms["message"]
    assert "目录无该角色可用 provider" in wms["message"]
    assert wms["fix"]

    # 安装门压红；结构校验不因角色缺 provider 变红（两种状态分开报）
    assert validate_template(SUPPLY, install_gate=True)["ok"] is False
    plain = validate_template(SUPPLY)
    assert plain["ok"] is True
    assert {b["role"] for b in plain["install"]["blocked"]} >= {"wms"}


def test_role_candidate_appears_after_catalog_growth():
    """3.5 正例：状态面候选集补齐后不再阻断（不改模板代码）。"""
    status = {
        "schema": "erp-connector-status/v1",
        "role_bindings": [
            {"role": role, "templates": ["supply-chain"],
             "candidates": [f"{role}-acme"], "install_blocked": False,
             "reason": ""}
            for role in ("wms", "supplier-portal", "carrier-portal")
        ],
    }
    assert check_template_role_bindings(SUPPLY, status=status) == []


def test_role_missing_from_status_is_unverified_not_silent():
    """状态面没有该角色 → 明确报「无法核对」，不静默放行。"""
    status = {"schema": "erp-connector-status/v1", "role_bindings": []}
    blocked = check_template_role_bindings(SUPPLY, status=status)
    assert blocked and all(b["code"] == "ROLE_STATUS_UNVERIFIED" for b in blocked)

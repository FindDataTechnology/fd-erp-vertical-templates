"""行业模板四件套清单 schema（erp-vertical-templates 任务 1.3 / design D1）。

四件套 = 连接器 pack + 舰队蓝图 + 科目映射 pack + 报表 preset：
- 版本化：清单与四件各自带独立版本（可独立升级）。
- 市场分发：连接器/映射 pack 为 pack v2 形状（fleet-packs 口径：skills/
  mcpServers/fleet/_casting），清单本身是市场安装单元。
- appliance 兼容：清单相对路径引用，packer 可整树进整机包。
- 扩展 pack（可选件，add-supply-chain-vertical）：pricing_rules_pack（取价
  规则 pack）/ pagesets_pack（页面集 pack）——既有模板不带仍合法，声明时
  须过各自面校验。

清单约束段是硬约束（boundary.py 按其审计）：
ledger_access=kernel-tools-only / bypass_ledger_forbidden=true /
writeback_lane=kernel-proposal-whitelist / external_write=none。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import TemplateError

TEMPLATE_SCHEMA = "erp-vertical-template/v1"
ASSET_KINDS = ("connector_pack", "fleet_blueprint", "account_mapping_pack", "report_preset")
#: 扩展 pack 类型（add-supply-chain-vertical：取价规则 pack / 页面集 pack）。
#: 可选件：既有模板不带仍合法；声明时须过各自面校验。
OPTIONAL_ASSET_KINDS = ("pricing_rules_pack", "pagesets_pack")
ASSET_KIND_VALUES = ("pack", "fleet-blueprint", "report-preset",
                     "rules-pack", "pageset-pack")
_CONSTRAINT_LOCKS = {
    "ledger_access": "kernel-tools-only",
    "bypass_ledger_forbidden": True,
    "writeback_lane": "kernel-proposal-whitelist",
    "external_write": "none",
}
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def load_template_manifest(source: str | Path | dict[str, Any],
                           *, validate_assets: bool = True) -> dict[str, Any]:
    """加载并校验模板四件套清单。

    validate_assets=True 时逐一读取四件资产文件（存在性 + 各自 schema 面校验）。
    """
    body: dict[str, Any]
    if isinstance(source, dict):
        body = source
        root = None
    else:
        path = Path(source)
        if path.is_dir():  # 模板目录形态：清单约定为目录下 manifest.json
            path = path / "manifest.json"
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TemplateError("MANIFEST_INVALID", f"清单不可读：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise TemplateError("MANIFEST_INVALID", f"清单不是合法 JSON：{exc}") from exc
        root = path.parent
    _validate(body)
    if validate_assets and root is not None:
        _validate_assets(body, root)
    return body


def _validate(body: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        raise TemplateError("MANIFEST_INVALID", "清单须为对象")
    if body.get("schema") != TEMPLATE_SCHEMA:
        raise TemplateError("MANIFEST_INVALID", f"schema 须为 {TEMPLATE_SCHEMA}",
                            detail={"found": body.get("schema")})
    if not str(body.get("vertical") or "").strip():
        raise TemplateError("MANIFEST_INVALID", "缺 vertical")
    for field in ("name_zh", "name_en", "version"):
        if not str(body.get(field) or "").strip():
            raise TemplateError("MANIFEST_INVALID", f"缺 {field}")
    if not _SEMVER.match(str(body.get("version"))):
        raise TemplateError("MANIFEST_INVALID", f"version 须为 semver：{body.get('version')}")
    assets = body.get("assets")
    if not isinstance(assets, dict):
        raise TemplateError("MANIFEST_INVALID", "缺 assets")
    checked_kinds = list(ASSET_KINDS) + [
        k for k in OPTIONAL_ASSET_KINDS if k in assets]
    for kind in checked_kinds:
        asset = assets.get(kind)
        if not isinstance(asset, dict):
            raise TemplateError("MANIFEST_INVALID", f"缺四件套资产：{kind}")
        if asset.get("kind") not in ASSET_KIND_VALUES:
            raise TemplateError("MANIFEST_INVALID", f"{kind}.kind 非法")
        if not str(asset.get("path") or "").strip():
            raise TemplateError("MANIFEST_INVALID", f"{kind}.path 必填")
        if not _SEMVER.match(str(asset.get("version") or "")):
            raise TemplateError("MANIFEST_INVALID", f"{kind}.version 须为 semver")
    constraints = body.get("constraints")
    if not isinstance(constraints, dict):
        raise TemplateError("MANIFEST_INVALID", "缺 constraints（模板硬边界）")
    for key, locked in _CONSTRAINT_LOCKS.items():
        if constraints.get(key) != locked:
            raise TemplateError(
                "MANIFEST_INVALID",
                f"constraints.{key} 必须为 {locked!r}（模板硬边界，不可协商）",
                detail={"found": constraints.get(key)},
            )


def _validate_assets(body: dict[str, Any], root: Path) -> None:
    assets = body["assets"]
    checked_kinds = list(ASSET_KINDS) + [
        k for k in OPTIONAL_ASSET_KINDS if k in assets]
    for kind in checked_kinds:
        path = root / assets[kind]["path"]
        if not path.is_file():
            raise TemplateError("MANIFEST_INVALID",
                                f"{kind} 资产不存在：{assets[kind]['path']}")
        text = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TemplateError("MANIFEST_INVALID",
                                f"{kind} 资产不是合法 JSON：{exc}") from exc
        if kind == "fleet_blueprint":
            _check_blueprint(parsed, path)
        elif kind in ("connector_pack", "account_mapping_pack",
                      "pricing_rules_pack", "pagesets_pack"):
            _check_pack(parsed, path)
        else:  # report_preset
            _check_preset(parsed, path)


def _check_blueprint(bp: dict[str, Any], path: Path) -> None:
    """舰队蓝图面校验（fleet-packs/blueprints/schema.md v2 关键字段）。"""
    rel = str(path)
    if not isinstance(bp, dict):
        raise TemplateError("MANIFEST_INVALID", f"{rel}: 蓝图须为对象")
    if not isinstance(bp.get("agents"), list) or not bp["agents"]:
        raise TemplateError("MANIFEST_INVALID", f"{rel}: 蓝图 agents 须为非空数组")
    fleet = bp.get("fleet")
    if not isinstance(fleet, dict) or not isinstance(fleet.get("members"), list):
        raise TemplateError("MANIFEST_INVALID", f"{rel}: 蓝图 fleet.members 缺失")
    if not isinstance(fleet.get("orchestration"), dict):
        raise TemplateError("MANIFEST_INVALID", f"{rel}: 蓝图 orchestration 缺失")
    if not isinstance(bp.get("monitoring"), dict) or not bp["monitoring"].get("escalate"):
        raise TemplateError("MANIFEST_INVALID",
                            f"{rel}: 蓝图 monitoring.escalate 缺失（差异须可拍板）")


def _check_pack(pack: dict[str, Any], path: Path) -> None:
    """pack v2 兼容面校验（fleet-packs manifest 口径的最小闭包）。

    扩展 pack 类型在其载荷键上追加面校验（缺载荷键同样拒绝）：
    - pricing_rules_pack：pricing_rules（erp-pricing-rules/v1，规则带出处）；
    - pagesets_pack：pagesets（页面集清单）。
    """
    rel = str(path)
    if not isinstance(pack, dict):
        raise TemplateError("MANIFEST_INVALID", f"{rel}: pack 须为对象")
    if not str(pack.get("name") or "").strip():
        raise TemplateError("MANIFEST_INVALID", f"{rel}: pack 缺 name")
    if not isinstance(pack.get("skills"), list) or not pack["skills"]:
        raise TemplateError("MANIFEST_INVALID", f"{rel}: pack 缺 skills（发布门下限）")
    if not isinstance(pack.get("mcpServers"), list):
        raise TemplateError("MANIFEST_INVALID", f"{rel}: pack 缺 mcpServers")
    if not isinstance(pack.get("_casting"), dict):
        raise TemplateError("MANIFEST_INVALID", f"{rel}: pack 缺 _casting")
    payload = pack.get("pricing_rules")
    if payload is not None:
        _check_pricing_rules_payload(payload, rel)
    pagesets = pack.get("pagesets")
    if pagesets is not None:
        _check_pagesets_payload(pagesets, rel)


def _check_pricing_rules_payload(payload: dict[str, Any], rel: str) -> None:
    """取价规则载荷面：schema/规则带出处（rate_source）——无出处规则不上架。"""
    if not isinstance(payload, dict) or payload.get("schema") != "erp-pricing-rules/v1":
        raise TemplateError("MANIFEST_INVALID",
                            f"{rel}: pricing_rules schema 须为 erp-pricing-rules/v1")
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise TemplateError("MANIFEST_INVALID", f"{rel}: pricing_rules.rules 缺失")
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or not str(r.get("layer") or "").strip():
            raise TemplateError("MANIFEST_INVALID", f"{rel}: rules[{i}] 缺 layer")
        if not str(r.get("rate_source") or "").strip():
            raise TemplateError("MANIFEST_INVALID",
                                f"{rel}: rules[{i}] 缺 rate_source（无出处规则拒绝上架）")


def _check_pagesets_payload(pagesets: Any, rel: str) -> None:
    """页面集载荷面：四端页面集清单（key/端/pages 齐备）。"""
    if not isinstance(pagesets, list) or not pagesets:
        raise TemplateError("MANIFEST_INVALID", f"{rel}: pagesets 须为非空数组")
    for i, ps in enumerate(pagesets):
        if not isinstance(ps, dict) or not str(ps.get("key") or "").strip():
            raise TemplateError("MANIFEST_INVALID", f"{rel}: pagesets[{i}] 缺 key")
        if not str(ps.get("target_end") or "").strip():
            raise TemplateError("MANIFEST_INVALID",
                                f"{rel}: pagesets[{i}] 缺 target_end（ue/se/sp/cp/miniapp）")
        if not isinstance(ps.get("pages"), list) or not ps["pages"]:
            raise TemplateError("MANIFEST_INVALID", f"{rel}: pagesets[{i}] 缺 pages")


def _check_preset(preset: dict[str, Any], path: Path) -> None:
    rel = str(path)
    if not isinstance(preset, dict):
        raise TemplateError("MANIFEST_INVALID", f"{rel}: preset 须为对象")
    if preset.get("schema") != "erp-report-preset/v1":
        raise TemplateError("MANIFEST_INVALID", f"{rel}: preset schema 须为 erp-report-preset/v1")
    if not isinstance(preset.get("reports"), list) or not preset["reports"]:
        raise TemplateError("MANIFEST_INVALID", f"{rel}: preset 缺 reports")

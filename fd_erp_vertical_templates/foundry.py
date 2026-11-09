"""模板铸造（optimize-erp-solution tasks 2.1 / 2.2，design D10）。

一条命令从零生成行业模板骨架：清单 + 四件套（连接器 pack / 舰队蓝图 /
科目映射 pack / 报表 preset），可选扩展件（取价规则 pack / 页面集 pack）。
生成物**立即**进校验器——铸不出来却看着像模板的骨架，比没有骨架更坏。

设计取舍（design D10）：
- 铸造在模板仓，发布仍走兄弟仓 `fleet-packs/tooling/publish_pack.py`（不复制）。
- 骨架是「最小可校验」，不是「填好内容的模板」：行业名、来源说明必填，角色绑定
  默认不声明（角色得有目录实测 provider 才写得进去，见 bindings.py）。
- 扩展件按参数生成；不带参数时**不生成空目录**（空壳会被人当成已完成）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .manifest import TEMPLATE_SCHEMA, load_template_manifest

DEFAULT_VERSION = "0.1.0"
_VERTICAL_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

#: 骨架资产文件名（市场分发形态：pack v2 形状的 JSON）。
ASSET_FILES = {
    "connector_pack": "connector-pack/pack.json",
    "fleet_blueprint": "fleet/template.json",
    "account_mapping_pack": "account-mapping/pack.json",
    "report_preset": "report-preset/preset.json",
    "pricing_rules_pack": "pricing-rules/pack.json",
    "pagesets_pack": "pagesets/pack.json",
}


def scaffold_template(dest: str | Path, *, vertical: str, name_zh: str, name_en: str,
                      description_zh: str, description_en: str,
                      version: str = DEFAULT_VERSION,
                      with_pricing_rules: bool = False,
                      with_pagesets: bool = False,
                      validate: bool = True) -> dict[str, Any]:
    """生成模板骨架；返回生成的文件清单与校验结果。

    `dest` 必须不存在或为空目录——覆盖别人的模板不是「铸造」，是破坏。
    """
    if not _VERTICAL_RE.match(vertical):
        raise TemplateError("INVALID_ARGUMENT",
                            f"vertical 须为小写 kebab-case：{vertical!r}")
    for label, value in (("name_zh", name_zh), ("name_en", name_en),
                         ("description_zh", description_zh),
                         ("description_en", description_en)):
        if not str(value or "").strip():
            raise TemplateError("INVALID_ARGUMENT",
                                f"{label} 必填：未填来源说明的模板不允许进仓（design Risks）")
    root = Path(dest)
    if root.exists() and any(root.iterdir()):
        raise TemplateError("INVALID_ARGUMENT", f"目标目录非空：{root}")

    assets: dict[str, dict[str, Any]] = {
        kind: {"kind": _asset_kind(kind), "path": ASSET_FILES[kind], "version": version}
        for kind in ("connector_pack", "fleet_blueprint",
                     "account_mapping_pack", "report_preset")
    }
    if with_pricing_rules:
        assets["pricing_rules_pack"] = {"kind": "rules-pack",
                                        "path": ASSET_FILES["pricing_rules_pack"],
                                        "version": version}
    if with_pagesets:
        assets["pagesets_pack"] = {"kind": "pageset-pack",
                                   "path": ASSET_FILES["pagesets_pack"],
                                   "version": version}

    manifest = {
        "schema": TEMPLATE_SCHEMA,
        "vertical": vertical,
        "name_zh": name_zh,
        "name_en": name_en,
        "version": version,
        "description_zh": description_zh,
        "description_en": description_en,
        "assets": assets,
        "constraints": {
            "ledger_access": "kernel-tools-only",
            "bypass_ledger_forbidden": True,
            "writeback_lane": "kernel-proposal-whitelist",
            "external_write": "none",
        },
        "pack_v2": {"compatible": True,
                    "note": "四件套均为 pack v2 形状；市场安装模板 = 全件一并到位、各件独立升级。"},
        "i18n": {"en": {"name": name_en, "description": description_en}},
        "boundaries_zh": {
            "sourcing": description_zh,
            "external_write": "对任何外部系统零写入（只读滩头）",
        },
        "casting": {"generated_by": "new-vertical"},
    }

    written: list[str] = []
    _write(root / "manifest.json", manifest)
    written.append("manifest.json")
    for kind, asset in assets.items():
        rel = asset["path"]
        body = _asset_body(kind, vertical=vertical, name_zh=name_zh,
                           name_en=name_en, description_zh=description_zh,
                           description_en=description_en, version=version)
        _write(root / rel, body)
        written.append(rel)

    result: dict[str, Any] = {"ok": True, "template": str(root), "vertical": vertical,
                              "version": version, "written": sorted(written),
                              "optional": {"pricing_rules": with_pricing_rules,
                                           "pagesets": with_pagesets}}
    if validate:
        load_template_manifest(root)   # 失败即抛（生成物必须立刻可校验）
        result["validated"] = True
    return result


def _asset_kind(kind: str) -> str:
    return {"connector_pack": "pack", "fleet_blueprint": "fleet-blueprint",
            "account_mapping_pack": "pack", "report_preset": "report-preset",
            "pricing_rules_pack": "rules-pack",
            "pagesets_pack": "pageset-pack"}[kind]


def _asset_body(kind: str, *, vertical: str, name_zh: str, name_en: str,
                description_zh: str, description_en: str,
                version: str) -> dict[str, Any]:
    if kind == "connector_pack":
        return {
            "schema": "erp-vertical-connector-pack/v1",
            "name": f"{name_zh}·连接器包",
            "version": version,
            "description": description_zh,
            "tags": ["ERP", vertical, "connector"],
            "notes": _unpublished_note(description_zh),
            "visibility": "public",
            "category": "facet",
            "connector_bindings": [],
            "skills": [{"name": f"erp-{vertical}-connector-discipline",
                        "description": ("连接器纪律卡：外部系统对接只经连接器载体；"
                                        "BYOK 凭证零落库零日志；未接通如实 NOT_WIRED。")}],
            "mcpServers": [],
            "fleet": {"members": [], "orchestration": {"mode": "handoff"}},
            "_casting": {"packDependencies": {},
                         "note": _unpublished_note(description_zh)},
            "i18n": {"en": {"name": f"{name_en} connector pack",
                            "description": description_en,
                            "notes": _unpublished_note(description_en, lang="en")}},
        }
    if kind == "fleet_blueprint":
        return {
            "name": f"{name_zh}·舰队蓝图（模板）",
            "version": version,
            "description": description_zh,
            "agents": [{"id": f"{vertical}-watcher", "name": "巡检岗",
                        "description": "按周期巡检数据与差异",
                        "persona": "每回合巡检数据源，差异如实报出，不猜。"},
                       {"id": f"{vertical}-reconciler", "name": "核对岗",
                        "description": "差异逐条定性并引用证据",
                        "persona": "逐条定性差异，引用双侧原始引用；结论经 proposal 回写。"},
                       {"id": f"{vertical}-decider", "name": "拍板岗",
                        "description": "升级差异裁决（确认门唯一落账）",
                        "persona": "对升级差异裁决；确认门只在本席位。"}],
            "fleet": {
                "members": [
                    {"ref": f"{vertical}-watcher", "cadence": {"cron": "0 1 * * *"}},
                    {"ref": f"{vertical}-reconciler", "cadence": {"cron": "30 1 * * *"}},
                    {"ref": f"{vertical}-decider", "cadence": {"cron": "0 2 * * *"}},
                ],
                "orchestration": {"mode": "watch-reconcile-approve"},
            },
            "monitoring": {"escalate": {"threshold": "未定性差异 > 0 或金额差异超阈值",
                                        "seat": "owner"}},
            "_casting": {"packDependencies": {},
                         "note": "骨架蓝图：三岗在本包内联，铸造前按行业补 target。"},
        }
    if kind == "account_mapping_pack":
        return {
            "schema": "erp-vertical-connector-pack/v1",
            "name": f"{name_zh}·科目映射包",
            "version": version,
            "description": ("行业科目对齐内核通用科目（呈现层）；映射只影响报表与查询，"
                            "不改已落账分录。"),
            "tags": ["ERP", vertical, "科目映射"],
            "visibility": "public",
            "category": "facet",
            "account_mapping": {
                "schema": "erp-account-mapping/v1",
                "vertical": vertical,
                "version": version,
                "universal_preset_version": "v1",
                "industry_accounts": [
                    {"code": f"{vertical[:2].upper()}-0001", "name_zh": "行业营业收入",
                     "name_en": "Industry operating revenue", "maps_to": "4000",
                     "kind": "revenue"},
                    {"code": f"{vertical[:2].upper()}-0002", "name_zh": "行业营业成本",
                     "name_en": "Industry cost of sales", "maps_to": "5000",
                     "kind": "expense"},
                ],
            },
            "skills": [{"name": f"erp-{vertical}-mapping-discipline",
                        "description": "映射纪律卡：只做呈现层对齐，不向内核注入科目行。"}],
            "mcpServers": [],
            "fleet": {"members": [], "orchestration": {"mode": "presentation"}},
            "_casting": {"packDependencies": {}, "note": "骨架映射包：科目按行业补齐。"},
        }
    if kind == "report_preset":
        return {
            "schema": "erp-report-preset/v1",
            "name": f"{name_zh}·报表 preset",
            "vertical": vertical,
            "version": version,
            "notes": "骨架 preset：报表口径须带出处；无出处数字不上报。",
            "reports": [{"key": f"{vertical}-daily",
                         "title_zh": "日结对账日报",
                         "title_en": "Daily reconciliation report",
                         "kind": "report",
                         "sections": [{"key": "orders", "label_zh": "订单流",
                                       "label_en": "Orders", "metrics": ["order_count"]},
                                      {"key": "diffs", "label_zh": "差异",
                                       "label_en": "Diffs",
                                       "metrics": ["missing_in_right", "extra_in_right"]}],
                         "caliber": "原币对账；本位币折算带汇率出处，无出处不折算。"}],
        }
    if kind == "pricing_rules_pack":
        return {
            "schema": "erp-vertical-connector-pack/v1",
            "name": f"{name_zh}·取价规则包",
            "version": version,
            "description": "三层取价规则骨架（客户价/活动价/券）；规则须带出处 rate_source。",
            "tags": ["ERP", vertical, "取价"],
            "notes": _unpublished_note("骨架规则集：规则为占位口径，实施时按客户价格政策替换。"),
            "visibility": "public",
            "category": "facet",
            "pricing_rules": {
                "schema": "erp-pricing-rules/v1",
                "vertical": vertical,
                "version": version,
                "layers": ["customer_price", "promo", "coupon"],
                "rules": [{"layer": "customer_price",
                           "scope": {"product_code": "*", "customer_id": "*"},
                           "price": "0.00",
                           "valid_from": "2026-01-01", "valid_to": "2027-12-31",
                           "rate_source": f"template-seed:{vertical}/price-list/v1",
                           "note": "骨架示例：实施时替换为客户价格政策"}],
            },
            "skills": [{"name": f"erp-{vertical}-pricing-discipline",
                        "description": "取价纪律卡：无出处规则拒绝参与取价；同维度重叠拒绝配置。"}],
            "mcpServers": [],
            "fleet": {"members": [], "orchestration": {"mode": "pricing"}},
            "_casting": {"packDependencies": {}, "note": "骨架取价规则包。"},
        }
    if kind == "pagesets_pack":
        return {
            "schema": "erp-vertical-connector-pack/v1",
            "name": f"{name_zh}·页面集包",
            "version": version,
            "description": ("四端页面集骨架（UE/SE/SP/CP）：页面建在既有小程序底座，"
                            "数据与 MCP 同源，确认门交互一致。"),
            "tags": ["ERP", vertical, "页面集"],
            "visibility": "public",
            "category": "facet",
            "pagesets": [
                {"key": f"{vertical}-ue", "target_end": "ue", "title_zh": "UE 工作台",
                 "title_en": "UE console", "pages": ["home", "list", "detail"]},
            ],
            "skills": [{"name": f"erp-{vertical}-pagesets-discipline",
                        "description": "页面集纪律卡：载体唯一、数据同源、确认门交互一致。"}],
            "mcpServers": [],
            "fleet": {"members": [], "orchestration": {"mode": "pagesets"}},
            "_casting": {"packDependencies": {}, "note": "骨架页面集包。"},
        }
    raise TemplateError("INVALID_ARGUMENT", f"未知资产种类：{kind}")


def _unpublished_note(description: str, *, lang: str = "zh") -> str:
    """新铸造的 pack 一律先写「未上架」——上架状态只能由 publish-record.json 派生。"""
    from .publish import _CLAUSE_UNPUBLISHED_EN, _CLAUSE_UNPUBLISHED_ZH

    clause = _CLAUSE_UNPUBLISHED_EN if lang == "en" else _CLAUSE_UNPUBLISHED_ZH
    text = str(description).strip().rstrip("。. ")
    return f"{clause}。{text}。"


def _write(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")


__all__ = ["ASSET_FILES", "DEFAULT_VERSION", "scaffold_template"]

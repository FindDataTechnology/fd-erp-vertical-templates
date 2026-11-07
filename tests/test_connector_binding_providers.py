"""绑定声明的 provider 必须是连接器目录真实存在的 service id（防「角色名」误写）。

为什么要有这道门：`connector_bindings[].provider` 的语义是**萬星连接器目录里的
service id**（paas `lib/pack-manifest.js` 只校验形状，不校验存在性——paas 在 CI
里读不到目录）。于是 `provider: "wms"` 这种「业务角色名」能一路通过校验，落地后
绑定解析只能报 `unwired`，而且是**永久**的：客户接什么账号都关不上。供应链包
就踩了这个坑（3/5 条绑定永久缺口，其中 `wms-snapshot` 还是非 optional，安装门
永远显示未解决）。

目录在 open-connector-mt 仓，CI 读不到 → 把 service id 集快照进
`tests/fixtures/connector-catalog-service-ids.json`（`scripts/refresh_connector_catalog_ids.py`
生成）。本文件的两条断言：

1. 每个声明（含 `alternativeProviders`）的 provider ∈ 快照 ∪ `ROLE_PROVIDERS`（显式
   登记、带理由与决策指针的角色名；登记项本身不许腐坏）；
2. 本机有目录 checkout 时，快照必须与目录当前值逐值相等（CI 无 checkout 则跳过）。

非 optional 的角色 provider 不判红——那要产品拍板（改成真厂商 id 还是降为 optional），
门只负责把这类声明从「静默通过」变成「必须登记」，并在 pytest 输出里点名。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates"
SNAPSHOT = REPO / "tests" / "fixtures" / "connector-catalog-service-ids.json"
REFRESH = REPO / "scripts" / "refresh_connector_catalog_ids.py"

# 已登记的角色名：目录里没有同名 service id，声明方是业务角色。
# 每项必须写清 reason（为什么是角色不是厂商）与 decision（谁来关掉它）。
# 关掉的方式只有两种：改成目录里的真 service id，或把该绑定降为 optional。
ROLE_PROVIDERS: dict[str, dict[str, str]] = {
    "wms": {
        "reason": "客户 WMS 品牌未定（接口形态待锚点客户确认），只能用角色名占位",
        "decision": "已降 optional（非 optional 却无从连接 = 安装门挂一条用户处理不了的项）；客户确认品牌后改真 id（目录现有最近项 wangdian，仅 read 面）并恢复非 optional。见 docs/connector-onboarding-supply-chain.md §4.3",
    },
    "supplier-portal": {
        "reason": "目录没有任何 SRM/供应商门户 provider，角色名无厂商可填",
        "decision": "在 open-connector-mt 新增 SRM provider 后换 id；本仓不能自造。见同上文档 §4.3",
    },
    "carrier-portal": {
        "reason": "承运商是多个厂商的集合，单条声明无法写死一家",
        "decision": "改真 id（目录有 sf_express / kuaidi100，可作主+备选）或降 optional；见同上文档 §4.3",
    },
}


def _service_ids() -> set[str]:
    body = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    ids = body["service_ids"]
    assert body["count"] == len(ids), "快照 count 与 service_ids 长度不一致"
    return set(ids)


def _connector_packs() -> list[tuple[str, list[dict]]]:
    """所有带 connector_bindings 的包（按路径命名，便于报错定位）。"""
    out: list[tuple[str, list[dict]]] = []
    for path in sorted(TEMPLATES.rglob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue  # 非 manifest 的坏 JSON 由 manifest 测试负责
        bindings = body.get("connector_bindings") if isinstance(body, dict) else None
        if isinstance(bindings, list) and bindings:
            out.append((str(path.relative_to(REPO)), bindings))
    assert out, "没有任何包声明 connector_bindings —— 该门失去意义，检查 glob 是否写错"
    return out


def test_every_declared_provider_is_a_real_catalog_service_id():
    catalog = _service_ids()
    bad: list[str] = []
    seen_roles: set[str] = set()
    for where, bindings in _connector_packs():
        for binding in bindings:
            provider = binding.get("provider")
            key = binding.get("key")
            if provider in ROLE_PROVIDERS:
                seen_roles.add(provider)
            elif provider not in catalog:
                bad.append(f"{where}: {key} → provider {provider!r} 不在目录，也未登记为角色名")
            for alt in binding.get("alternativeProviders") or []:
                if alt not in catalog:
                    # 备选是「换个厂商也能连」，写角色名没有意义且同样永久缺口
                    bad.append(f"{where}: {key} → alternativeProvider {alt!r} 不在目录")
    assert not bad, "绑定声明指向了目录里不存在的 service id:\n  " + "\n  ".join(bad)
    # 登记不许腐坏：登记了却没人用 → 删掉，别留一张过期的豁免表
    stale = sorted(set(ROLE_PROVIDERS) - seen_roles)
    assert not stale, f"ROLE_PROVIDERS 里有已无人声明的条目（删掉）：{stale}"


def test_role_provider_registry_entries_carry_reason_and_decision():
    for provider, entry in ROLE_PROVIDERS.items():
        assert set(entry) == {"reason", "decision"}, f"{provider}: 只允许 reason/decision 两键"
        assert len(entry["reason"]) > 10 and len(entry["decision"]) > 10, f"{provider}: 理由与决策太短"


def test_non_optional_binding_must_name_a_real_provider():
    """非 optional = 「装完之后这条必须已满足」。指向目录里不存在的 id 时它永远满足不了，
    安装门就永久挂着一条用户无从处理的未解决项（客户接什么账号都关不上）——那不是
    「能力缺口」，是对用户的错误指引。所以角色名只允许出现在 optional 绑定上。

    沿用历史：`wms-snapshot` 原本非 optional + provider=wms，就是这条规则的第一个违规；
    2026-11-08 降为 optional（客户 WMS 品牌未定），真 id 确认后改回。"""
    violations = [
        f"{where}: {b.get('key')}（provider={b.get('provider')}）— 角色名却不能 optional"
        for where, bindings in _connector_packs()
        for b in bindings
        if b.get("provider") in ROLE_PROVIDERS and not b.get("optional")
    ]
    assert not violations, (
        "非 optional 绑定声明了目录里不存在的 provider（安装门永久未解决）：\n  "
        + "\n  ".join(violations)
    )


def _load_refresh_module():
    spec = importlib.util.spec_from_file_location("refresh_catalog_ids", REFRESH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_snapshot_matches_the_live_catalog_when_the_checkout_is_present():
    refresh = _load_refresh_module()
    if not (refresh.catalog_dir() / "catalog" / "apps").is_dir():
        pytest.skip("没有 open-connector-mt checkout（设 CONNECTOR_CATALOG_DIR 可跑）")
    rendered = refresh.render(refresh.read_service_ids())
    assert rendered == SNAPSHOT.read_text(encoding="utf-8"), (
        "目录已变化，快照过期 —— 跑 python3 scripts/refresh_connector_catalog_ids.py"
    )

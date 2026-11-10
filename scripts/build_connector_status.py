#!/usr/bin/env python3
"""派生 connector-status.json：外部系统对接的三态证据链。

三态（design D5）：
  catalog         连接器目录里有该 provider（open-connector-mt/catalog/apps/<id>.json）
  executor_tests  provider 执行器有测试（src/providers/<id>/*.test.ts）
  live            有 live 验收记录（connector-live-acceptance.json 里的条目 + 证据文件）

live 只能人工写入验收记录后成立——扫描代码永远推不出「已接通」，那样必然假绿。

用法：
  python3 scripts/build_connector_status.py            # 重新派生并写 connector-status.json
  python3 scripts/build_connector_status.py --check    # 只校验现有文件是否与派生结果一致
  python3 scripts/build_connector_status.py --print    # 打到 stdout，不写盘

退出码：0 一致；1 不一致（live 缺证据、清单漂移、501 挂账对不上）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FINDDATA = REPO.parent
CONNECTOR_REPO = FINDDATA / "open-connector-mt"
API_REPO = FINDDATA / "fd-erp-vertical-api"

STATUS_PATH = REPO / "connector-status.json"
ACCEPTANCE_PATH = REPO / "connector-live-acceptance.json"
TEMPLATES = REPO / "templates"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_providers() -> set[str]:
    apps = CONNECTOR_REPO / "catalog" / "apps"
    if not apps.is_dir():
        return set()
    return {p.stem for p in apps.glob("*.json")}


def executor_test_count(provider: str) -> int:
    d = CONNECTOR_REPO / "src" / "providers" / provider
    if not d.is_dir():
        return 0
    return len(list(d.glob("*.test.ts")))


FACTS_FIXTURE = REPO / "tests" / "fixtures" / "connector-provider-facts.json"


def provider_facts() -> tuple[dict[str, dict], str]:
    """(provider → {catalog, executor_tests}, 来源)。

    有连接器仓 checkout 就读实时；CI 只有本仓，读入库快照（仓约定：优先 checkout，
    没有就读 fixtures 快照，同 `.github/workflows/gates.yml` 对面级对位门的说明）。
    两条路径必须导出同一份事实，否则 `--check` 会在其中一侧假红。
    """
    if (CONNECTOR_REPO / "catalog" / "apps").is_dir():
        catalog = catalog_providers()
        return (
            {p: {"catalog": True, "executor_tests": executor_test_count(p)} for p in catalog},
            "live:open-connector-mt",
        )
    if FACTS_FIXTURE.is_file():
        data = _load(FACTS_FIXTURE)
        # 快照可能在仓外（测试把夹具指到 tmp、或有人用配置改路径）。直接
        # relative_to(REPO) 会在那种情形抛 ValueError，把「换了个路径」变成崩溃。
        try:
            label = FACTS_FIXTURE.relative_to(REPO).as_posix()
        except ValueError:
            label = str(FACTS_FIXTURE)
        return dict(data.get("providers") or {}), f"snapshot:{label}"
    return {}, "missing"


def refresh_facts_fixture() -> int:
    """从实时连接器仓重写事实快照（本地刷新用；CI 不调用）。"""
    providers = sorted(p.stem for p in (CONNECTOR_REPO / "catalog" / "apps").glob("*.json"))
    facts = {
        p: {"catalog": True, "executor_tests": executor_test_count(p)} for p in providers
    }
    # 绑定里提到的、但目录没有的角色名也要留一行，否则快照会"看起来都齐"
    for b in bindings():
        facts.setdefault(str(b["provider"]), {"catalog": False, "executor_tests": 0})
    body = {
        "_comment": (
            "open-connector-mt 的 provider 事实快照。CI 只有本仓 checkout，读不到连接器仓；"
            "三态必须能离线推导。刷新：scripts/build_connector_status.py --refresh-fixture。"
        ),
        "source": "open-connector-mt",
        "count": len(facts),
        "providers": facts,
    }
    FACTS_FIXTURE.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return len(facts)


def snapshot_covered_providers() -> set[str]:
    """仓内事实快照覆盖了哪些 provider。快照不在 → 空集（缺件由 provider_facts 报）。"""
    if not FACTS_FIXTURE.is_file():
        return set()
    data = _load(FACTS_FIXTURE)
    return set((data.get("providers") or {}).keys())


def uncovered_bindings() -> list[str]:
    """模板已引用、但事实快照里没有的 provider（排序）。

    为什么单独查这一条：有连接器仓 checkout 时派生走**实时**扫描，快照缺项在这一侧
    完全看不出来；CI 只有本仓、只能读快照，缺一项就会把「目录里有这个 provider」
    派生成「目录里没有」——把「不知道」写成「不存在」，而门照绿。所以快照的覆盖面
    必须是**被断言的事**，不能靠上次刷新时恰好带上。
    """
    covered = snapshot_covered_providers()
    if not covered:
        return []  # 快照整体缺失另行报错，不重复计
    referenced = {b["provider"] for b in bindings() if b.get("provider")}
    return sorted(referenced - covered)


def bindings() -> list[dict]:
    out: list[dict] = []
    for manifest in sorted(TEMPLATES.glob("*/connector-pack/pack.json")):
        pack = _load(manifest)
        template = manifest.parent.parent.name
        for b in pack.get("connector_bindings", []):
            out.append(
                {
                    "template": template,
                    "key": b.get("key"),
                    "external_system": b.get("external_system"),
                    "provider": b.get("provider"),
                    "provider_role": bool(b.get("providerRole")),
                    "optional": bool(b.get("optional")),
                    "direction": b.get("direction"),
                }
            )
    return out


def live_records() -> dict[str, dict]:
    if not ACCEPTANCE_PATH.is_file():
        return {}
    data = _load(ACCEPTANCE_PATH)
    return data.get("accepted", {}) if isinstance(data, dict) else {}


def _evidence_exists(rel: str) -> bool:
    """验收记录里的证据路径必须真实存在（相对 finddata 根或模板仓）。"""
    if not rel:
        return False
    for base in (FINDDATA, REPO):
        if (base / rel).exists():
            return True
    return False


def planned_surfaces() -> dict:
    """501 挂账计数：优先读纵向 API 仓，CI 读 claims/ 下的入库镜像（同源派生）。"""
    src = API_REPO / "planned-surfaces.json"
    if not src.is_file():
        src = REPO / "claims" / "planned-surfaces.json"
    if not src.is_file():
        return {"available": False, "count": None, "by_capability": {}}
    data = _load(src)
    items = data.get("surfaces") or data.get("endpoints") or []
    by_cap: dict[str, int] = {}
    for it in items:
        cap = it.get("capability") or it.get("cap") or "?"
        by_cap[cap] = by_cap.get(cap, 0) + 1
    return {"available": True, "count": len(items), "by_capability": dict(sorted(by_cap.items()))}


def derive() -> dict:
    facts, facts_source = provider_facts()
    catalog = {p for p, f in facts.items() if f.get("catalog")}
    live = live_records()
    problems: list[str] = []
    if facts_source == "missing":
        problems.append(
            "连接器 provider 事实不可得：既没有 open-connector-mt checkout，也没有 "
            "tests/fixtures/connector-provider-facts.json（三态无法判定，不得当成全部缺失）"
        )

    # 快照必须覆盖模板已引用的每个 provider——否则 CI（只能读快照）会把被引用的
    # provider 派生成「目录里没有」，而实时侧不是，两侧结论分叉且无人被拦。
    uncovered = uncovered_bindings()
    if uncovered:
        problems.append(
            "事实快照缺模板已引用的 provider：" + "、".join(uncovered)
            + "（重跑 scripts/build_connector_status.py --refresh-fixture；"
            "缺项会在只有本仓的 CI 里被误判成「目录无此 provider」）"
        )

    integrations: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for b in bindings():
        provider = b["provider"]
        if not provider:
            problems.append(f"{b['template']}/{b['key']}：绑定缺 provider 字段")
            continue
        if (b["template"], provider) in seen:
            # 同一模板下同 provider 只留一条（支付渠道可能多处引用）
            for item in integrations:
                if item["template"] == b["template"] and item["provider"] == provider:
                    item["binding_keys"].append(b["key"])
            continue
        seen.add((b["template"], provider))

        cat_ok = provider in catalog and not b["provider_role"]
        tests_n = int((facts.get(provider) or {}).get("executor_tests") or 0)
        rec = live.get(provider)
        live_ok = False
        live_evidence = None
        if rec:
            live_evidence = rec.get("evidence")
            if not _evidence_exists(live_evidence or ""):
                problems.append(
                    f"live 记录缺证据：provider={provider} evidence={live_evidence!r} 不存在"
                )
            elif not cat_ok:
                problems.append(f"live 记录存在但目录无 provider：{provider}")
            elif tests_n == 0:
                problems.append(f"live 记录存在但执行器无测试：{provider}")
            else:
                live_ok = True

        states = {
            "catalog": {
                "ok": cat_ok,
                "evidence": f"open-connector-mt/catalog/apps/{provider}.json" if cat_ok else None,
            },
            "executor_tests": {
                "ok": tests_n > 0,
                "evidence": f"open-connector-mt/src/providers/{provider}/*.test.ts ({tests_n})"
                if tests_n
                else None,
            },
            "live": {
                "ok": live_ok,
                "evidence": live_evidence if live_ok else None,
                "accepted_at": rec.get("accepted_at") if rec else None,
            },
        }
        missing = [k for k, v in states.items() if not v["ok"]]
        integrations.append(
            {
                "template": b["template"],
                "binding_keys": [b["key"]],
                "external_system": b["external_system"],
                "provider": provider,
                "provider_role": b["provider_role"],
                "optional": b["optional"],
                "states": states,
                "state": "connected" if not missing else "not_connected",
                "missing": missing,
            }
        )

    # 角色绑定：候选集来自目录实测；空集必须显式阻断安装
    roles: dict[str, dict] = {}
    for b in bindings():
        if not b["provider_role"]:
            continue
        role = b["provider"]
        entry = roles.setdefault(
            role,
            {"role": role, "templates": [], "binding_keys": [], "candidates": [], "install_blocked": True},
        )
        if b["template"] not in entry["templates"]:
            entry["templates"].append(b["template"])
        entry["binding_keys"].append(f"{b['template']}/{b['key']}")
        # 目录里与角色同名的 provider 才算候选（不做品牌猜测）
        entry["candidates"] = sorted(c for c in catalog if c == role)
    for entry in roles.values():
        entry["install_blocked"] = not entry["candidates"]
        entry["reason"] = (
            "目录无该角色可用 provider；由客户在连接器里选定实际品牌后按其流程新增"
            if entry["install_blocked"]
            else "目录已有该角色 provider"
        )

    planned = planned_surfaces()
    return {
        "schema": "erp-connector-status/v1",
        "source": {
            "catalog": "open-connector-mt/catalog/apps",
            "provider_tests": "open-connector-mt/src/providers/*/*.test.ts",
            "bindings": "templates/*/connector-pack/pack.json#connector_bindings",
            "live_acceptance": "connector-live-acceptance.json",
            "planned_surfaces": "fd-erp-vertical-api/planned-surfaces.json",
        },
        "integrations": sorted(integrations, key=lambda i: (i["template"], i["provider"])),
        "role_bindings": [roles[k] for k in sorted(roles)],
        "planned_surfaces": planned,
        "verdict": {
            "connected": sorted(f"{i['template']}/{i['provider']}" for i in integrations if i["state"] == "connected"),
            "not_connected": sorted(
                f"{i['template']}/{i['provider']}" for i in integrations if i["state"] != "connected"
            ),
            "problems": problems,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="派生 / 校验连接器三态状态面")
    ap.add_argument("--check", action="store_true", help="只校验，不写盘")
    ap.add_argument("--print", dest="do_print", action="store_true", help="打到 stdout")
    ap.add_argument("--refresh-fixture", action="store_true",
                    help="从连接器仓 checkout 重写 tests/fixtures/connector-provider-facts.json")
    args = ap.parse_args(argv)

    if args.refresh_fixture:
        if not (CONNECTOR_REPO / "catalog" / "apps").is_dir():
            print(f"FAIL: 没有连接器仓 checkout（{CONNECTOR_REPO}），无法刷新事实快照",
                  file=sys.stderr)
            return 1
        n = refresh_facts_fixture()
        print(f"wrote {FACTS_FIXTURE.relative_to(FINDDATA)}（{n} 个 provider）")
        return 0

    derived = derive()
    if args.do_print:
        print(json.dumps(derived, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not derived["verdict"]["problems"] else 1

    if args.check:
        if not STATUS_PATH.is_file():
            print(f"FAIL: {STATUS_PATH.name} 不存在，先跑一次不带 --check 的派生", file=sys.stderr)
            return 1
        current = _load(STATUS_PATH)
        if current != derived:
            print("FAIL: connector-status.json 与派生结果不一致，重新派生后提交", file=sys.stderr)
            for p in derived["verdict"]["problems"]:
                print(f"  - {p}", file=sys.stderr)
            return 1
        if derived["verdict"]["problems"]:
            print("FAIL: 三态证据链有问题：", file=sys.stderr)
            for p in derived["verdict"]["problems"]:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(
            "OK: 三态与派生一致；连接 %d / 未接通 %d；501 挂账 %s 条"
            % (
                len(derived["verdict"]["connected"]),
                len(derived["verdict"]["not_connected"]),
                derived["planned_surfaces"]["count"],
            )
        )
        return 0

    STATUS_PATH.write_text(
        json.dumps(derived, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {STATUS_PATH.relative_to(FINDDATA)}")
    for p in derived["verdict"]["problems"]:
        print(f"  problem: {p}", file=sys.stderr)
    return 0 if not derived["verdict"]["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

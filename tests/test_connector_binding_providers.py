"""绑定声明的 provider 语义：要么是目录里真实的 service id，要么显式标成角色。

为什么要有这道门：`connector_bindings[].provider` 默认语义是**萬星连接器目录里的
service id**（paas `lib/pack-manifest.js` 只校验形状，不校验存在性——paas 在 CI
里读不到目录）。在角色绑定出现之前，写成 `provider: "wms"` 这种「业务角色名」能
一路通过校验，落地后绑定解析只能报 `unwired` 且是**永久**的：客户接什么账号都
关不上。供应链包就踩过这个坑（3/5 条绑定永久缺口，其中 `wms-snapshot` 还是非
optional，安装门永远显示未解决）。

2026-11-08 起 paas 支持角色绑定（`add-connector-role-mapping`）：绑定带
`providerRole: true` 时，`provider` 是**角色名**，由客户在自己的连接器里选定实际
系统，未选定时报 `unmapped`——一个用户能自己关掉的状态。于是这道门改成认那个
显式开关，而不是靠"名字看起来不像厂商"来猜：

1. `providerRole: true` ⇒ provider 必须是 `ROLE_PROVIDERS` 里登记过的角色名
   （带理由与决策指针），且不得再写 `alternativeProviders`（映射是用户的选择，
   回退备选会让状态面与用户认知分叉）；
2. 没有 `providerRole` ⇒ provider（含 `alternativeProviders`）必须 ∈ 目录快照；
   角色名不再能靠"看起来不像 service id"蒙混过关；
3. `providerRole: true` 而 provider 恰好是真实 service id ⇒ 判红：那是把一个厂商
   写成了角色，客户会被要求"选一个系统"却只能选到它；
4. 本机有目录 checkout 时，快照必须与目录当前值逐值相等（CI 无 checkout 则跳过）。

非 optional 的角色绑定**允许**（这正是角色绑定的用途：必接但品牌不定的系统，由
客户选定后就地闭环），前提是通过第 1 条登记。
"""
from __future__ import annotations

import importlib.util
import re
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates"
SNAPSHOT = REPO / "tests" / "fixtures" / "connector-catalog-service-ids.json"
REFRESH = REPO / "scripts" / "refresh_connector_catalog_ids.py"

# 已登记的角色名：目录里没有同名 service id，声明方是业务角色，且**必须**由绑定上
# 的 `providerRole: true` 显式声明（见 paas add-connector-role-mapping）。
# 每项必须写清 reason（为什么是角色不是厂商）与 decision（谁来关掉这条登记）。
# 关掉的方式：声明方改回真 service id（并删掉这条登记），或该角色不再被任何包需要。
ROLE_PROVIDERS: dict[str, dict[str, str]] = {
    "wms": {
        "reason": "客户 WMS 品牌不定，一条声明无法写死厂商；目录里也没有同名 service id",
        "decision": "角色绑定（providerRole: true）：客户在连接器卡片选定自己的 WMS，未选定前报「待选系统」。目录新增覆盖写入的 WMS provider 后可考虑改真 id。见 docs/connector-onboarding-supply-chain.md §4.3/§4.7",
    },
    "supplier-portal": {
        "reason": "目录没有任何 SRM/供应商门户 provider，角色名无厂商可填",
        "decision": "角色绑定：客户选定自己的供应商门户系统。open-connector-mt 新增 SRM provider 后可改真 id；本仓不能自造。见同上文档 §4.3/§4.7",
    },
    "carrier-portal": {
        "reason": "承运商是多个厂商的集合，单条声明无法写死一家",
        "decision": "角色绑定：客户选定自己的承运商门户（目录有 sf_express / kuaidi100，可作该客户的选择之一）。见同上文档 §4.3/§4.7",
    },
    "pos": {
        "reason": "门店 POS 厂商逐客户不同，目录里没有任何 POS 收银 provider；餐饮包自己也写明「框架一个、厂商逐个插拔，不预设名单」",
        "decision": "角色绑定（餐饮连锁对账连接器包 pos-sales，2026-11-08）：客户在连接器卡片选定自己实际在用的 POS。未适配厂商走包内既有导出文件替代通道，故该绑定为 optional，不阻塞安装。目录新增 POS provider 后可考虑改真 id",
    },
    "cn-commerce": {
        "reason": "国内电商平台（淘宝/天猫/京东）在目录里没有 service id；且同一个客户可能只在其中一个平台开店，写成某个固定 id 就等于替客户选平台",
        "decision": "角色绑定（跨境电商对账连接器包 cn-commerce-store，2026-11-08）：客户选定自己实际在用的平台与账号。拼多多上游 schema 缺失，故不声明该绑定——声明了就是一条永远关不上的缺口",
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
            is_role = binding.get("providerRole") is True
            if is_role:
                # The explicit switch: the name is a role, so it must be a
                # registered one, must not be a real service id (that would ask
                # the customer to "choose a system" from a one-item list), and
                # must not carry alternatives (a mapped role has exactly one
                # answer; silently falling back would contradict the choice).
                if provider not in ROLE_PROVIDERS:
                    bad.append(f"{where}: {key} → provider {provider!r} 标为角色却未在 ROLE_PROVIDERS 登记")
                else:
                    seen_roles.add(provider)
                if provider in catalog:
                    bad.append(f"{where}: {key} → provider {provider!r} 是目录真实 service id，不该标成角色")
                if binding.get("alternativeProviders"):
                    bad.append(f"{where}: {key} → 角色绑定不得声明 alternativeProviders（映射由客户选定，没有回退）")
            elif provider not in catalog:
                # Without the switch the name is a service id, full stop: a role
                # name that merely "looks like" a business word is the old trap.
                bad.append(f"{where}: {key} → provider {provider!r} 不在目录，也未以 providerRole 声明为角色")
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


def test_role_name_without_the_explicit_flag_is_rejected():
    """角色名不能只靠"看起来像业务词"混进声明。

    历史：`wms-snapshot` 曾是 `provider: "wms"` 且没有 `providerRole`，这条声明在
    paas 侧只能被当作一个不存在的 service id 去问目录，结果是**永久** `unwired`——
    客户接什么账号都关不上。2026-11-08 起它带 `providerRole: true`，那是 paas 认的
    唯一开关；本用例把"开关丢了"变成一条点名报错，而不是退回老语义静默通过。
    """
    violations = [
        f"{where}: {b.get('key')}（provider={b.get('provider')}）— 角色名缺 providerRole: true"
        for where, bindings in _connector_packs()
        for b in bindings
        if b.get("provider") in ROLE_PROVIDERS and b.get("providerRole") is not True
    ]
    assert not violations, (
        "登记为角色名的 provider 必须以 providerRole: true 显式声明：\n  " + "\n  ".join(violations)
    )


def test_non_optional_role_binding_is_allowed_and_the_fix_is_choosing_a_system():
    """非 optional 的角色绑定是允许的——这正是角色绑定的用途。

    旧门要求「角色名只能出现在 optional 绑定上」，理由是那时角色名永远满足不了，
    非 optional 就等于给用户挂一条他处理不了的项。角色绑定改变了这个事实：客户
    在连接器卡片选定自己的系统后该绑定就满足，未选定报 `unmapped`（可关闭、不阻塞
    安装）。所以现在只要求带开关并登记，不要求 optional。
    """
    role_bindings = [
        (where, b)
        for where, bindings in _connector_packs()
        for b in bindings
        if b.get("providerRole") is True
    ]
    assert role_bindings, "没有任何角色绑定——这条规则就失去意义，检查上面的开关是否被误删"
    for where, binding in role_bindings:
        assert binding.get("optional") is True or binding.get("optional") is False, (
            f"{where}: {binding.get('key')} 的 optional 必须是显式布尔（省略即被读成非必接）"
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


# ── 受控词表（docs/connector-role-vocabulary.md） ─────────────────────────────
#
# 角色名是跨包的公共词汇：同一个角色在餐饮包里叫 pos、在下一个行业包里也必须叫 pos。
# 但代码里没有任何东西阻止下一个包自创 `pos-system` / `cn-ecom` 这类近义词——词表会慢慢
# 散掉，而散掉的代价由客户承担（同一件事回答两次）。所以词表做成两处联动的门：
# 登记表（ROLE_PROVIDERS）+ 文档表（本文档的 `| `role` | …` 行），漏一处就红。

VOCAB_DOC = REPO / "docs" / "connector-role-vocabulary.md"
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def _documented_roles() -> set[str]:
    """文档表里登记的角色名（`| `pos` | …` 形式的第一列）。"""
    text = VOCAB_DOC.read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|", text, flags=re.MULTILINE))


def test_role_names_are_kebab_case():
    bad = [name for name in ROLE_PROVIDERS if not ROLE_NAME_RE.match(name)]
    assert not bad, f"角色名必须是全小写 kebab-case（{ROLE_NAME_RE.pattern}）：{bad}"


def test_role_names_do_not_collide_with_catalog_ids():
    """角色名不得等于目录里真实存在的 service id。

    现在不冲突不代表以后不冲突：目录新增同名 provider 时，这条会红，逼我们在
    「改角色名」与「改真 id」之间做决定，而不是让同一个字符串有两种含义。
    """
    collided = sorted(set(ROLE_PROVIDERS) & _service_ids())
    assert not collided, f"角色名与目录 service id 撞名，必须二选一改名：{collided}"


def test_role_vocabulary_document_matches_the_registry():
    """文档表与登记表必须逐项一致——新增角色两处都改，删除同理。"""
    documented = _documented_roles()
    registered = set(ROLE_PROVIDERS)
    assert documented - registered == set(), (
        f"词表文档里有登记表没有的角色（要么补登记，要么删行）：{sorted(documented - registered)}"
    )
    assert registered - documented == set(), (
        f"登记表里有词表文档没有的角色（新词必须写进 {VOCAB_DOC.name}）：{sorted(registered - documented)}"
    )


# ── 表面级对位（docs/connector-binding-surfaces.md） ──────────────────────────
#
# 为什么光有 provider 存在性门不够：paas 的覆盖检查是**类级**的（read/write），
# 所以 provider 只要有任意一个 read 动作，声明"只读"的绑定就会被判 satisfied 并授权。
# 2026-11-08 实测抓到两例，都是我们自己的包：
#   · `provider: meituan` 的"外卖结算" —— 目录里 meituan 唯一的 read 是 query_travel（酒旅）；
#   · `provider: shopify` 的"订单/收款/库存" —— 目录里 shopify 的 13 个 read 全是博客/页面，
#     订单库存在 shopify_admin 里，而 payouts 一个动作都没有。
# 两例都会"绿灯授权一个错的面"。所以固定 provider 的绑定必须在
# docs/connector-binding-surfaces.md 里写清"哪个动作承担这个面"，并被这条门逐一核对：
# 文档里点名的动作必须真的存在于该 provider 的目录清单里（角色绑定不需要，客户自己选）。

SURFACES_DOC = REPO / "docs" / "connector-binding-surfaces.md"
CATALOG_APPS = REPO.parent / "open-connector-mt" / "catalog" / "apps"
ACTIONS_SNAPSHOT = REPO / "tests" / "fixtures" / "connector-binding-actions.json"


def _catalog_actions(service: str) -> set[str] | None:
    """该 provider 的动作名集合。

    优先读本机 `open-connector-mt` checkout（最新），没有就读快照
    `tests/fixtures/connector-binding-actions.json`——CI 没有那个 checkout，而这道门
    正是最该在 CI 里跑的一道（"类级绿灯、面不对"的错就是它抓的）。两者都没有才返回 None。
    """
    path = CATALOG_APPS / f"{service}.json"
    if path.exists():
        body = json.loads(path.read_text(encoding="utf-8"))
        return {str(action.get("name")) for action in body.get("actions") or [] if action.get("name")}
    if ACTIONS_SNAPSHOT.exists():
        body = json.loads(ACTIONS_SNAPSHOT.read_text(encoding="utf-8"))
        actions = (body.get("providers") or {}).get(service)
        if actions is not None:
            return {str(action.get("name")) for action in actions if action.get("name")}
    return None


def _surface_rows() -> dict[tuple[str, str], list[str]]:
    """把对位表读成 {(包目录, 绑定 key): [动作名…]}。"""
    text = SURFACES_DOC.read_text(encoding="utf-8")
    rows: dict[tuple[str, str], list[str]] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].startswith("`"):
            continue
        pack_dir = cells[0].strip("`")
        key = cells[1].strip("`")
        actions = [a.strip().strip("`") for a in cells[3].split(",") if a.strip() and a.strip() != "—"]
        rows[(pack_dir, key)] = actions
    return rows


def test_fixed_provider_bindings_have_a_documented_surface_mapping():
    """每条固定 provider 的绑定都必须在对位表里点名承担它的目录动作。"""
    documented = _surface_rows()
    missing: list[str] = []
    for root in sorted((REPO / "templates").iterdir()):
        if not root.is_dir():
            continue
        pack_path = root / "connector-pack" / "pack.json"
        if not pack_path.exists():
            continue
        body = json.loads(pack_path.read_text(encoding="utf-8"))
        for binding in body.get("connector_bindings") or []:
            if binding.get("providerRole") is True:
                continue  # 角色绑定的面由客户选的系统决定
            entry = (root.name, str(binding.get("key")))
            actions = documented.get(entry)
            if not actions:
                missing.append(f"{root.name}/{binding.get('key')}（provider={binding.get('provider')}）")
    assert not missing, (
        "固定 provider 的绑定缺少面级对位（类级覆盖绿灯不代表面能用，"
        f"在 {SURFACES_DOC.name} 里写清承担它的动作）：\n  " + "\n  ".join(missing)
    )


def test_documented_actions_really_exist_in_the_catalog():
    """对位表里点名的动作必须真的存在于该 provider 的目录清单里。

    这条是把"我以为 meituan 能做外卖结算"这类错误挡在发布之前：没有目录 checkout 时跳过
    （CI 无 checkout），本地/出片前必跑。
    """
    if not CATALOG_APPS.is_dir() and not ACTIONS_SNAPSHOT.exists():
        pytest.skip("既无 open-connector-mt checkout 也无动作集快照——先跑 scripts/refresh_connector_catalog_ids.py")
    bad: list[str] = []
    for root in sorted((REPO / "templates").iterdir()):
        pack_path = root / "connector-pack" / "pack.json"
        if not pack_path.exists():
            continue
        body = json.loads(pack_path.read_text(encoding="utf-8"))
        by_key = {str(b.get("key")): b for b in body.get("connector_bindings") or []}
        for (pack_dir, key), actions in _surface_rows().items():
            if pack_dir != root.name or key not in by_key:
                continue
            binding = by_key[key]
            if binding.get("providerRole") is True:
                continue
            if not actions:
                bad.append(f"{pack_dir}/{key}: 对位表里没有动作（面无法被验证）")
                continue
            known = _catalog_actions(str(binding.get("provider")))
            if known is None:
                continue  # 本机缺该 provider 的目录文件：不是本门的判据
            unknown = sorted(set(actions) - known)
            if unknown:
                bad.append(
                    f"{pack_dir}/{key}: 对位表点了 {unknown}，但目录里 {binding.get('provider')} 没有这些动作"
                    f"（现有: {sorted(known)[:6]}…）"
                )
    assert not bad, "面级对位与目录不一致：\n  " + "\n  ".join(bad)

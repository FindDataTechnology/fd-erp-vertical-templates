"""能力矩阵 1.x：结构、初版事实、降级规则。

判据纪律（本仓既有教训）：断言要打在**坏的那个东西本身**上。所以
`test_initial_matrix_has_no_available` 断言的是「没有一格够格」，而不是
「读到了 28 格」——后者在错误填表时照样绿。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability import (
    EVIDENCE_PROVIDER_ACTION,
    EVIDENCE_RUNTIME_READ,
    MATRIX_SCHEMA,
    STATUS_AVAILABLE,
    STATUS_UNAVAILABLE,
    STATUS_UNMAPPED,
    load_matrix,
    review_due_unavailable,
)
from fd_erp_vertical_templates.errors import TemplateError

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
MATRIX = TEMPLATES / "crossborder" / "capability-matrix.json"
SCHEMA_FILE = (Path(__file__).resolve().parent.parent
               / "fd_erp_vertical_templates" / "schemas" / "capability-matrix.schema.json")


def _cell(status: str = STATUS_UNAVAILABLE, **over) -> dict:
    base = {"status": status, "channel": "x.y", "evidence": [], "reason": "why"}
    base.update(over)
    return base


def _matrix(flows: dict, *, platform: str = "p1") -> dict:
    return {
        "schema": MATRIX_SCHEMA,
        "vertical": "crossborder",
        "updated_at": "2026-11-10",
        "platforms": [{"key": platform, "flows": flows}],
    }


def test_schema_file_matches_code_schema_constant():
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == MATRIX_SCHEMA
    # 三态与四流是契约：任一侧加值必须两边同改
    cell = schema["$defs"]["cell"]["properties"]["status"]["enum"]
    assert cell == ["available", "unavailable", "unmapped"]
    flows = schema["properties"]["platforms"]["items"]["properties"]["flows"][
        "propertyNames"]["enum"]
    assert flows == ["orders", "settlements", "inventory", "refunds"]


def test_matrix_loads_all_declared_platforms():
    """平台集合从矩阵自身派生，不写死个数。

    原先写死「恰好 7 个平台 / 28 格」——自有渠道（own_channel）加入后失效。
    该断言的是「矩阵里声明的平台与它的 flows 一一对上」，不是「永远 7 个」。
    """
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    assert m.vertical == "crossborder"
    raw = json.loads(MATRIX.read_text(encoding="utf-8"))
    declared = {p["key"] for p in raw["platforms"]}
    assert set(m.platform_keys()) == declared
    expected_cells = sum(len(p["flows"]) for p in raw["platforms"])
    assert len(m.cells) == expected_cells


def test_matrix_cell_counts_are_partitioned():
    """格子状态三态互斥且覆盖全部格子。

    原先断言「available 恒为 0」「unavailable 恒为 4」——那是 2026-11-10 的基线，
    有格转 available 后就失效。断言「三态之和 = 总格数」才是这条测试该守的东西：
    它抓的是「某格状态丢失 / 计两次」，不是「永远没有可用格」。
    """
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    s = m.summary()
    assert s[STATUS_AVAILABLE] + s[STATUS_UNAVAILABLE] + s[STATUS_UNMAPPED] == len(m.cells)
    # 拼多多四格是 unavailable（上游 schema 缺失），这条是业务事实，保持不变
    pd = [c for (p, _f), c in m.cells.items() if p == "pinduoduo"]
    assert all(c.status != STATUS_AVAILABLE for c in pd)


def test_every_non_available_cell_explains_itself():
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    for (p, f), cell in m.cells.items():
        if cell.status != STATUS_AVAILABLE:
            assert cell.reason.strip(), f"{p}.{f} 缺 reason：对外无法解释"


def test_pinduoduo_is_unavailable_not_unmapped():
    """「确认拿不到」与「还没接」不能混：混了就会对客户说错方向。"""
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    for flow in ("orders", "settlements", "inventory", "refunds"):
        assert m.status("pinduoduo", flow) == STATUS_UNAVAILABLE
    assert m.status("taobao", "orders") == STATUS_UNMAPPED


def test_partial_flow_declaration_reports_undeclared_legs_as_missing():
    """只声明部分流时，recon_shape 只数已声明的可用腿，未声明的按缺腿报。

    这条防的是「没声明 = 没这一腿」被读成「没声明 = 没问题」：如果 recon_shape
    按固定的三腿减法、而缺腿只看已声明项，一个只声明 orders 的平台会被算成
    「已完整」（0 缺腿），实际结算与库存两腿根本不存在。
    """
    body = _matrix({"orders": _cell(STATUS_AVAILABLE, measured_at="2026-11-09",
                                    evidence=[{"kind": EVIDENCE_PROVIDER_ACTION, "ref": "a"},
                                              {"kind": EVIDENCE_RUNTIME_READ, "ref": "b"}])})
    m = load_matrix(body, today=date(2026, 11, 10))
    assert m.recon_shape("p1") == 1
    missing = {d["flow"] for d in m.missing_legs("p1")}
    assert missing == {"settlements", "inventory"}, missing


def test_recon_shape_is_zero_when_nothing_is_wired():
    """用**合成**矩阵守这条不变量，不读仓内真实矩阵。

    2026-11-25 修正：此前后者，断言「所有平台 recon_shape 都是 0」——那是把当时的状态
    写死成断言。shopify 真接通 orders/inventory 之后它就红了，而代码没坏。判据应问
    「没接线的矩阵 recon_shape 是否为 0」，不是「今天恰好还没接线」。
    """
    m = load_matrix(_matrix({f: _cell(STATUS_UNAVAILABLE)
                             for f in ("orders", "settlements", "inventory", "refunds")}),
                    today=date(2026, 11, 10))
    for platform in m.platform_keys():
        assert m.recon_shape(platform) == 0
    missing = m.missing_legs("p1")
    assert {d["flow"] for d in missing} == {"orders", "settlements", "inventory"}
    assert all(d["kind"] == "capability_missing" for d in missing)
    assert all(d["matrix_ref"].startswith("p1.") for d in missing)


# ---------------------------------------------------------------- 降级规则


def test_available_requires_provider_action_evidence():
    body = _matrix({"orders": _cell(STATUS_AVAILABLE, measured_at="2026-11-09",
                                    evidence=[{"kind": EVIDENCE_RUNTIME_READ,
                                               "ref": "run-1"}])})
    cell = load_matrix(body, today=date(2026, 11, 10)).cell("p1", "orders")
    assert cell.status == STATUS_UNMAPPED
    assert cell.downgraded_from == STATUS_AVAILABLE
    assert "provider_action" in cell.reason


def test_available_requires_runtime_read_evidence():
    body = _matrix({"orders": _cell(STATUS_AVAILABLE, measured_at="2026-11-09",
                                    evidence=[{"kind": EVIDENCE_PROVIDER_ACTION,
                                               "ref": "actions.ts:1"}])})
    cell = load_matrix(body, today=date(2026, 11, 10)).cell("p1", "orders")
    assert cell.status == STATUS_UNMAPPED
    assert "真实读取" in cell.reason


def test_available_requires_measured_at():
    body = _matrix({"orders": _cell(STATUS_AVAILABLE, measured_at=None,
                                    evidence=[{"kind": EVIDENCE_PROVIDER_ACTION, "ref": "a"},
                                              {"kind": EVIDENCE_RUNTIME_READ, "ref": "b"}])})
    cell = load_matrix(body, today=date(2026, 11, 10)).cell("p1", "orders")
    assert cell.status == STATUS_UNMAPPED
    assert "测量时点" in cell.reason


def test_available_expires_after_review_window():
    stale = (date(2026, 11, 10) - timedelta(days=91)).isoformat()
    body = _matrix({"orders": _cell(STATUS_AVAILABLE, measured_at=stale,
                                    review_window_days=90,
                                    evidence=[{"kind": EVIDENCE_PROVIDER_ACTION, "ref": "a"},
                                              {"kind": EVIDENCE_RUNTIME_READ, "ref": "b"}])})
    cell = load_matrix(body, today=date(2026, 11, 10)).cell("p1", "orders")
    assert cell.status == STATUS_UNMAPPED
    assert "复核窗" in cell.reason


def test_available_survives_when_evidence_complete_and_fresh():
    body = _matrix({"orders": _cell(STATUS_AVAILABLE, measured_at="2026-11-09",
                                    review_window_days=90,
                                    evidence=[{"kind": EVIDENCE_PROVIDER_ACTION, "ref": "a"},
                                              {"kind": EVIDENCE_RUNTIME_READ, "ref": "b"}])})
    m = load_matrix(body, today=date(2026, 11, 10))
    assert m.cell("p1", "orders").available
    assert m.recon_shape("p1") == 1


# ---------------------------------------------------------------- 结构校验


def test_missing_reason_on_unmapped_is_rejected():
    body = _matrix({"orders": _cell(STATUS_UNMAPPED, reason="")})
    with pytest.raises(TemplateError) as ei:
        load_matrix(body)
    assert ei.value.code == "MATRIX_INVALID"
    assert "reason" in ei.value.message


def test_unknown_flow_is_rejected():
    body = _matrix({"payouts": _cell()})
    with pytest.raises(TemplateError) as ei:
        load_matrix(body)
    assert "流名非法" in ei.value.message


def test_unknown_status_is_rejected():
    body = _matrix({"orders": _cell("maybe")})
    with pytest.raises(TemplateError) as ei:
        load_matrix(body)
    assert ei.value.code == "MATRIX_INVALID"


def test_missing_cell_raises_not_found_not_false():
    """缺格必须报出来，不能默认可用（读不到≠不存在）。"""
    body = _matrix({"orders": _cell()})
    m = load_matrix(body)
    with pytest.raises(TemplateError) as ei:
        m.status("p1", "inventory")
    assert ei.value.code == "NOT_FOUND"


def test_fingerprint_ignores_updated_at():
    a = load_matrix(MATRIX, today=date(2026, 11, 10)).fingerprint()
    raw = json.loads(MATRIX.read_text(encoding="utf-8"))
    raw["updated_at"] = "2026-12-31"
    b = load_matrix(raw, today=date(2026, 11, 10)).fingerprint()
    assert a == b
    assert a.startswith("sha256:")


def test_review_due_lists_stale_unavailable():
    old = (date(2026, 11, 10) - timedelta(days=400)).isoformat()
    body = _matrix({
        "orders": _cell(STATUS_UNAVAILABLE, measured_at=old, review_window_days=365),
        "inventory": _cell(STATUS_UNAVAILABLE, measured_at="2026-11-01"),
    })
    m = load_matrix(body, today=date(2026, 11, 10))
    due = review_due_unavailable(m, today=date(2026, 11, 10))
    assert [d["flow"] for d in due] == ["orders"]
    assert "复核窗" in due[0]["basis"]


# ---------------------------------------------------------------- 升格机制（正向）

def test_upgrades_to_available_only_with_both_evidence_halves():
    """升格的正向路径：provider_action + runtime_read + 新鲜测量时点 → available。

    此前只测过「降级」，「什么情况才该升」没有正向断言——那会让规则半边失守：
    只剩降级逻辑时，任何格子永远不可能变绿，也就没人发现升格条件写错了。
    """
    body = _matrix({"orders": _cell(
        STATUS_AVAILABLE, measured_at="2026-11-09", review_window_days=90,
        evidence=[{"kind": EVIDENCE_PROVIDER_ACTION, "ref": "actions.ts:816"},
                  {"kind": EVIDENCE_RUNTIME_READ, "ref": "run-2026-11-09"}])})
    m = load_matrix(body, today=date(2026, 11, 10))
    cell = m.cell("p1", "orders")
    assert cell.available, cell.reason
    assert cell.downgraded_from is None
    assert m.recon_shape("p1") == 1
    assert m.missing_legs("p1") == [
        d for d in m.missing_legs("p1") if d["flow"] != "orders"]


def test_upgrade_requires_both_halves_even_when_fresh():
    """新鲜度够、但只有一半证据 → 仍不升格（两条正交条件）。"""
    fresh = "2026-11-09"
    only_action = _matrix({"orders": _cell(
        STATUS_AVAILABLE, measured_at=fresh,
        evidence=[{"kind": EVIDENCE_PROVIDER_ACTION, "ref": "a"}])})
    assert not load_matrix(only_action, today=date(2026, 11, 10)).cell(
        "p1", "orders").available

    only_read = _matrix({"orders": _cell(
        STATUS_AVAILABLE, measured_at=fresh,
        evidence=[{"kind": EVIDENCE_RUNTIME_READ, "ref": "b"}])})
    assert not load_matrix(only_read, today=date(2026, 11, 10)).cell(
        "p1", "orders").available


def test_runtime_smoke_alone_does_not_upgrade():
    """接线成功的冒烟（runtime_read=接线）不等于读到数据——settlements 就是这种状态。

    真实状态锚点：shopify.settlements 有 provider_action + runtime_read(接线)，
    但仍必须是 unmapped，因为「动作能调用」与「读到真实数据」是两件事。
    """
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    cell = m.cell("shopify", "settlements")
    kinds = {e.kind for e in cell.evidence}
    assert EVIDENCE_PROVIDER_ACTION in kinds
    assert EVIDENCE_RUNTIME_READ in kinds, "接线冒烟应作为 runtime_read 记录在案"
    assert not cell.available, (
        "接线冒烟就升格了 available——真实数据还没读到，这条会骗客户")


# ---------------------------------------------------------------- schema 漂移

def test_schema_declares_every_top_level_key_in_the_matrix():
    """schema 必须声明矩阵实际出现的每个顶层键。

    本轮真实踩到：给矩阵加了 `version` 字段（发布门要按它判版本单调），
    但 schema 文件没跟上——`additionalProperties: true` 让校验照样绿，
    于是「文档契约」静默落后于事实。
    """
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    declared = set(schema["properties"])
    actual = set(json.loads(MATRIX.read_text(encoding="utf-8")))
    missing = actual - declared
    assert not missing, (
        f"矩阵有 schema 未声明的顶层键：{sorted(missing)}——"
        "schema 是互操作契约，落后于实际文件即漂移")


def test_matrix_version_matches_manifest_declaration():
    """矩阵 version 与 manifest 的 capability_matrix.version 必须一致（发布门按它判单调）。"""
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    manifest = json.loads((TEMPLATE_DIR := MATRIX.parent / "manifest.json")
                          .read_text(encoding="utf-8"))
    declared = manifest["assets"]["capability_matrix"]["version"]
    assert matrix.get("version") == declared, (
        f"矩阵 version={matrix.get('version')} 与清单声明 {declared} 不一致")


# ---------------------------------------------------------------- 死代码防回归

def test_no_unused_public_helpers_in_capability():
    """能力矩阵模块的公开 helper 必须真的有人用。

    本会话真实清掉过一个 `unavailable_flows`（与 missing_legs 功能重叠、无调用方）
    和两个从未被调用的 flows 查询。交付物里的死代码就是噪声——更难的是它会被
    当成「有能力的证据」：读者看到有个 available_flows 就以为有人靠它做决定。
    """
    import ast as _ast
    import pathlib as _pathlib

    repo = _pathlib.Path(__file__).resolve().parent.parent
    # 排除 .venv/.git 等：否则「有没有安装过」会改变判据——门自己先得了它要治的病
    # （本会话真实踩到：本地因 .venv 里有副本而假绿，干净检出才暴露 to_dict 是死代码）。
    skip_dirs = (".venv", ".git", "graphify-out", "__pycache__", "node_modules")
    src_all = "".join(
        p.read_text(encoding="utf-8")
        for p in repo.rglob("*.py")
        if not any(f"/{d}/" in str(p) for d in skip_dirs))
    tree = _ast.parse((repo / "fd_erp_vertical_templates" / "capability.py")
                      .read_text(encoding="utf-8"))

    dead: list[str] = []
    for cls in [n for n in tree.body if isinstance(n, _ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, _ast.FunctionDef)]:
            if fn.name.startswith("__"):
                continue
            # 属性不算；私有也算进来（私有死代码同样是噪声）
            if any("property" in _ast.dump(d) for d in fn.decorator_list):
                continue
            if src_all.count(fn.name) <= 1:
                dead.append(f"{cls.name}.{fn.name}")
    assert not dead, f"capability.py 有无人调用的方法：{dead}"

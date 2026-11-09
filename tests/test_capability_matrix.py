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


def test_initial_matrix_loads_all_seven_platforms():
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    assert m.vertical == "crossborder"
    assert set(m.platform_keys()) == {
        "shopify", "tiktok_shop", "taobao", "tmall", "jd", "merchant_erp", "pinduoduo"}
    assert len(m.cells) == 7 * 4


def test_initial_matrix_has_no_available_cell():
    """改造前的真实基线：内圈没有一格够格 available。"""
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    assert m.summary()[STATUS_AVAILABLE] == 0, m.summary()
    assert m.summary()[STATUS_UNAVAILABLE] == 4      # 拼多多四条流
    assert m.summary()[STATUS_UNMAPPED] == 7 * 4 - 4


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


def test_recon_shape_is_zero_before_wiring():
    m = load_matrix(MATRIX, today=date(2026, 11, 10))
    for platform in m.platform_keys():
        assert m.recon_shape(platform) == 0
    missing = m.missing_legs("shopify")
    assert {d["flow"] for d in missing} == {"orders", "settlements", "inventory"}
    assert all(d["kind"] == "capability_missing" for d in missing)
    assert all(d["matrix_ref"].startswith("shopify.") for d in missing)


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

"""矩阵 → 产物一致性（change 任务 2.3 / 5.2）。

门要打在**漂移本身**上：矩阵改了而产物没重生成、preset 声明了不可得的腿、
pack 描述还写着旧口径——三种都判红。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability import load_matrix
from fd_erp_vertical_templates.reports import load_report_preset

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "crossborder"
MATRIX = TEMPLATE / "capability-matrix.json"
PRESET = TEMPLATE / "report-preset" / "preset.json"
PACK = TEMPLATE / "connector-pack" / "pack.json"


def _matrix():
    return load_matrix(MATRIX, today=date(2026, 11, 10))


def test_preset_and_pack_are_in_sync_with_matrix():
    fp = _matrix().fingerprint()
    preset = json.loads(PRESET.read_text(encoding="utf-8"))
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    assert preset["_generated_from"]["fingerprint"] == fp, \
        "preset 落后于矩阵：重跑 scripts/gen_capability_artifacts.py"
    assert pack["_generated_from"]["fingerprint"] == fp, \
        "connector pack 落后于矩阵：重跑 scripts/gen_capability_artifacts.py"


def test_pack_description_has_no_unbacked_availability_claim():
    """「已实测可用」这类没有载体的说法不得出现在生成产物里。"""
    text = PACK.read_text(encoding="utf-8")
    for banned in ("已实测可用", "实测可用", "supports all", "均已支持"):
        assert banned not in text, f"pack 描述含无载体声明：{banned}"


def test_pack_capability_statement_matches_matrix_statuses():
    m = _matrix()
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    for platform, info in pack["capability"]["platforms"].items():
        for flow, cell in info["flows"].items():
            expected = m.status(platform, flow)
            assert cell["status"] == expected, f"{platform}.{flow} 与矩阵不一致"


def test_preset_legs_are_bound_to_matrix_flows():
    m = _matrix()
    preset = load_report_preset(PRESET)
    bound = [sec for rep in preset["reports"]
             for sec in rep.get("sections", []) if sec.get("leg_flow")]
    assert bound, "preset 里没有任何腿绑定矩阵——派生关系断了"
    for sec in bound:
        assert sec["availability_source"] == "capability-matrix"
        assert sec["leg_flow"] in ("orders", "settlements", "inventory")
    # 每条绑定的腿都能在矩阵里找到至少一个平台条目（否则是孤儿声明）
    for sec in bound:
        assert any((p, sec["leg_flow"]) in m.cells for p in m.platform_keys())


def test_all_unavailable_legs_carry_a_reason_template():
    """当前没有任何平台能供的腿，必须带统一原因；不能留着让人以为是好数据。"""
    preset = load_report_preset(PRESET)
    legs = preset["leg_capability"]
    for rep in preset["reports"]:
        for sec in rep.get("sections", []):
            leg = sec.get("key")
            if leg in legs and not legs[leg]["supplied_by_any"]:
                assert sec.get("unavailable_reason_template"), \
                    f"{leg} 无通道可供，但缺 unavailable_reason_template"


def test_resolve_sections_keeps_position_and_drops_metrics():
    """运行期解析：不可得腿保留位置、标原因、清空 metrics（防 0 值渲染）。"""
    m = _matrix()
    sections = [{"key": "orders", "label_zh": "订单流", "metrics": ["a", "b"],
                 "leg_flow": "orders"},
                {"key": "diffs", "label_zh": "差异", "metrics": ["c"]}]
    out = m.resolve_sections(sections, "shopify")
    assert [s["key"] for s in out] == ["orders", "diffs"]
    assert out[0]["availability"]["status"] == "unmapped"
    assert out[0]["metrics"] == []
    assert out[0]["unavailable_reason"]
    assert out[1]["availability"]["status"] == "available"


def test_available_leg_keeps_metrics():
    m = load_matrix({
        "schema": "ecommerce-capability-matrix/v1",
        "vertical": "crossborder",
        "platforms": [{"key": "p", "flows": {"orders": {
            "status": "available", "channel": "a", "reason": "",
            "measured_at": "2026-11-09",
            "evidence": [{"kind": "provider_action", "ref": "a"},
                         {"kind": "runtime_read", "ref": "b"}]}}}],
    }, today=date(2026, 11, 10))
    out = m.resolve_sections([{"key": "orders", "metrics": ["x"],
                               "leg_flow": "orders"}], "p")
    assert out[0]["metrics"] == ["x"]
    assert out[0]["availability"]["status"] == "available"


def test_review_pending_lists_every_unmapped_cell():
    m = _matrix()
    pending = m.review_pending()
    assert len(pending) == m.summary()["unmapped"]
    for row in pending:
        assert row["reason"]
        assert row["next_action"]

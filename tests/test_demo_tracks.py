"""演示双轨（A 任务 6.2）。

两条判据：
1. 两轨都能跑，且各自「真实矩阵」那一遍把缺腿说清楚（不伪装成已接通）；
2. 「假设已接通」那一遍只改内存里的矩阵，**不动磁盘上的矩阵指纹**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability import load_matrix, matrix_path_for
from fd_erp_vertical_templates.demo_tracks import (
    TRACKS,
    assumed_matrix,
    load_domestic_fixture,
    run_track,
)

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "crossborder"


def test_tracks_cover_both_merchant_paths():
    assert TRACKS == ("shopify", "domestic")


def test_domestic_fixture_loads_full_and_missing_variants():
    full = load_domestic_fixture(TEMPLATE)
    assert len(full["orders"]) == 3 and len(full["settlements"]) == 3
    missing = load_domestic_fixture(TEMPLATE, "settlements_missing")
    assert missing["settlements"] == []
    assert missing["dropped"] == ["settlements"]
    with pytest.raises(KeyError):
        load_domestic_fixture(TEMPLATE, "no_such_variant")


def test_domestic_track_reports_honest_missing_then_assumed_shape():
    out = run_track("domestic", template_dir=TEMPLATE)
    assert out["track"] == "domestic" and out["platform"] == "merchant_erp"
    # 真实矩阵：一条腿都没接通
    assert out["honest"]["shape"] == 0
    assert {m["flow"] for m in out["honest"]["capability_missing"]} == {
        "orders", "settlements", "inventory"}
    # 假设接通：三方成立
    assert out["assumed"]["shape"] == 3
    assert out["assumed"]["capability_missing"] == []
    assert "非矩阵事实" in out["assumed"]["note"]


def test_domestic_missing_variant_keeps_settlements_gap_under_assumption():
    """缺结算变体即使假设「已接通」，结算腿仍不可得——变体丢的是数据不是能力。

    这一条防的是「演示脚本自己把缺腿补上」这种最容易被客户当场戳穿的错。
    """
    out = run_track("domestic", template_dir=TEMPLATE,
                    variant="settlements_missing")
    assert out["assumed"]["shape"] == 2
    assert [m["flow"] for m in out["assumed"]["capability_missing"]] == ["settlements"]


def test_shopify_track_with_caller_records():
    records = {
        "store": "demo-shop",
        "orders": [{"id": "o1", "amount": "250", "currency": "USD",
                    "status": "paid", "occurred_at": "2026-11-06", "link": "o1"}],
        "settlements": [{"id": "p1", "amount": "240", "currency": "USD",
                         "status": "paid", "occurred_at": "2026-11-06",
                         "link": "o1", "fees": {"commission": "15"}}],
        "inventory": [{"id": "i1", "amount": "0", "status": "deducted",
                       "occurred_at": "2026-11-06", "link": "o1"}],
    }
    out = run_track("shopify", template_dir=TEMPLATE, records=records)
    assert out["honest"]["shape"] == 0
    assert out["assumed"]["shape"] == 3
    assert "amount_mismatch" in out["assumed"]["diff_kinds"]


def test_assumed_matrix_does_not_touch_disk():
    before = matrix_path_for(TEMPLATE).read_text(encoding="utf-8")
    fp_before = load_matrix(matrix_path_for(TEMPLATE)).fingerprint()
    m = assumed_matrix(load_matrix(matrix_path_for(TEMPLATE)), "merchant_erp")
    assert m.recon_shape("merchant_erp") == 3
    after = matrix_path_for(TEMPLATE).read_text(encoding="utf-8")
    assert before == after
    assert load_matrix(matrix_path_for(TEMPLATE)).fingerprint() == fp_before


def test_unknown_track_is_rejected():
    with pytest.raises(ValueError):
        run_track("taobao", template_dir=TEMPLATE)

"""对外表述一致性门（change 任务 6.1-6.3）。

负例必须真的红——一个永远绿的门等于没有门。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability_check import check_template, render_lint

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "crossborder"
WORKSPACE = REPO.parent

DOCS = [
    WORKSPACE / "docs" / "crossborder-supply-map.md",
    WORKSPACE / "docs" / "presales-yichang" / "bossai.md",
    WORKSPACE / "fd-erp" / "collateral" / "demo-guide-crossborder.md",
    WORKSPACE / "collateral-solution" / "00-方案说明.md",
]


@pytest.mark.skipif(
    not all(p.is_file() for p in DOCS),
    reason="finddata 根的 docs/ 与 collateral-* 不在位（CI 单仓 checkout，根不是 git 仓）："
           "锚点对位需要真文档，机制用例见下方 tmp_path 系列")
def test_repo_docs_agree_with_matrix():
    result = check_template(TEMPLATE, today=date(2026, 11, 10), doc_paths=DOCS)
    assert result["ok"], render_lint(result)
    anchors = next(c for c in result["checks"] if c["name"] == "doc-anchors")
    assert "19 个锚点" in anchors["detail"]


def test_anchor_claiming_available_is_red(tmp_path):
    doc = tmp_path / "claim.md"
    doc.write_text("| 店铺经营 | <!-- capability:shopify.orders=available --> |",
                   encoding="utf-8")
    result = check_template(TEMPLATE, today=date(2026, 11, 10), doc_paths=[doc])
    assert not result["ok"]
    kinds = [c["kind"] for c in result["conflicts"]]
    assert kinds == ["anchor-drift"]
    c = result["conflicts"][0]
    assert c["expected"] == "unmapped" and c["found"] == "available"
    assert c["line"] == 1
    assert "矩阵为准" in c["fix"]


def test_anchor_on_missing_cell_is_red(tmp_path):
    doc = tmp_path / "typo.md"
    doc.write_text("<!-- capability:pindoudou.orders=unavailable -->", encoding="utf-8")
    result = check_template(TEMPLATE, today=date(2026, 11, 10), doc_paths=[doc])
    assert not result["ok"]
    assert result["conflicts"][0]["kind"] == "anchor-unknown-cell"
    assert result["conflicts"][0]["cell"] == "pindoudou.orders"


def test_bad_anchor_status_is_red(tmp_path):
    doc = tmp_path / "bad.md"
    doc.write_text("<!-- capability:shopify.orders=probably -->", encoding="utf-8")
    result = check_template(TEMPLATE, today=date(2026, 11, 10), doc_paths=[doc])
    assert not result["ok"]
    assert result["conflicts"][0]["kind"] == "anchor-bad-status"


def test_missing_doc_is_skipped_not_failed():
    result = check_template(TEMPLATE, today=date(2026, 11, 10),
                            doc_paths=[Path("/nonexistent/x.md")])
    assert result["ok"]


def test_artifact_drift_is_red(tmp_path):
    """产物落后于矩阵（改了矩阵没重生成）→ 红，并给出修法。"""
    import json
    import shutil

    tpl = tmp_path / "crossborder"
    shutil.copytree(TEMPLATE, tpl)
    preset = tpl / "report-preset" / "preset.json"
    body = json.loads(preset.read_text(encoding="utf-8"))
    body["_generated_from"]["fingerprint"] = "sha256:stale"
    preset.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    result = check_template(tpl, today=date(2026, 11, 10), doc_paths=[])
    assert not result["ok"]
    drift = [c for c in result["conflicts"] if c["kind"] == "artifact-drift"]
    assert drift and drift[0]["artifact"] == "report-preset/preset.json"
    assert "gen_capability_artifacts" in drift[0]["fix"]


def test_review_pending_covers_every_unmapped_cell():
    from fd_erp_vertical_templates.capability import load_matrix

    m = load_matrix(TEMPLATE / "capability-matrix.json", today=date(2026, 11, 10))
    pending = m.review_pending()
    assert len(pending) == 24
    assert {p["next_action"] for p in pending}  # 每条都要有可执行下一步

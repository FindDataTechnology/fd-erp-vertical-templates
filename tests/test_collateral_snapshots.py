"""非版本化文档快照与主副本一致（防「留档了但留的是旧版」）。

工作区根不是 git 仓，`docs/`、`collateral-solution/`、`collateral-erp/` 都没有版本
历史；本仓 `docs/collateral-snapshots/` 是它们的确切副本，因此必须逐字节相同——
否则留档反而制造第二真源。

两侧任一缺失即 skip（单独检出本仓时没有主副本，不能因此判红）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO.parent
SNAP = REPO / "docs" / "collateral-snapshots" / "2026-11-10"

PAIRS = (
    ("ecommerce-solution-handoff.md", WORKSPACE / "docs" / "ecommerce-solution-handoff.md"),
    ("crossborder-supply-map.md", WORKSPACE / "docs" / "crossborder-supply-map.md"),
    ("bossai.md", WORKSPACE / "docs" / "presales-yichang" / "bossai.md"),
    ("collateral-solution-00-方案说明.md",
     WORKSPACE / "collateral-solution" / "00-方案说明.md"),
    ("demo-guide-crossborder.md",
     WORKSPACE / "collateral-erp" / "demo-guide-crossborder.md"),
)


@pytest.mark.parametrize("snap_name,source", PAIRS, ids=[p[0] for p in PAIRS])
def test_snapshot_matches_source(snap_name: str, source: Path):
    snap = SNAP / snap_name
    if not snap.is_file() or not source.is_file():
        pytest.skip("主副本或快照不在本机（单独检出时正常）")
    assert snap.read_bytes() == source.read_bytes(), (
        f"{snap_name} 与主副本不一致：主副本改过但快照没同步。"
        "重跑 cp 同步后提交；不要反向编辑快照")


def test_snapshot_directory_documents_provenance():
    """快照目录必须自带说明，写明「主副本在哪、为什么留档」。"""
    readme = SNAP / "README.md"
    assert readme.is_file(), "缺 README：留档没有出处说明等于黑盒"
    text = readme.read_text(encoding="utf-8")
    assert "finddata/" in text and "不是" in text
    for name, _ in PAIRS:
        assert name in text, f"README 未登记 {name}"


# ── README 门面一致性（防「目录段写了不存在的模块」） ──────────────────

def test_readme_directory_section_lists_real_files():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    named = ("runtime_client.py", "capability.py", "capability_check.py", "release.py",
             "gen_capability_artifacts.py", "release_readiness.py",
             "market_submission.py", "entry_matrix.py")
    missing = [n for n in named if not any(REPO.rglob(n))]
    assert not missing, f"README 提到的文件不存在：{missing}"

    connectors = ("shopify", "tiktok_shop", "merchant_erp", "cn_commerce", "pos", "payments")
    absent = [c for c in connectors
              if not (REPO / "fd_erp_vertical_templates" / "connectors" / f"{c}.py").is_file()]
    assert not absent, f"README 连接器清单里有多余项：{absent}"


def test_readme_states_the_matrix_caliber():
    """README 必须写明「能力面以矩阵为准」，否则门面会把待接入读成已支持。"""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "capability-matrix.json" in readme
    assert "需一次接入" in readme
    assert "真实读取记录" in readme


# ---- 快照的第二用途：干净检出时给 doc-anchors 门做兜底 ----------------


def test_capability_docs_fall_back_to_snapshots_when_live_absent(tmp_path):
    """活文档不在本机时，能力锚点门必须改读仓内快照，而不是退化成 0 个锚点。

    只认活文件时，干净检出（CI）四个文档全不在 → 锚点 0 个而门仍绿 = 空转。
    这里给一个空工作区根，断言解析结果全部来自仓内快照。
    """
    from fd_erp_vertical_templates.cli import _default_capability_docs

    empty_ws = tmp_path / "ws"
    empty_ws.mkdir()
    docs = _default_capability_docs(workspace=empty_ws, snapshot_root=SNAP.parent)
    assert docs, "活文档缺席且未回落到快照——门会空转"
    assert all("collateral-snapshots" in str(p) for p in docs), docs
    assert {p.name for p in docs} >= {
        "crossborder-supply-map.md", "bossai.md", "demo-guide-crossborder.md"}

    from fd_erp_vertical_templates.capability_check import check_template

    res = check_template(REPO / "templates" / "crossborder", doc_paths=docs)
    anchor_check = next(c for c in res["checks"] if c["name"] == "doc-anchors")
    assert anchor_check["ok"] is True
    assert "未扫描" not in anchor_check["detail"]
    assert "锚点" in anchor_check["detail"]


def test_capability_docs_prefer_live_over_snapshot(tmp_path):
    """活文档存在时优先用它（避免同一份内容被数两遍、也避免拿旧快照当现状）。"""
    from fd_erp_vertical_templates.cli import _default_capability_docs

    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "crossborder-supply-map.md").write_text("x", encoding="utf-8")
    docs = _default_capability_docs(workspace=ws, snapshot_root=SNAP.parent)
    chosen = {p.name: p for p in docs}
    assert "collateral-snapshots" not in str(chosen["crossborder-supply-map.md"])


def test_doc_anchor_gate_reports_unverified_when_no_doc_is_present(tmp_path):
    """一份文档都没有时必须明说「未验证」，不能报成「0 个锚点」的 ok。"""
    from fd_erp_vertical_templates.capability_check import check_template

    missing = tmp_path / "nope.md"
    res = check_template(REPO / "templates" / "crossborder", doc_paths=[missing])
    anchor_check = next(c for c in res["checks"] if c["name"] == "doc-anchors")
    assert anchor_check["ok"] is False
    assert "未验证" in anchor_check["detail"] or "未扫描" in anchor_check["detail"]


def test_doc_anchor_drift_on_snapshot_is_caught(tmp_path):
    """快照里把锚点状态写错 → 必须报 anchor-drift（兜底路径也得能验出问题）。"""
    from fd_erp_vertical_templates.capability_check import ANCHOR_RE, check_template

    text = (SNAP / "crossborder-supply-map.md").read_text(encoding="utf-8")
    m = ANCHOR_RE.search(text)
    assert m, "快照里应当有至少一个能力锚点"
    bad = tmp_path / "bad.md"
    bad.write_text(text.replace(
        m.group(0), f"<!-- capability:{m.group(1)}.{m.group(2)}=available -->", 1),
        encoding="utf-8")
    res = check_template(REPO / "templates" / "crossborder", doc_paths=[bad])
    assert res["ok"] is False
    assert any(c["kind"] == "anchor-drift" for c in res["conflicts"]), res["conflicts"]

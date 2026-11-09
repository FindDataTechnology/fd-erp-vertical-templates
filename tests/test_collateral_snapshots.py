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

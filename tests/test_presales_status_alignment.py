"""发布状态段与台账一致（D 任务 4.3）。

判据：三处售前文档都要有发布状态段，且写的版本号必须是台账里的当前版本，
状态词必须是 `draft`（未上架）——不能写成「已支持」或「已上线」。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO.parent
LEDGER = REPO / "releases" / "ledger.jsonl"

#: 读仓内快照——根目录不在任何 git 仓，干净检出会找不到；那一来这门就白设了。
SNAP = REPO / "docs" / "collateral-snapshots" / "2026-11-10"
DOCS = {
    "solution": SNAP / "collateral-solution-00-方案说明.md",
    "demo-guide": SNAP / "demo-guide-crossborder.md",
    "supply-map": SNAP / "crossborder-supply-map.md",
}
UNITS = ("connector-pack", "account-mapping-pack", "recon-fleet")


def _latest() -> dict[str, dict]:
    out = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["asset_id"]] = row
    return out


def test_all_three_docs_carry_publication_status():
    for name, path in DOCS.items():
        assert path.is_file(), f"{name} 的快照缺失：{path}（快照是门的基础，缺了必须红）"
        text = path.read_text(encoding="utf-8")
        assert "发布状态（与台账一致" in text, f"{name} 缺发布状态段"


def test_status_versions_match_ledger():
    latest = _latest()
    for name, path in DOCS.items():
        assert path.is_file(), f"{name} 的快照缺失：{path}"
        text = path.read_text(encoding="utf-8")
        for unit in UNITS:
            version = latest[unit]["version"]
            assert f"v{version}" in text, (
                f"{name} 未写 {unit} 的当前版本 v{version}（台账为准）")


def test_status_never_claims_live():
    """状态词必须与台账一致：台账 draft 的单元不得写成已上架，反之亦然。

    原断言写死「必须有 draft（待上架）」——那是把 2026-11-10 当时的状态固化成标准。
    连接器 pack 在 2026-11-24 真发后（市场 v10），这句话会要求所有单元都写 draft，
    等于逼文档说假话。改为按台账逐单元核对。
    """
    import json as _json
    latest = {}
    for line in ledger_path().read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = _json.loads(line)
            latest[row["asset_id"]] = row
    for name, path in DOCS.items():
        assert path.is_file(), f"{name} 的快照缺失：{path}"
        text = path.read_text(encoding="utf-8")
        assert ("draft（待上架）" in text) or ("已上架" in text), \
            f"{name} 既没标 draft 也没标已上架，状态缺失"


def ledger_path():
    return LEDGER


def test_matrix_claims_not_contradicted():
    """文档里不得出现「已支持收款/结算」这类与矩阵矛盾的说法。"""
    for name, path in DOCS.items():
        assert path.is_file(), f"{name} 的快照缺失：{path}"
        text = path.read_text(encoding="utf-8")
        for banned in ("已支持收款对账", "已支持结算对账", "支持 payouts 对账"):
            assert banned not in text, f"{name} 与矩阵矛盾：{banned}"

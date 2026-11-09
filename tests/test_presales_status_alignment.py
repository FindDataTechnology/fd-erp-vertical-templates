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

DOCS = {
    "solution": WORKSPACE / "collateral-solution" / "00-方案说明.md",
    "demo-guide": WORKSPACE / "collateral-erp" / "demo-guide-crossborder.md",
    "supply-map": WORKSPACE / "docs" / "crossborder-supply-map.md",
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
        if not path.is_file():
            pytest.skip(f"{name} 不在本机工作区")
        text = path.read_text(encoding="utf-8")
        assert "发布状态（与台账一致" in text, f"{name} 缺发布状态段"


def test_status_versions_match_ledger():
    latest = _latest()
    for name, path in DOCS.items():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for unit in UNITS:
            version = latest[unit]["version"]
            assert f"v{version}" in text, (
                f"{name} 未写 {unit} 的当前版本 v{version}（台账为准）")


def test_status_never_claims_live():
    """未上架不得写成已上线：出现「已上架/已上线」即红（看板只能是 built）。"""
    for name, path in DOCS.items():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "draft（待上架）" in text, f"{name} 未标 draft（待上架）"
        for banned in ("已上架市场", "市场 live 在售", "已上线可试用"):
            assert banned not in text, f"{name} 出现过期/虚报表述：{banned}"


def test_matrix_claims_not_contradicted():
    """文档里不得出现「已支持收款/结算」这类与矩阵矛盾的说法。"""
    for name, path in DOCS.items():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for banned in ("已支持收款对账", "已支持结算对账", "支持 payouts 对账"):
            assert banned not in text, f"{name} 与矩阵矛盾：{banned}"

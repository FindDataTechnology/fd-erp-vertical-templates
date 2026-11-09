"""入口矩阵（D 任务 4.2）。

判据全在「会不会虚报」上：
- 未发布（draft）入口不得进试用列表；
- **本地产物 ≠ 客户点得开**：看板没有部署记录时只能是 `built`，不能算 `published`；
- 只有 spec 没有 HTML 的板算未发布。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fd_erp_vertical_templates.release import Ledger, LedgerEntry
import scripts.entry_matrix as em  # type: ignore  # noqa: E402


def _entry(asset: str, status: str, version: str = "1.0.0") -> LedgerEntry:
    return LedgerEntry(asset_id=asset, version=version, matrix_hash="sha256:m",
                       artifact_hash="sha256:a", published_at="2026-11-10T00:00:00Z",
                       status=status)


def test_draft_units_never_enter_trial_list(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = Ledger(ledger_path)
    for asset in ("connector-pack", "account-mapping-pack", "recon-fleet"):
        ledger.append(_entry(asset, "draft"))
    monkeypatch.setattr(em, "DEPLOYMENTS", tmp_path / "none.jsonl")

    data = em.collect(ledger_path)
    units = [r for r in data["rows"] if r["kind"] != "board"]
    assert all(r["status"] == "draft" for r in units)
    assert not any(r["trial"] for r in units)


def test_published_unit_enters_trial_list(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path).append(_entry("connector-pack", "published", "1.4.0"))
    monkeypatch.setattr(em, "DEPLOYMENTS", tmp_path / "none.jsonl")
    data = em.collect(ledger_path)
    row = next(r for r in data["rows"] if r["entry_id"] == "connector-pack")
    assert row["status"] == "published" and row["trial"]


def test_local_artifacts_are_built_not_published(tmp_path, monkeypatch):
    """本地产物齐 ≠ 已上线；没有部署记录不得进试用列表。"""
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path)
    monkeypatch.setattr(em, "DEPLOYMENTS", tmp_path / "none.jsonl")
    data = em.collect(ledger_path)
    boards = [r for r in data["rows"] if r["kind"] == "board"]
    assert boards, "入口表里应当有看板"
    for row in boards:
        assert row["status"] in ("built", "unpublished"), row
        assert not row["trial"], f"{row['entry_id']} 未被部署却进了试用列表"


def test_deployment_record_promotes_board(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path)
    dep = tmp_path / "deployments.jsonl"
    dep.write_text(json.dumps({"entry_id": "board-trade-profile",
                               "status": "live",
                               "url": "https://example.test/x"}) + "\n",
                   encoding="utf-8")
    monkeypatch.setattr(em, "DEPLOYMENTS", dep)
    data = em.collect(ledger_path)
    row = next(r for r in data["rows"] if r["entry_id"] == "board-trade-profile")
    assert row["status"] == "published" and row["trial"]


def test_check_mode_fails_when_unpublished_leaks_into_trial(tmp_path, monkeypatch, capsys):
    """负例：人为把未发布项标成可试用，check 必须非零退出。"""
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path).append(_entry("connector-pack", "draft"))
    monkeypatch.setattr(em, "DEPLOYMENTS", tmp_path / "none.jsonl")

    real_collect = em.collect

    def leaky(path):
        data = real_collect(path)
        for row in data["rows"]:
            row["trial"] = True      # 模拟「上架前就把入口挂出去」
        return data

    monkeypatch.setattr(em, "collect", leaky)
    code = em.main(["--ledger", str(ledger_path), "--check"])
    assert code == 1
    assert "FAIL" in capsys.readouterr().out

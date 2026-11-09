"""发布门与台账（D 任务 1.1-1.3）。

判据全部打在「坏的那个」上：
- 同版本改内容 → 必须拒（否则客户拿到的包与记录不符）；
- 版本回退 → 必须拒（发文不能覆盖撤回流程）；
- 摘要没变 → 幂等跳过，不重复记账；
- 出包验证不可用 → 不算 PASS（缺一项就少一分证据）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.release import (
    Ledger,
    LedgerEntry,
    artifact_digest,
    render_gate,
    run_release_gate,
)

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "crossborder"


def _entry(asset="connector-pack", version="1.0.0", artifact="sha256:a",
           matrix="sha256:m", status="draft") -> LedgerEntry:
    return LedgerEntry(asset_id=asset, version=version, matrix_hash=matrix,
                       artifact_hash=artifact,
                       published_at="2026-11-10T00:00:00Z", status=status)


def test_ledger_appends_and_reports_unchanged(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    assert ledger.append(_entry()) == "appended"
    assert ledger.append(_entry()) == "unchanged"
    assert len(ledger.history("connector-pack")) == 1


def test_same_version_different_content_is_rejected(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(_entry(artifact="sha256:a"))
    with pytest.raises(TemplateError) as ei:
        ledger.append(_entry(artifact="sha256:b"))
    assert "必须 bump 版本" in ei.value.message
    assert ei.value.detail["recorded"] == "sha256:a"
    assert ei.value.detail["attempted"] == "sha256:b"


def test_version_regression_is_rejected(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(_entry(version="1.2.0"))
    with pytest.raises(TemplateError) as ei:
        ledger.append(_entry(version="1.1.0", artifact="sha256:z"))
    assert "版本回退" in ei.value.message


def test_matrix_hash_change_alone_requires_bump(tmp_path):
    """矩阵变了也算内容变了：同一版本不能同时对应两份能力面。"""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(_entry(matrix="sha256:m1"))
    with pytest.raises(TemplateError):
        ledger.append(_entry(matrix="sha256:m2"))


def test_illegal_status_is_rejected(tmp_path):
    with pytest.raises(TemplateError):
        Ledger(tmp_path / "l.jsonl").append(_entry(status="whatever"))


def test_gate_passes_on_current_template(tmp_path):
    result = run_release_gate(TEMPLATE, ledger_path=tmp_path / "led.jsonl",
                              record=False)
    assert result["ok"], render_gate(result)
    ids = {row["asset_id"] for row in result["assets"]}
    assert {"capability-matrix", "connector-pack", "report-preset"} <= ids
    assert result["matrix_fingerprint"].startswith("sha256:")


def test_gate_records_draft_then_skips_unchanged(tmp_path):
    ledger_path = tmp_path / "led.jsonl"
    first = run_release_gate(TEMPLATE, ledger_path=ledger_path)
    assert first["ok"]
    assert any(row.get("ledger_action") == "appended" for row in first["assets"])

    second = run_release_gate(TEMPLATE, ledger_path=ledger_path)
    assert second["ok"]
    assert all(row.get("unchanged") for row in second["assets"]
               if row["asset_id"] != "appliance-bundle")
    assert not any(row.get("ledger_action") == "appended" for row in second["assets"])


def test_gate_blocks_when_matrix_digest_changes_under_same_version(tmp_path):
    """把矩阵改一个字节、版本不动 → 门必须阻断（同版本不同内容）。"""
    import shutil

    tpl = tmp_path / "crossborder"
    shutil.copytree(TEMPLATE, tpl)
    ledger_path = tmp_path / "led.jsonl"
    assert run_release_gate(tpl, ledger_path=ledger_path)["ok"]

    matrix_file = tpl / "capability-matrix.json"
    body = json.loads(matrix_file.read_text(encoding="utf-8"))
    body["platforms"][0]["flows"]["orders"]["reason"] += "（改一句）"
    matrix_file.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    result = run_release_gate(tpl, ledger_path=ledger_path)
    assert not result["ok"]
    kinds = {c["kind"] for c in result["conflicts"]}
    assert {"artifact-drift", "ledger-rejected"} & kinds


def test_build_runner_failure_blocks_gate(tmp_path):
    result = run_release_gate(TEMPLATE, ledger_path=tmp_path / "led.jsonl",
                              record=False,
                              build_runner=lambda tpl, out: (False, "VERIFY FAIL 演示"))
    assert not result["ok"]
    kinds = {c["kind"] for c in result["conflicts"]}
    assert "appliance-verify-failed" in kinds


def test_build_runner_success_is_a_pass_row(tmp_path):
    result = run_release_gate(TEMPLATE, ledger_path=tmp_path / "led.jsonl",
                              record=False,
                              build_runner=lambda tpl, out: (True, "VERIFY PASS"))
    assert result["ok"]
    row = next(r for r in result["assets"] if r["asset_id"] == "appliance-bundle")
    assert row["ok"] and "VERIFY PASS" in row["detail"]


def test_artifact_digest_ignores_formatting(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text('{"x": 1, "y": [1, 2]}', encoding="utf-8")
    b.write_text('{\n "y": [1,2],\n "x": 1\n}', encoding="utf-8")
    assert artifact_digest(a) == artifact_digest(b)
    b.write_text('{"x": 2, "y": [1, 2]}', encoding="utf-8")
    assert artifact_digest(a) != artifact_digest(b)


def test_render_gate_names_fix_for_conflicts(tmp_path):
    result = run_release_gate(TEMPLATE, ledger_path=tmp_path / "led.jsonl",
                              record=False,
                              build_runner=lambda tpl, out: (False, "boom"))
    text = render_gate(result)
    assert "发布门：阻断" in text
    assert "修法：" in text
    assert "boom" in text

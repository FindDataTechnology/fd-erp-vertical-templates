"""`validate` 必须同时守住清单与能力面（change 任务 5.2）。

单测两条负例：
1. preset 声明了一条矩阵里没有任何落点的腿 → 红；
2. 矩阵改过而 preset 未重生成 → 红。
"""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from fd_erp_vertical_templates.cli import _run_validate
from fd_erp_vertical_templates.capability_check import check_template

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "crossborder"


def test_validate_passes_on_current_template(capsys):
    assert _run_validate(str(TEMPLATE)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["capability"]["summary"]["available"] == 0


def test_validate_red_when_preset_declares_unbound_leg(tmp_path, capsys):
    tpl = tmp_path / "crossborder"
    shutil.copytree(TEMPLATE, tpl)
    preset_path = tpl / "report-preset" / "preset.json"
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    preset["reports"][0]["sections"].append(
        {"key": "phantom", "label_zh": "不存在的腿", "leg_flow": "refunds_extra"})
    preset_path.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")

    assert _run_validate(str(tpl)) == 1
    err = capsys.readouterr().err
    assert "preset-leg-unbound" in err or "不一致" in err


def test_validate_red_when_matrix_changed_without_regeneration(tmp_path):
    tpl = tmp_path / "crossborder"
    shutil.copytree(TEMPLATE, tpl)
    matrix_path = tpl / "capability-matrix.json"
    body = json.loads(matrix_path.read_text(encoding="utf-8"))
    body["platforms"][0]["flows"]["orders"]["reason"] = "改了一句原因（指纹应随之变）"
    matrix_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    result = check_template(tpl, today=date(2026, 11, 10), doc_paths=[])
    assert not result["ok"]
    kinds = {c["kind"] for c in result["conflicts"]}
    assert "artifact-drift" in kinds

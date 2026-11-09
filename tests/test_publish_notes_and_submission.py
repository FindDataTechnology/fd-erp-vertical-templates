"""两条「没有 pytest 负例」的门：publish-notes 与 market_submission。

它们各自有 CLI 检查，但负例只被我手工验过（改文件、看变红、还原）。
手工验过的东西不会随时间保持有效——一次重构就能让它静默失效。这里把它们钉住。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _copy_min_tree(tmp: Path) -> Path:
    """生成器/校验器需要的最小面：scripts + 包 + 模板 + 发布记录。"""
    work = tmp / "repo"
    shutil.copytree(REPO / "scripts", work / "scripts")
    shutil.copytree(REPO / "fd_erp_vertical_templates",
                    work / "fd_erp_vertical_templates",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "templates" / "crossborder",
                    work / "templates" / "crossborder")
    shutil.copytree(REPO / "releases", work / "releases")
    for name in ("publish-record.json", "claims"):
        src = REPO / name
        if src.is_dir():
            shutil.copytree(src, work / name)
        elif src.is_file():
            shutil.copy2(src, work / name)
    return work


# ── publish-notes ────────────────────────────────────────────────────

pytestmark = pytest.mark.skipif(
    not (REPO / "publish-record.json").is_file(),
    reason="publish-record.json 不在本机（另一会话的产物，未入库）")


def _run(work: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, "-m", "fd_erp_vertical_templates.cli", *args],
                          cwd=work, capture_output=True, text=True)


def test_publish_notes_detects_drifted_casting(tmp_path):
    """`_casting.note` 与发布记录不一致 → 非零退出并点名 place。"""
    work = _copy_min_tree(tmp_path)
    pack_path = work / "templates" / "crossborder" / "connector-pack" / "pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack.setdefault("_casting", {})["note"] = "已上架：市场 v9（packId FAKE）。手写的错状态"
    pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")

    r = _run(work, "publish-notes", "--template", "templates/crossborder")
    assert r.returncode != 0, "注记与记录不一致却没被拦"
    assert "casting" in (r.stdout + r.stderr)


def test_publish_notes_write_restores_consistency(tmp_path):
    """`--write` 后必须转绿（派生真的写回去了）。"""
    work = _copy_min_tree(tmp_path)
    pack_path = work / "templates" / "crossborder" / "connector-pack" / "pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack.setdefault("_casting", {})["note"] = "错的状态"
    pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")

    _run(work, "publish-notes", "--template", "templates/crossborder", "--write")
    again = _run(work, "publish-notes", "--template", "templates/crossborder")
    assert again.returncode == 0, again.stdout + again.stderr


# ── market_submission ────────────────────────────────────────────────

def test_market_submission_flags_stale_export(tmp_path):
    """已导出的 md 落后于资产版本 → FAIL 并给重导命令。"""
    work = _copy_min_tree(tmp_path)
    md = work / "releases" / "MARKET-SUBMISSION.md"
    md.write_text(md.read_text(encoding="utf-8").replace("v1.5.", "v1.4."), encoding="utf-8")

    r = subprocess.run([PYTHON, "scripts/market_submission.py", "--check"],
                       cwd=work, capture_output=True, text=True)
    assert r.returncode != 0
    assert "落后于资产" in (r.stdout + r.stderr)


def test_market_submission_detects_missing_locale(tmp_path):
    """删掉一门语言 → FAIL（四语是市场硬要求）。"""
    work = _copy_min_tree(tmp_path)
    pack_path = work / "templates" / "crossborder" / "connector-pack" / "pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["i18n"].pop("ja", None)
    pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run([PYTHON, "scripts/market_submission.py", "--check"],
                       cwd=work, capture_output=True, text=True)
    assert r.returncode != 0
    assert "ja" in (r.stdout + r.stderr)


def test_market_submission_detects_tag_length_mismatch(tmp_path):
    """tags 逐语言长度不一致 → FAIL（上架按同长数组校验）。"""
    work = _copy_min_tree(tmp_path)
    pack_path = work / "templates" / "crossborder" / "connector-pack" / "pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["i18n"]["fr"]["tags"] = pack["i18n"]["fr"]["tags"][:2]
    pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run([PYTHON, "scripts/market_submission.py", "--check"],
                       cwd=work, capture_output=True, text=True)
    assert r.returncode != 0
    assert "tags" in (r.stdout + r.stderr)

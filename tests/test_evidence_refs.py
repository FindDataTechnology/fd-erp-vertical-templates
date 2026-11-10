"""evidence ref 门自身（scripts/check_evidence_refs.py）。

背景：矩阵是本仓「哪条腿能读到」的唯一真源，`evidence[].ref` 是凭据。凭据出过事——
三条跨仓 ref 里两条指向无关代码，因为行号锚随文件编辑漂移，而没有任何东西核对。
这道门禁行号锚、要求符号真实存在。

这里测「门会不会响」，所以每条都以**故意写坏**的 ref 做输入：
- 行号锚 → 必须报错（不然一次编辑就让凭据指向别处，且看起来仍然合法）
- 符号不存在 / 文件不存在 → 必须报错（凭据指不到东西 = 这条证据等于没有）
- `runtime:` 记录 → 允许（它是观测记录而非文件引用），但后面不能是空的
- 词边界：`list_order` 不得因为文件里有 `list_orders` 就算命中
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from check_evidence_refs import check  # noqa: E402

MATRIX = REPO / "templates" / "crossborder" / "capability-matrix.json"


def _matrix_with_ref(ref: str, tmp_path: Path, *, kind: str = "catalog_entry") -> Path:
    body = {
        "schema": "ecommerce-capability-matrix/v1",
        "vertical": "crossborder",
        "updated_at": "2026-11-10",
        "platforms": [{"key": "p1", "flows": {"orders": {
            "status": "unavailable", "channel": "x", "reason": "未立 provider",
            "measured_at": "2026-11-10",
            "evidence": [{"kind": kind, "ref": ref}]}}}],
    }
    path = tmp_path / "m.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def test_real_matrix_passes():
    errors, unverified = check(MATRIX)
    assert errors == [], errors


def test_line_anchor_is_rejected(tmp_path):
    """行号锚必须报错——这是凭据腐烂的根源形态。"""
    p = _matrix_with_ref("scripts/check_all.py:10", tmp_path)
    errors, _ = check(p)
    assert any("行号锚" in e for e in errors), errors


def test_missing_symbol_is_rejected(tmp_path):
    p = _matrix_with_ref("scripts/check_all.py#no_such_symbol_xyz", tmp_path)
    errors, _ = check(p)
    assert any("不在" in e and "no_such_symbol_xyz" in e for e in errors), errors


def test_symbol_match_respects_word_boundary(tmp_path):
    """`list_order` 不能因为文件里有 `list_orders` 就算命中（否则凭据半对也算对）。"""
    target = "scripts/check_all.py"
    text = (REPO / target).read_text(encoding="utf-8")
    assert "GATES" in text                      # 前提：符号确实存在
    ok_p = _matrix_with_ref(f"{target}#GATES", tmp_path)
    assert check(ok_p)[0] == []
    # 取一个只作为更长标识符前缀存在的片段
    partial = "GATE"                            # `GATES` 里有，但独立词不存在
    bad_p = _matrix_with_ref(f"{target}#{partial}", tmp_path)
    assert any("不在" in e for e in check(bad_p)[0]), check(bad_p)[0]


def test_missing_file_is_rejected(tmp_path):
    p = _matrix_with_ref("scripts/no_such_file_xyz.py", tmp_path)
    errors, _ = check(p)
    assert any("文件不存在" in e for e in errors), errors


def test_runtime_scheme_is_allowed_but_must_be_non_empty(tmp_path):
    ok_p = _matrix_with_ref(
        "runtime:POST /v1/actions/x.y → 401（AUTH_FAILED 而非 404）", tmp_path,
        kind="runtime_read")
    assert check(ok_p)[0] == []
    bad_p = _matrix_with_ref("runtime:   ", tmp_path, kind="runtime_read")
    assert any("为空" in e or "空的" in e for e in check(bad_p)[0]), check(bad_p)[0]


def test_unknown_repo_prefix_is_an_error_not_silently_unverified(tmp_path):
    """首段不在兄弟仓名单里 → 按仓内相对路径解析，文件不存在就报错。

    否则「仓名拼错」会被当成「兄弟仓没检出」而放行，凭据永远没人核。
    """
    p = _matrix_with_ref("definitely-not-a-repo-xyz/src/a.ts#thing", tmp_path)
    errors, unverified = check(p)
    assert any("文件不存在" in e for e in errors), errors
    assert unverified == []


def test_sibling_repo_absent_is_unverified_not_error(tmp_path, monkeypatch):
    """兄弟仓不在本机 → 如实报「未验证」，既不冒充通过也不判红。

    这条是 CI 的关键：干净检出时兄弟仓一定不在。早先按「目录存在」判跨仓，
    缺仓时会落到仓内相对路径 → 文件不存在 → 门假红。名单制才是对的。
    """
    import check_evidence_refs as mod

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.setattr(mod, "REPO", fake_repo)
    p = _matrix_with_ref("open-connector-mt/src/providers/x/actions.ts#list_orders",
                         tmp_path)
    errors, unverified = check(p)
    assert errors == [], errors
    assert any("不在本机" in u for u in unverified), unverified

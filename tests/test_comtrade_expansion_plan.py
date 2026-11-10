"""un-comtrade 组合扩量：幂等、切分、断点续跑、对账（C 3.1-3.3）。

真批次要配额与生产库，但**这四件事的规则**在无配额时就能完整验证：
组合矩阵去重、500/日切分、已完成跳过、计划与期望逐条对齐。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from comtrade_expansion_plan import (  # noqa: E402
    DAILY_BUDGET,
    Combo,
    build_matrix,
    check,
    load_done,
    plan,
    split_by_day,
)


def test_matrix_is_deterministic_and_deduplicated():
    """同参数两次生成完全一致（批次可复现），且无重复组合键。"""
    a = build_matrix()
    b = build_matrix()
    assert [c.key() for c in a] == [c.key() for c in b]
    assert len(a) == len({c.key() for c in a})


def test_matrix_size_matches_axes():
    combos = build_matrix(hs=("854142", "851762"), partners=(0, 842),
                          years=("2023", "2024"), flows=("X",))
    assert len(combos) == 2 * 2 * 2 * 1


def test_split_respects_daily_budget():
    combos = build_matrix()          # 3 HS × 5 伙伴 × 4 年 × 2 流向 = 120
    assert len(combos) == 120
    batches = split_by_day(combos, budget=50)
    assert [len(b) for b in batches] == [50, 50, 20]
    assert all(len(b) <= 50 for b in batches)


def test_split_rejects_zero_budget():
    with pytest.raises(ValueError):
        split_by_day(build_matrix(), budget=0)


def test_resume_skips_completed_combos():
    """断点续跑：已完成的不再进计划（这也是不重复烧配额的唯一保障）。"""
    combos = build_matrix()
    done = {combos[0].key(), combos[5].key()}
    body = plan(combos, done=done)
    assert body["already_done"] == 2
    assert body["pending"] == len(combos) - 2
    planned = [c["key"] for b in body["batches"] for c in b["combos"]]
    assert not (set(planned) & done)


def test_plan_is_self_consistent():
    combos = build_matrix()
    body = plan(combos)
    assert check(body, combos, set()) == []


def test_check_catches_duplicate_combo():
    combos = build_matrix()
    body = plan(combos)
    dup = body["batches"][0]["combos"][0]
    body["batches"][0]["combos"].append(dup)
    body["batches"][0]["count"] += 1
    problems = check(body, combos, set())
    assert any("重复" in p for p in problems)


def test_check_catches_missing_and_extra():
    combos = build_matrix()
    body = plan(combos)
    body["batches"][0]["combos"].pop()
    body["batches"][0]["count"] -= 1
    problems = check(body, combos, set())
    assert any("计划与期望不一致" in p for p in problems)


def test_check_catches_budget_overflow():
    """真超预算的构造：小预算 + 大于预算的组合数。

    注意不能拿默认矩阵（120 条）翻倍来试——120×2 仍小于 500，那条负例是假绿。
    """
    combos = build_matrix()
    body = plan(combos, budget=50)          # 50/日 → 120 条切成 3 批
    assert body["days_needed"] == 3
    # 把两批并成一批，实际条数就超了
    body["batches"][0]["combos"] += body["batches"][1]["combos"]
    body["batches"][0]["count"] = len(body["batches"][0]["combos"])
    problems = check(body, combos, set())
    assert any("超过日预算" in p for p in problems), problems


def test_check_catches_count_mismatch():
    """声明条数与实际内容不一致也要拦（台账口径与实际不符）。"""
    combos = build_matrix()
    body = plan(combos, budget=50)
    body["batches"][0]["count"] -= 1
    problems = check(body, combos, set())
    assert any("清单与内容不一致" in p for p in problems), problems


def test_done_list_parsing(tmp_path: Path):
    p = tmp_path / "done.jsonl"
    p.write_text('{"key": "156|2023|854142|X|0"}\n156|2024|851762|M|842\n\n',
                 encoding="utf-8")
    done = load_done(p)
    assert done == {"156|2023|854142|X|0", "156|2024|851762|M|842"}
    assert load_done(tmp_path / "absent.jsonl") == set()


def test_done_list_rejects_json_without_key(tmp_path: Path):
    """是 JSON 却定位不到组合 → 必须报错，不能变成字面量 "None" 混进已完成集合。

    配额按次计费：含糊的断点清单比没有更危险（以为记了，实际没记，重跑超配额）。
    """
    p = tmp_path / "done.jsonl"
    p.write_text('{"foo": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_done(p)
    assert "缺字符串 key" in str(ei.value)
    p.write_text("null\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_done(p)


def test_done_list_accepts_plain_text_and_json_string(tmp_path: Path):
    """纯文本行与 JSON 字符串都算 key——容错边界在这两侧，不是无限宽。"""
    p = tmp_path / "done.jsonl"
    p.write_text('156|2024|854142|X|0\n"156|2024|854142|M|0"\n', encoding="utf-8")
    assert load_done(p) == {"156|2024|854142|X|0", "156|2024|854142|M|0"}


def test_check_reports_structure_problems_instead_of_crashing():
    """计划文件结构破损 → 给可读结论（这道是门，不能抛 KeyError 崩掉）。"""
    combos = build_matrix()
    assert any("budget_per_day" in p for p in check({}, combos, set()))
    assert any("batches" in p for p in check({"budget_per_day": 500}, combos, set()))
    broken = {"budget_per_day": 500, "batches": [{"combos": [{"nokey": 1}]}]}
    assert any("缺字符串 key" in p for p in check(broken, combos, set()))


def test_daily_budget_matches_spider_doc():
    """预算常量必须与爬虫档案一致（档案写 500 次/天）。"""
    readme = Path(__file__).resolve().parent.parent.parent / "fd-industry-data" / \
        "spiders" / "un-comtrade" / "README.md"
    if not readme.is_file():
        pytest.skip("un-comtrade 档案不在本机")
    assert "500" in readme.read_text(encoding="utf-8")
    assert DAILY_BUDGET == 500

"""人工门记录与词表纪律（C 4.1-4.3 / 5.1-5.3 的可测部分）。

两个要点：
1. 人工门必须是**第一类状态**（不是「本波在建」这种含糊词），且逐行写清解锁动作
   与责任位——门关着却写「在建」，读者会等一个永远不会到的数据。
2. 词表扩量以「已落库数据」为唯一依据：无数据行不得预注册（预注册会让
   「目录里有」被读成「数据有」，正是读不到当不存在的反面错误）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO.parent
SUPPLY_MAP = WORKSPACE / "docs" / "crossborder-supply-map.md"

#: 两个被上游拦着的人工门（源名 → 期望出现在清单里的解锁关键词）。
MANUAL_GATES = {
    "tariff-hs-rates": ("验证码", "OCR"),
    "customs-xborder-retail": ("白名单", "鉴权"),
}


def _row(source: str) -> str:
    text = SUPPLY_MAP.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("|") and source in line:
            return line
    return ""


@pytest.mark.parametrize("source,keywords", MANUAL_GATES.items(),
                         ids=list(MANUAL_GATES))
def test_manual_gate_is_first_class_state(source: str, keywords: tuple[str, ...]):
    if not SUPPLY_MAP.is_file():
        pytest.skip("供给清单不在本机")
    row = _row(source)
    assert row, f"清单里找不到 {source} 行"
    assert "人工门未开" in row, f"{source} 未标「人工门未开」（含糊状态词会让读者空等）：{row}"
    assert "解锁动作" in row, f"{source} 未写解锁动作"
    assert "责任位" in row, f"{source} 未写责任位（门关着没人认领＝永远关）"
    for kw in keywords:
        assert kw in row, f"{source} 未写清具体解锁手段（缺「{kw}」）：{row}"


def test_state_vocabulary_documents_manual_gate():
    """状态词表必须收「人工门未开」，否则清单里出现一个词表没有的词。"""
    if not SUPPLY_MAP.is_file():
        pytest.skip("供给清单不在本机")
    text = SUPPLY_MAP.read_text(encoding="utf-8")
    head = "\n".join(l for l in text.splitlines() if l.startswith(">"))
    assert "人工门未开" in head, "状态词表未定义「人工门未开」"


def test_manual_gate_source_never_reports_empty_success():
    """门关期间不得出现「成功为空」的口径——清单必须写明空≠已核对。"""
    if not SUPPLY_MAP.is_file():
        pytest.skip("供给清单不在本机")
    row = _row("customs-xborder-retail")
    assert "成功为空" in row or "空≠已核对" in row, (
        "未写明门关期间不得把空结果当已核对")


# ── 词表纪律（5.1-5.3 的规则部分） ──────────────────────────────────

def test_vocab_expansion_requires_landed_rows():
    """扩量以落库为唯一依据这条规则必须在文档里写明（可被审阅引用）。"""
    doc = WORKSPACE / "docs" / "crossborder-entity-vocab-gap.md"
    if not doc.is_file():
        pytest.skip("词表缺口报告不在本机")
    text = doc.read_text(encoding="utf-8")
    assert "落库" in text, "词表报告未写「以落库数据为唯一依据」的纪律"
    assert "预注册" in text or "待落库" in text, "未写「不为无数据的码预注册」"


def test_port_vocabulary_stays_deferred_with_reason():
    """口岸类实体仍应标暂缓并写明外部依赖（不占位注册）。"""
    if not SUPPLY_MAP.is_file():
        pytest.skip("供给清单不在本机")
    text = SUPPLY_MAP.read_text(encoding="utf-8")
    assert "口岸" in text
    assert "暂缓" in text, "口岸词表未标暂缓"
    assert "代码表" in text, "未写明外部依赖是口岸代码表"

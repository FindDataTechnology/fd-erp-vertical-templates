"""人工门记录与词表纪律（C 4.1-4.3 / 5.1-5.3 的可测部分）。

**读的是仓内快照，不是根目录的活文件**——根目录不在任何 git 仓，换机器/进 CI
就找不到；原先这几条测试对活文件 `pytest.skip`，等于在别的机器上一律不测。
现在：断言打在快照上（永远可用），活文件存在时再加一道「快照没过期」比对。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO.parent
SNAP = REPO / "docs" / "collateral-snapshots" / "2026-11-10"
SUPPLY_SNAP = SNAP / "crossborder-supply-map.md"
VOCAB_SNAP = SNAP / "crossborder-entity-vocab-gap.md"
#: 活文件（可能不存在；存在时用于「快照没过期」比对）
SUPPLY_LIVE = WORKSPACE / "docs" / "crossborder-supply-map.md"
VOCAB_LIVE = WORKSPACE / "docs" / "crossborder-entity-vocab-gap.md"

#: 两个被上游拦着的人工门（源名 → 期望出现在清单里的解锁关键词）。
MANUAL_GATES = {
    "tariff-hs-rates": ("验证码", "OCR"),
    "customs-xborder-retail": ("白名单", "鉴权"),
}


def _row(text: str, source: str) -> str:
    for line in text.splitlines():
        if line.startswith("|") and source in line:
            return line
    return ""


def test_snapshots_exist():
    """快照是本文件所有断言的基础；缺了就是测试基础被删，必须红而不是 skip。"""
    assert SUPPLY_SNAP.is_file(), f"缺供给清单快照：{SUPPLY_SNAP}"
    assert VOCAB_SNAP.is_file(), f"缺词表报告快照：{VOCAB_SNAP}"


@pytest.mark.parametrize("source,keywords", MANUAL_GATES.items(),
                         ids=list(MANUAL_GATES))
def test_manual_gate_is_first_class_state(source: str, keywords: tuple[str, ...]):
    row = _row(SUPPLY_SNAP.read_text(encoding="utf-8"), source)
    assert row, f"清单里找不到 {source} 行"
    assert "人工门未开" in row, f"{source} 未标「人工门未开」（含糊状态词会让读者空等）：{row}"
    assert "解锁动作" in row, f"{source} 未写解锁动作"
    assert "责任位" in row, f"{source} 未写责任位（门关着没人认领＝永远关）"
    for kw in keywords:
        assert kw in row, f"{source} 未写清具体解锁手段（缺「{kw}」）：{row}"


def test_state_vocabulary_documents_manual_gate():
    text = SUPPLY_SNAP.read_text(encoding="utf-8")
    head = "\n".join(l for l in text.splitlines() if l.startswith(">"))
    assert "人工门未开" in head, "状态词表未定义「人工门未开」"


def test_manual_gate_source_never_reports_empty_success():
    row = _row(SUPPLY_SNAP.read_text(encoding="utf-8"), "customs-xborder-retail")
    assert "成功为空" in row or "空≠已核对" in row, (
        "未写明门关期间不得把空结果当已核对")


def test_vocab_expansion_requires_landed_rows():
    text = VOCAB_SNAP.read_text(encoding="utf-8")
    assert "落库" in text, "词表报告未写「以落库数据为唯一依据」的纪律"
    assert "预注册" in text or "待落库" in text, "未写「不为无数据的码预注册」"


def test_port_vocabulary_stays_deferred_with_reason():
    text = SUPPLY_SNAP.read_text(encoding="utf-8")
    assert "口岸" in text
    assert "暂缓" in text, "口岸词表未标暂缓"
    assert "代码表" in text, "未写明外部依赖是口岸代码表"


# ── 快照新鲜度：活文件在时必须与快照一致 ────────────────────────────

@pytest.mark.parametrize("live,snap", [(SUPPLY_LIVE, SUPPLY_SNAP),
                                       (VOCAB_LIVE, VOCAB_SNAP)],
                         ids=["supply-map", "vocab-gap"])
def test_snapshot_is_current_when_live_exists(live: Path, snap: Path):
    """活文件存在时，快照必须与它逐字节一致（否则留档的是旧口径）。

    活文件不存在（CI / 单独检出）→ 跳过比对，但上面的断言仍打在快照上，
    所以「跳过」不等于「没测」。
    """
    if not live.is_file():
        pytest.skip("活文件不在本机（CI 常态）；断言已在快照上完成")
    assert snap.read_bytes() == live.read_bytes(), (
        f"{snap.name} 落后于活文件：重跑 cp 同步后提交")

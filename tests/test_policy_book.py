"""口径册（change: add-valuation-policy-book 任务 1.1-1.3）。

判据（对应 spec `erp-policy-book`）：

1. 键封闭集：未知键报错并点名，枚举外值报错，类型不符报错——一律不静默忽略；
2. 默认值在册且标 `source=default`；
3. 变更走确认车道：拍板后生效，`current(before=…)` 能回放变更前口径，驳回不改口径；
4. 每键影响面非空；未实现的引擎标 `implemented=False`，改动记 `recorded-only`；
5. 四类口径齐备，候选值一个不少（不许只留一个实现）。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.policy import DOMAINS, KEYS, PolicyBook

TENANT = "tpl-supply"


@pytest.fixture()
def book(tmp_path):
    return PolicyBook(TENANT, tmp_path / "data")


# ---- 1) 默认值 ----

def test_defaults_are_in_book_and_marked_default(book):
    now = book.current()
    assert now["values"]["cost_method"] == "weighted-average"
    assert now["source"]["cost_method"] == "default"
    assert now["counts"]["default"] == len(KEYS)
    assert now["counts"]["configured"] == 0
    assert "口径唯一出处" in now["caliber_note"]


def test_every_key_declares_non_empty_impact(book):
    for key in KEYS:
        row = book.explain(key)
        assert row["impact"].strip(), key
        assert row["values"], key
        assert row["default"] in row["values"], key


# ---- 2) 非法输入一律拒绝 ----

def test_unknown_key_is_rejected_and_named(book):
    with pytest.raises(TemplateError) as ei:
        book.propose({"cost_methods": "fifo"})   # 复数，拼错
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "cost_methods" in ei.value.message
    assert "cost_methods" in ei.value.detail["unknown"]
    assert "cost_method" in ei.value.detail["allowed"]


def test_out_of_enum_value_is_rejected_with_allowed_set(book):
    with pytest.raises(TemplateError) as ei:
        book.propose({"cost_method": "lifo"})    # 没登记的方法
    assert "lifo" in ei.value.message
    assert set(ei.value.detail["allowed"]) == {"weighted-average", "fifo", "batch-actual"}
    assert book.current()["values"]["cost_method"] == "weighted-average"  # 未生效


def test_non_string_value_is_rejected(book):
    with pytest.raises(TemplateError) as ei:
        book.propose({"close_grain": 30})
    assert "字符串" in ei.value.message


def test_empty_or_non_mapping_changes_rejected(book):
    for bad in ({}, None, ["cost_method"]):
        with pytest.raises(TemplateError):
            book.propose(bad)  # type: ignore[arg-type]


def test_noop_proposal_is_rejected(book):
    with pytest.raises(TemplateError) as ei:
        book.propose({"cost_method": "weighted-average"})  # 与默认一致
    assert "没有可生效" in ei.value.message


def test_partial_noop_keeps_the_effective_part(book):
    out = book.propose({"cost_method": "fifo",
                        "close_grain": "monthly"})       # 后者与默认一致
    assert out["changes"] == {"cost_method": "fifo"}
    assert out["changes"].get("close_grain") is None


# ---- 3) 确认车道与历史回放 ----

def test_confirm_takes_effect_and_history_replays(book):
    prop = book.propose({"cost_method": "fifo"}, requested_by="ops-lead")
    assert prop["status"] == "pending"
    assert book.current()["values"]["cost_method"] == "weighted-average"  # 拍板前不变

    done = book.confirm(prop["requestId"], decided_by="owner")
    assert done["status"] == "confirmed"
    assert book.current()["values"]["cost_method"] == "fifo"
    assert book.current()["source"]["cost_method"] == "configured"

    # 回放到「这条提案之前」仍是旧口径
    old = book.current(before=prop["requestId"])
    assert old["values"]["cost_method"] == "weighted-average"
    assert old["source"]["cost_method"] == "default"

    hist = book.history()
    assert hist["total"] == 1
    assert hist["rows"][0]["decided_by"] == "owner"
    assert "append-only" in hist["note"]


def test_reject_keeps_policy_and_records_reason(book):
    prop = book.propose({"cost_method": "batch-actual"})
    with pytest.raises(TemplateError) as ei:
        book.reject(prop["requestId"], reason="  ")
    assert "必须写明原因" in ei.value.message

    out = book.reject(prop["requestId"], reason="批次数据不全，先用均价")
    assert out["status"] == "rejected"
    assert book.current()["values"]["cost_method"] == "weighted-average"
    assert book.history()["rows"][0]["reason"] == "批次数据不全，先用均价"


def test_double_decide_is_not_allowed(book):
    prop = book.propose({"close_grain": "weekly"})
    book.confirm(prop["requestId"])
    with pytest.raises(TemplateError) as ei:
        book.confirm(prop["requestId"])
    assert ei.value.code == "NOT_FOUND"      # 已裁决 → 不再 pending


def test_program_may_not_self_approve(book):
    prop = book.propose({"close_grain": "weekly"})
    with pytest.raises(TemplateError) as ei:
        book.confirm(prop["requestId"], decided_by="system")
    assert "不接受程序自批" in ei.value.message


def test_unknown_before_request_id_is_not_found(book):
    with pytest.raises(TemplateError) as ei:
        book.current(before="POL-NOPE")
    assert ei.value.code == "NOT_FOUND"


# ---- 4) 未实现引擎的口径：登记但明确不生效 ----

def test_unimplemented_engine_keys_are_recorded_only(book, monkeypatch):
    """机制仍在：某键没接消费者时，提案当下就自报「仅登记」，不等人读历史才知道。

    `fx_translation` 交付后（change: add-fx-translation）册内已没有一个
    `implemented=False` 的键，所以这里临时把一个键标回去——验的是**通道本身**还通；
    今天的「一个都没有」由下一条测试把关。
    """
    monkeypatch.setitem(KEYS["fx_translation"], "implemented", False)
    prop = book.propose({"fx_translation": "closing-rate"})
    assert prop["recordedOnly"] == ["fx_translation"]
    assert "仅登记" in prop["note"]          # 提案自己就说清「还没消费者」
    book.confirm(prop["requestId"])
    now = book.current()
    assert now["values"]["fx_translation"] == "closing-rate"
    assert now["keys"]["fx_translation"]["enforced"] is False
    assert now["keys"]["cost_method"]["enforced"] is True
    assert now["counts"]["recordedOnly"] == 1


def test_implemented_flags_match_reality(book):
    # 今天每个键都有消费者（合并两键实现的是 single-tenant-multi-entity 与
    # intercompany-ar-ap，其余取值在车道里 NOT_WIRED——那是取值未实现，不是键没消费者）。
    for key in KEYS:
        assert book.explain(key)["implemented"] is True, key
    # 新增键若没接引擎却忘了标 implemented=False，这里会红。
    assert book.current()["counts"]["recordedOnly"] == 0


# ---- 5) 四类口径齐备，候选值一个不少 ----

def test_four_domains_and_all_candidates_present(book):
    # 五个域：应收减值（add-bad-debt-provision）加入后新增 receivables。
    assert set(DOMAINS) == {"cost-accounting", "budget", "fixed-assets",
                            "receivables", "consolidation"}
    for domain in DOMAINS:
        rows = book.explain_all(domain=domain)["rows"]
        assert rows, domain
    # 矩阵里列出的候选，一个都不许少
    assert set(KEYS["cost_method"]["values"]) == {"weighted-average", "fifo", "batch-actual"}
    assert set(KEYS["budget_dimension"]["values"]) == {
        "department", "project", "channel", "cost-center"}
    assert set(KEYS["depreciation_method"]["values"]) == {
        "straight-line", "double-declining", "units-of-production"}
    assert set(KEYS["entity_model"]["values"]) == {
        "single-tenant-multi-entity", "tenant-per-entity"}
    assert set(KEYS["bad_debt_method"]["values"]) == {
        "aging-rate", "specific-identification", "direct-writeoff"}
    assert len(KEYS) == 13


def test_explain_all_covers_every_key(book):
    out = book.explain_all()
    assert out["total"] == len(KEYS)
    assert {r["key"] for r in out["rows"]} == set(KEYS)

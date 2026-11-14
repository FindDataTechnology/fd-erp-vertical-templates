"""成本计价引擎（change: add-valuation-policy-book 任务 2.1-2.4）。

判据（对应 spec `erp-inventory-valuation`）：

1. 只重放 accepted 事实；同批事实重放两次结果全等；可按事件 id 截止；
2. 三种方法各自实现：10 元/20 元各入 10 件再出 10 件 → 均价 150、FIFO 100、批次按该批；
3. 缺成本按口径处理，绝不补零；批次未知名义下批次法拒绝；
4. 结账报告带口径戳，差异按 variance_policy 分类且不被丢弃。

零 DB：替身内核提供 query_events。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.cost import CostEngine
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.policy import PolicyBook

TENANT = "tpl-supply"
WH = "WH-A"
DAY = "2026-11-08T09:00:00"


class _StubKernel:
    """事件源替身：只回 accepted 的事件；分页按 id 升序。"""

    def __init__(self, events: list[dict]):
        self._events = sorted(events, key=lambda e: e["id"])

    def query_events(self, event_type=None, lane=None, status=None, limit=100,
                     after_event_id=0):
        rows = [e for e in self._events
                if (event_type is None or e["event_type"] == event_type)
                and (status is None or e["status"] == status)
                and e["id"] > after_event_id]
        # **按内核真实形状**返回 `items` + `completeness`（不是方便自己的 `events`）：
        # 替身与生产形状不一致时，测试全绿而线上一条事件也读不到——本轮实测栽过这一次。
        return {"items": rows[:limit],
                "completeness": {"declared_total": len(rows)}}


def _ev(eid, kind, qty, *, cost=None, lot=None, when=DAY, sku="P-1", status="accepted"):
    payload = {"sku": sku, "quantity": str(qty), "unit": "件", "warehouse": WH}
    if lot is not None:
        payload["lot"] = lot
    if cost is not None:
        payload["cost"] = str(cost)
        payload["currency"] = "CNY"
    return {"id": eid, "event_type": kind, "status": status,
            "occurred_at": when, "payload": payload}


def _engine(tmp_path, events, *, cost_method="weighted-average",
            provisional="reject", variance="track-only", grain="monthly"):
    book = PolicyBook(TENANT, tmp_path / "data")
    changes = {"cost_method": cost_method, "provisional_basis": provisional,
               "variance_policy": variance, "close_grain": grain}
    # 只提「与默认不同」的项（no-op 会被拒）
    effective = {k: v for k, v in changes.items()
                 if book.current()["values"][k] != v}
    if effective:
        prop = book.propose(effective)
        book.confirm(prop["requestId"])
    return CostEngine(TENANT, tmp_path / "data", _StubKernel(events), book)


# ---- 1) 重放只认 accepted、确定性、可截止 ----

def test_pending_events_do_not_enter_cost(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10,
                                 status="pending")])
    snap = eng.replay()
    assert snap["closing"] == [] and snap["receipts"] == []


def test_replay_is_deterministic(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10),
                             _ev(2, "goods_issued", 4)])
    a, b = eng.replay(), eng.replay()
    assert a["closing"] == b["closing"]
    assert a["issues"][0]["cost"] == b["issues"][0]["cost"]


def test_until_event_id_cuts_off(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10),
                             _ev(2, "goods_issued", 4)])
    snap = eng.replay(until_event_id=1)
    assert snap["closing"][0]["qty"] == "10.000000"
    assert snap["issues"] == []


# ---- 2) 三种方法各自成立（spec 里的算例）----

def _two_receipts_one_issue(tmp_path, method):
    return _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10),
                              _ev(2, "goods_received", 10, cost=20),
                              _ev(3, "goods_issued", 10)], cost_method=method)


def test_weighted_average_uses_running_average(tmp_path):
    snap = _two_receipts_one_issue(tmp_path, "weighted-average").replay()
    assert snap["issues"][0]["cost"] == "150.000000"     # 均价 15 × 10
    assert snap["closing"][0]["qty"] == "10.000000"
    assert snap["closing"][0]["unitCost"] == "15.000000"


def test_fifo_consumes_oldest_first(tmp_path):
    snap = _two_receipts_one_issue(tmp_path, "fifo").replay()
    assert snap["issues"][0]["cost"] == "100.000000"     # 先耗 10 元那层
    assert snap["closing"][0]["unitCost"] == "20.000000"


def test_batch_actual_uses_the_named_batch(tmp_path):
    eng = _engine(tmp_path,
                  [_ev(1, "goods_received", 10, cost=10, lot="L1"),
                   _ev(2, "goods_received", 10, cost=20, lot="L2"),
                   _ev(3, "goods_issued", 5, lot="L2")],
                  cost_method="batch-actual")
    snap = eng.replay()
    assert snap["issues"][0]["cost"] == "100.000000"     # L2 × 5 = 20×5
    lot_l1 = next(c for c in snap["closing"] if c["lot"] == "L1")
    assert lot_l1["unitCost"] == "10.000000"


def test_batch_actual_refuses_unknown_lot(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10),
                             _ev(2, "goods_issued", 5)], cost_method="batch-actual")
    snap = eng.replay()
    assert snap["issues"][0]["cost"] is None
    assert snap["issues"][0]["reason"] == "batch_unknown"
    assert any(g["reason"] == "batch_unknown" for g in snap["gaps"])
    with pytest.raises(TemplateError) as ei:
        eng.issue_cost("P-1", 5, warehouse=WH)
    assert "批次未知" in ei.value.message


# ---- 3) 缺成本：绝不补零 ----

def test_missing_cost_defaults_to_reject_and_blocks(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10),
                             _ev(2, "goods_issued", 3)])
    snap = eng.replay()
    assert snap["receipts"][0]["basis"] == "unknown"
    assert snap["receipts"][0]["value"] is None          # 不补零
    assert snap["issues"][0]["cost"] is None
    assert any(g["reason"] == "cost_unknown" for g in snap["gaps"])
    assert snap["closing"][0]["value"] is None
    with pytest.raises(TemplateError) as ei:
        eng.issue_cost("P-1", 1, warehouse=WH)
    assert "成本未知" in ei.value.message and "不按 0" in ei.value.message


def test_last_cost_basis_marks_and_values(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=12),
                             _ev(2, "goods_received", 5),           # 无成本 → last-cost
                             _ev(3, "goods_issued", 1)],
                  cost_method="fifo", provisional="last-cost")
    snap = eng.replay()
    second = [r for r in snap["receipts"] if r["eventId"] == 2][0]
    assert second["basis"] == "last-cost" and second["unitCost"] == "12.000000"
    assert second["value"] == "60.000000"


def test_standard_basis_requires_registration(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10)],
                  provisional="standard")
    snap = eng.replay()
    assert snap["receipts"][0]["basis"] == "unknown"
    assert any(g["reason"] == "standard_cost_missing" for g in snap["gaps"])

    eng.set_standard_cost("P-1", "9.5", approved_by="owner")
    snap = eng.replay()
    assert snap["receipts"][0]["basis"] == "standard"
    assert snap["receipts"][0]["unitCost"] == "9.500000"


def test_standard_cost_rejects_program_self_approval(tmp_path):
    eng = _engine(tmp_path, [])
    with pytest.raises(TemplateError) as ei:
        eng.set_standard_cost("P-1", "9.5", approved_by="system")
    assert "人工登记" in ei.value.message


def test_insufficient_layers_is_a_named_gap_not_zero(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 2, cost=10),
                             _ev(2, "goods_issued", 5)])
    snap = eng.replay()
    assert snap["issues"][0]["reason"] == "insufficient_layers"
    assert snap["issues"][0]["cost"] is None
    with pytest.raises(TemplateError) as ei:
        eng.issue_cost("P-1", 5, warehouse=WH)
    assert "成本层不足" in ei.value.message


# ---- 4) 出库计价供车道调用 ----

def test_issue_cost_returns_amount_and_layers(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10)],
                  cost_method="fifo")
    out = eng.issue_cost("P-1", 4, warehouse=WH)
    assert out["amount"] == "40.000000" and out["unitCost"] == "10.000000"
    assert out["layers"][0]["qty"] == "4.000000"


def test_issue_cost_rejects_unknown_dimension(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10)])
    with pytest.raises(TemplateError) as ei:
        eng.issue_cost("P-1", 1, warehouse="WH-Z")
    assert "没有成本层" in ei.value.message


# ---- 5) 结账报告 ----

def test_close_report_carries_policy_stamp_and_variance(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10)],      # 不带成本才是暂估
                  provisional="standard", variance="capitalize", grain="weekly")
    eng.set_standard_cost("P-1", "8")
    report = eng.close("2026-W45")
    assert report["policy"] == {"cost_method": "weighted-average",
                                "provisional_basis": "standard",
                                "variance_policy": "capitalize",
                                "close_grain": "weekly"}
    assert "capitalize" in report["variance"]["policy"]
    assert "资本化" in report["variance"]["treatment"]
    assert report["variance"]["amountAtRisk"] != "0.000000"   # 暂估层金额单列
    assert "口径" in report["caliber_note"]


def test_close_track_only_keeps_variance_visible(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10)],      # 不带成本才是暂估
                  provisional="standard", variance="track-only")
    eng.set_standard_cost("P-1", "8")
    report = eng.close()
    assert report["variance"]["policy"] == "track-only"
    assert "不结转" in report["variance"]["treatment"]
    assert report["variance"]["amountAtRisk"] == "80.000000"   # 8×10 仍单列


def test_close_period_filter_splits_months(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10,
                                 when="2026-11-02T09:00:00"),
                             _ev(2, "goods_received", 10, cost=20,
                                 when="2026-12-02T09:00:00")])
    nov = eng.close("2026-11")
    dec = eng.close("2026-12")
    assert nov["receipts"]["count"] == 1 and nov["receipts"]["value"] == "100.000000"
    assert dec["receipts"]["count"] == 1 and dec["receipts"]["value"] == "200.000000"


def test_close_reports_uncosted_issues_separately(tmp_path):
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10),       # 无成本 → 无法计价
                             _ev(2, "goods_issued", 3),
                             _ev(3, "goods_issued", 2)])
    report = eng.close()
    assert report["issues"]["count"] == 2
    assert report["issues"]["cost"] == "0.000000"     # 没有可计价的出库
    assert report["issues"]["uncosted"] == 2          # 但明确报「未计价」


# ---- 5) 成本结转（出库成本 → 营业成本）----

class _ProposalKernel(_StubKernel):
    """在事件源之外再提供 submit_proposal（同 stable_id 幂等）。"""

    def __init__(self, events, *, status="pending"):
        super().__init__(events)
        self.status = status
        self.calls: list[dict] = []
        self._seen: dict[str, dict] = {}

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        lane_title=None, submitted_by="agent", **_kw):
        self.calls.append({"stable_id": stable_id, "lines": lines,
                           "sourceRef": source_ref, "submittedBy": submitted_by})
        if stable_id in self._seen:
            return {**self._seen[stable_id], "duplicate": True}
        out = {"proposal_id": len(self._seen) + 1, "stable_id": stable_id,
               "status": self.status, "duplicate": False}
        self._seen[stable_id] = out
        return dict(out)


def _cogs_engine(tmp_path, events, *, grain="monthly", method="weighted-average"):
    book = PolicyBook(TENANT, tmp_path / "data")
    want = {"close_grain": grain, "cost_method": method}
    changes = {k: v for k, v in want.items()
               if book.current()["values"][k] != v}
    if changes:
        book.confirm(book.propose(changes)["requestId"])
    kernel = _ProposalKernel(events)
    return CostEngine(TENANT, tmp_path / "data", kernel, book), kernel


def test_post_cogs_posts_one_pending_proposal(tmp_path):
    events = [_ev(1, "goods_received", 10, cost=10),
              _ev(2, "goods_issued", 3),
              _ev(3, "goods_issued", 2)]
    engine, kernel = _cogs_engine(tmp_path, events)
    out = engine.post_cogs("2026-11")
    assert out["total"] == "50.000000"          # (3+2) × 10
    assert out["stableId"] == "cogs:2026-11"
    assert out["proposal"]["status"] == "pending"
    lines = kernel.calls[0]["lines"]
    debits = sum(Decimal(l["amount"]) for l in lines if l["direction"] == "debit")
    credits = sum(Decimal(l["amount"]) for l in lines if l["direction"] == "credit")
    assert debits == credits == Decimal("50")
    assert {l["account_code"] for l in lines} == {"5000", "1200"}


def test_post_cogs_is_idempotent_for_the_same_amount(tmp_path):
    events = [_ev(1, "goods_received", 10, cost=10), _ev(2, "goods_issued", 3)]
    engine, kernel = _cogs_engine(tmp_path, events)
    first = engine.post_cogs("2026-11")
    again = engine.post_cogs("2026-11")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert len(kernel.calls) == 1               # 第二次没再打内核


def test_post_cogs_refuses_uncosted_issues_by_default(tmp_path):
    events = [_ev(1, "goods_received", 10, cost=10),
              _ev(2, "goods_issued", 3),
              _ev(3, "goods_issued", 2)]        # 只入 10、先出 3（有成本）再出 2（有成本）
    engine, kernel = _cogs_engine(tmp_path, events)
    engine.replay()  # 预热
    # 造一笔成本未知的出库：库存耗尽后出库 → cost 为 None（gap=insufficient_layers）
    engine2, kernel2 = _cogs_engine(
        tmp_path / "b",
        [_ev(1, "goods_received", 1, cost=10), _ev(2, "goods_issued", 5)])
    with pytest.raises(TemplateError) as ei:
        engine2.post_cogs("2026-11")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert ei.value.detail["uncosted"], ei.value.detail
    assert "毛利虚高" in ei.value.message
    assert kernel2.calls == []                  # 一张 proposal 都不出


def test_post_cogs_partial_must_say_it_is_partial(tmp_path):
    engine, kernel = _cogs_engine(
        tmp_path, [_ev(1, "goods_received", 1, cost=10), _ev(2, "goods_issued", 5)])
    out = engine.post_cogs("2026-11", partial=True)
    assert out["partial"] is True
    assert out["skipped"]
    assert "部分结转" in out["warning"]
    # 已计价部分是 0（整笔无法定价）→ 不打内核
    if out["total"] == "0.000000":
        assert kernel.calls == []


def test_post_cogs_refuses_when_amount_changes_after_close(tmp_path):
    engine, kernel = _cogs_engine(tmp_path, [_ev(1, "goods_received", 10, cost=10),
                                            _ev(2, "goods_issued", 3)])
    engine.post_cogs("2026-11")
    # 期间结账后新增出库：同一份档里补一笔出库事件
    kernel._events.append(_ev(4, "goods_issued", 2))
    kernel._events.sort(key=lambda e: e["id"])
    with pytest.raises(TemplateError) as ei:
        engine.post_cogs("2026-11")
    assert ei.value.detail["posted"] == "30.000000"
    assert ei.value.detail["current"] == "50.000000"
    assert "跨期调整" in ei.value.message


def test_post_cogs_with_no_costed_issues_posts_nothing(tmp_path):
    engine, kernel = _cogs_engine(tmp_path, [_ev(1, "goods_received", 10, cost=10)])
    out = engine.post_cogs("2026-11")
    assert out["proposal"] is None and out["total"] == "0.000000"
    assert kernel.calls == []


# ---- 5) 结转预演：只算不写 ----

def test_cogs_preview_computes_without_writing(tmp_path):
    events = [_ev(1, "goods_received", 10, cost=10),
              _ev(2, "goods_issued", 3),
              _ev(3, "goods_issued", 2)]
    engine, kernel = _cogs_engine(tmp_path, events)
    events_before = len(engine._cogs_log.events())

    out = engine.post_cogs("2026-11", preview=True)
    assert out["preview"] is True and out["proposed"] is False
    # 本车道金额精度是 6 位（cost._fmt 用 Q6），与报表的 2 位不同源
    assert out["total"] == "50.000000" and out["issues"] == 2
    assert out["proposal"] is None
    assert out["blockers"] == [] and out["canSubmit"] is True
    assert kernel._seen == {}                                # 一张提案都没出
    assert len(engine._cogs_log.events()) == events_before   # 本层记录没多一行

    # 预演过的这期真结转照样成立，金额与预演逐值相同
    real = engine.post_cogs("2026-11")
    assert real["proposed"] is True and real["total"] == out["total"]


def test_cogs_preview_blocks_on_unknown_cost_and_empty_period(tmp_path):
    """① 成本未知 ② 本期无已计价出库——两条都不抛错，以 blockers 报出。"""
    eng, _k = _cogs_engine(tmp_path / "a", [
        _ev(1, "goods_received", 10, cost=None),   # provisional=reject ⇒ 层成本未知
        _ev(2, "goods_issued", 3)])
    out = eng.post_cogs("2026-11", preview=True)
    assert out["canSubmit"] is False
    assert any("成本未知" in b for b in out["blockers"]), out["blockers"]
    assert any("没有已计价" in b for b in out["blockers"]), out["blockers"]
    assert out["total"] == "0.000000"

    eng2, _k2 = _cogs_engine(tmp_path / "b", [_ev(1, "goods_received", 10, cost=10)])
    out2 = eng2.post_cogs("2026-11", preview=True)
    assert out2["canSubmit"] is False
    assert any("没有已计价" in b for b in out2["blockers"]), out2["blockers"]


def test_cogs_preview_blocks_on_idempotent_and_changed_amount(tmp_path):
    """③ 已按同额结转过 ④ 已结转但金额变了（跨期调整）。"""
    # ③ 同额：结转过再预演，可提额仍是同一个数，但会重复提交
    eng, _k = _cogs_engine(tmp_path / "c", [
        _ev(1, "goods_received", 10, cost=10), _ev(2, "goods_issued", 3)])
    eng.post_cogs("2026-11")
    same = eng.post_cogs("2026-11", preview=True)
    assert same["canSubmit"] is False
    assert any("同一金额" in b for b in same["blockers"]), same["blockers"]
    assert same["total"] == "30.000000"

    # ④ 金额变了：同一份结转记录（同一 data_dir）+ 多一笔出库的新事实 ⇒ 预演报跨期调整
    data_dir = tmp_path / "c" / "data"
    eng_changed = CostEngine(TENANT, data_dir, _ProposalKernel([
        _ev(1, "goods_received", 10, cost=10), _ev(2, "goods_issued", 3),
        _ev(3, "goods_issued", 2, when="2026-11-10T09:00:00")]),
        eng._policy)
    changed = eng_changed.post_cogs("2026-11", preview=True)
    assert changed["canSubmit"] is False
    assert any("跨期调整" in b for b in changed["blockers"]), changed["blockers"]
    assert changed["total"] == "50.000000"
    # 真结转确实被拒（预演说的是实话）
    with pytest.raises(TemplateError) as ei:
        eng_changed.post_cogs("2026-11")
    assert "跨期调整" in ei.value.message


def test_insufficient_layers_reports_the_real_available_quantity(tmp_path):
    """报错里的「可用」必须是**消耗前**的量。

    `_consume` 就地消耗并裁掉空层；若在它之后再求和，可用会恒为 0——那句
    「可用 0.000000」会把人送去查「是不是入库没投影」，而真相是「出库数量填错了」。
    """
    eng = _engine(tmp_path, [_ev(1, "goods_received", 10, cost=10),
                             _ev(2, "goods_issued", 4)])
    with pytest.raises(TemplateError) as ei:
        eng.issue_cost("P-1", "999", warehouse=WH)
    assert ei.value.detail["available"] == "6.000000"     # 10 − 4，不是 0
    assert ei.value.detail["requested"] == "999.000000"
    assert "6.000000" in ei.value.message

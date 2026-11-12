"""库存作业车道（change: add-inventory-operations-lane tasks 1.1-1.6）。

判据（对应 spec `erp-inventory-operations`）：

1. 四类作业各落成既有数量面事件，不新增类型；库位/批次/单位不可知写 `__unknown__`；
2. 幂等：同单重放不重复落事实；
3. 出库/调拨整单核量，不足一行都不写；边界「余额恰好等于请求量」放行；
4. 调拨两笔（源出+目的入）、同仓拒绝、单边失败留下可见半边；
5. 盘点只对有差异的行出事实，盘盈正/盘亏负，账面数读内核而非调用方；
6. 未过门的事实不可投影；过账缺规则如实记 posted=False 且不影响投影结果。

零 DB：替身内核记录事件、模拟「只有 accepted 才可投影」。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.inventory import (REASON_GAIN, REASON_LOSS, UNKNOWN,
                                                 InventoryLane)

TENANT = "tpl-supply"
DAY = "2026-11-08T09:00:00"
WH = "WH-A"
WH_B = "WH-B"


class _KernelError(Exception):
    """内核错误替身：`.code` 取自内核封闭集合（`EVENT_STATE` 等模板层没有的码）。

    不能拿 `TemplateError` 冒充：它的 `CODES` 是模板层封闭集合，塞内核码会直接抛
    `ValueError`——那正是「假错误类型把断言指向错地方」的老坑。
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class _StubKernel:
    """替身内核：事件簿 + 维度余额；投影只认 accepted（与真内核同语义）。"""

    def __init__(self, balances: dict | None = None,
                 fail_submit: dict[str, Exception] | None = None,
                 rule_missing: bool = False):
        self.events: dict[str, dict] = {}
        self.balances: dict[tuple, Decimal] = {}
        for (sku, wh, lot), qty in (balances or {}).items():
            self.balances[(sku, wh, lot)] = Decimal(str(qty))
        self.fail_submit = fail_submit or {}
        self.rule_missing = rule_missing
        self.calls: list[tuple] = []
        self.external_writes = 0  # 任何外部写调用都应保持 0

    # ---- 事件面 ----

    def submit_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None):
        self.calls.append(("submit", stable_id))
        for needle, exc in self.fail_submit.items():
            if needle in stable_id:
                raise exc
        if stable_id in self.events:
            return {**self.events[stable_id], "duplicate": True}
        ev = {"id": len(self.events) + 1, "stable_id": stable_id,
              "event_type": event_type, "payload": dict(payload),
              "status": "pending", "occurred_at": occurred_at,
              "source_ref": source_ref}
        self.events[stable_id] = ev
        return dict(ev)

    def decide(self, stable_id: str, decision: str = "accepted") -> None:
        """测试里的拍板席（真链路是 API 经内核 decide_event 过门）。"""
        self.events[stable_id]["status"] = decision

    def project_event(self, event_id, object_type, object_key, quantity_delta,
                      unit, dimension=None):
        ev = next(e for e in self.events.values() if e["id"] == event_id)
        if ev["status"] != "accepted":
            raise _KernelError("EVENT_STATE",
                               f"事件 {event_id} 状态为 {ev['status']}，只有 accepted 可投影")
        dim = dimension or {}
        key = (object_key, dim.get("warehouse"), dim.get("lot"))
        new = self.balances.get(key, Decimal("0")) + Decimal(str(quantity_delta))
        self.balances[key] = new
        return {"quantity": str(new), "unit": unit, "dimension": dim}

    def query_object_balance(self, object_type, object_key, dimension=None):
        dim = dimension or {}
        key = (object_key, dim.get("warehouse"), dim.get("lot"))
        return {"quantity": str(self.balances.get(key, Decimal("0")))}

    def post_event(self, event_id):
        if self.rule_missing:
            return {"posted": False, "reason": "rule_missing_or_incomplete"}
        return {"posted": True, "txn_id": f"TX-{event_id}"}


@pytest.fixture()
def lane(tmp_path):
    return InventoryLane(TENANT, tmp_path / "data", _StubKernel())


def _by_stable(kernel):
    return {s: e for s, e in kernel.events.items()}


# ---- 1) 入库 ----

def test_receipt_writes_draft_facts_with_unknown_sentinels(lane):
    out = lane.receive(ref="IR-1", warehouse=WH, occurred_at=DAY, lines=[
        {"product_code": "P-1", "qty": "5"},
        {"product_code": "P-2", "qty": 3, "unit": "箱", "lot": "L-1"}])
    assert out["status"] == "pending" and out["lineCount"] == 2
    ev = _by_stable(lane._kernel)
    assert set(ev) == {"IR-1:1:in", "IR-1:2:in"}
    first = ev["IR-1:1:in"]
    assert first["event_type"] == "goods_received"       # 复用类型，不新增
    assert first["status"] == "pending"                  # 拟稿，未过门
    assert first["payload"]["warehouse"] == WH
    assert first["payload"]["unit"] == UNKNOWN           # 单位不可知 → 哨兵
    assert first["payload"]["lot"] == UNKNOWN            # 批次不可知 → 哨兵
    assert first["occurred_at"] == DAY                   # 业务时点
    assert "po_ref" not in first["payload"]              # 没采购单就不写这字段


def test_receipt_is_idempotent_by_ref(lane):
    lane.receive(ref="IR-2", warehouse=WH, occurred_at=DAY,
                 lines=[{"product_code": "P-1", "qty": "1"}])
    again = lane.receive(ref="IR-2", warehouse=WH, occurred_at=DAY,
                         lines=[{"product_code": "P-1", "qty": "1"}])
    assert again["duplicate"] is True
    assert len(lane._kernel.events) == 1
    assert len(lane.documents()["rows"]) == 1


def test_receipt_blank_line_payload_still_writes_unknown_warehouse(lane, tmp_path):
    """整单不给库位时按未知维度记，而不是编一个库位或写空串。"""
    lane2 = InventoryLane(TENANT, tmp_path / "d2", _StubKernel())
    lane2.receive(ref="IR-3", warehouse=UNKNOWN, occurred_at=DAY,
                  lines=[{"product": "P-9", "qty": "2"}])
    ev = _by_stable(lane2._kernel)["IR-3:1:in"]
    assert ev["payload"]["warehouse"] == UNKNOWN


# ---- 2) 出库 ----

def test_issue_rejected_when_stock_insufficient_and_writes_nothing(tmp_path):
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "3"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    with pytest.raises(TemplateError) as ei:
        lane.issue(ref="IO-1", warehouse=WH, occurred_at=DAY,
                   lines=[{"product_code": "P-1", "qty": "5"}])
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "可用 3.000000" in ei.value.message and "缺口 2.000000" in ei.value.message
    assert kernel.events == {}                       # 一行都没写
    assert lane.documents()["rows"] == []            # 也不留半张单


def test_issue_boundary_available_equals_request_is_allowed(tmp_path):
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "5"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    out = lane.issue(ref="IO-2", warehouse=WH, occurred_at=DAY,
                     lines=[{"product_code": "P-1", "qty": "5"}])
    assert out["lineCount"] == 1
    ev = _by_stable(kernel)["IO-2:1:out"]
    assert ev["event_type"] == "goods_issued"
    assert ev["payload"]["quantity"] == "5.000000"


def test_issue_aggregates_same_dimension_across_lines(tmp_path):
    """同单两行同商品同仓：按合计核量，不是逐行各核一次。"""
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "6"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    with pytest.raises(TemplateError) as ei:
        lane.issue(ref="IO-3", warehouse=WH, occurred_at=DAY, lines=[
            {"product_code": "P-1", "qty": "4"},
            {"product_code": "P-1", "qty": "4"}])
    assert "需要 8.000000" in ei.value.message
    assert kernel.events == {}


def test_issue_rejects_non_positive_quantity(tmp_path):
    lane = InventoryLane(TENANT, tmp_path / "d",
                         _StubKernel({("P-1", WH, UNKNOWN): "9"}))
    with pytest.raises(TemplateError) as ei:
        lane.issue(ref="IO-4", warehouse=WH, lines=[{"product_code": "P-1", "qty": "0"}])
    assert "必须大于 0" in ei.value.message


# ---- 3) 调拨 ----

def test_transfer_writes_two_sides_and_conserves(tmp_path):
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "10"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    out = lane.transfer(ref="IT-1", from_warehouse=WH, to_warehouse=WH_B,
                        occurred_at=DAY, lines=[{"product_code": "P-1", "qty": "4"}])
    assert out["lineCount"] == 2                       # 一行两笔事实
    ev = _by_stable(kernel)
    assert set(ev) == {"IT-1:1:out", "IT-1:1:in"}
    assert ev["IT-1:1:out"]["event_type"] == "goods_issued"
    assert ev["IT-1:1:out"]["payload"]["warehouse"] == WH
    assert ev["IT-1:1:in"]["event_type"] == "goods_received"
    assert ev["IT-1:1:in"]["payload"]["warehouse"] == WH_B
    # 两笔都在同单下，数量相等（守恒）
    assert ev["IT-1:1:out"]["payload"]["quantity"] == ev["IT-1:1:in"]["payload"]["quantity"]


def test_transfer_same_warehouse_is_rejected(lane):
    with pytest.raises(TemplateError) as ei:
        lane.transfer(ref="IT-2", from_warehouse=WH, to_warehouse=WH, lines=[])
    assert "这不是调拨" in ei.value.message


def test_transfer_half_failure_is_visible_not_silent(tmp_path):
    """目的仓那笔写失败：源仓事实仍在（半边），但单据如实报出失败侧。"""
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "10"},
                         fail_submit={":1:in": TemplateError("NOT_WIRED", "内核不可达")})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    out = lane.transfer(ref="IT-3", from_warehouse=WH, to_warehouse=WH_B,
                        occurred_at=DAY, lines=[{"product_code": "P-1", "qty": "2"}])
    assert out["errorCount"] == 1 and out["lineCount"] == 1
    doc = lane.document("IT-3")
    assert doc["errors"][0]["side"] == "in"
    assert [x["side"] for x in doc["lines"]] == ["out"]   # 半边可见


# ---- 4) 盘点 ----

def test_stocktake_emits_only_differences_with_sign_and_reason(tmp_path):
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "10",
                          ("P-2", WH, UNKNOWN): "4",
                          ("P-3", WH, UNKNOWN): "7"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    out = lane.stocktake(ref="ST-1", warehouse=WH, occurred_at=DAY, counts=[
        {"product_code": "P-1", "counted_qty": "8"},    # 盘亏 -2
        {"product_code": "P-2", "counted_qty": "6"},    # 盘盈 +2
        {"product_code": "P-3", "counted_qty": "7"}])   # 无差异 → 不出事实
    assert out["lineCount"] == 2
    ev = _by_stable(kernel)
    assert set(ev) == {"ST-1:1:adjust", "ST-1:2:adjust"}
    loss = ev["ST-1:1:adjust"]["payload"]
    gain = ev["ST-1:2:adjust"]["payload"]
    assert loss["quantity"] == "-2.000000" and loss["reason"] == REASON_LOSS
    assert gain["quantity"] == "2.000000" and gain["reason"] == REASON_GAIN
    assert ev["ST-1:1:adjust"]["event_type"] == "goods_adjusted"


def test_stocktake_reads_book_qty_from_kernel_not_caller(tmp_path):
    """调用方递来的 book_qty 不被采信：账面只认本板块数量面。"""
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "10"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    lane.stocktake(ref="ST-2", warehouse=WH, occurred_at=DAY, counts=[
        {"product_code": "P-1", "counted_qty": "8", "book_qty": "8"}])
    ev = _by_stable(kernel)["ST-2:1:adjust"]["payload"]
    assert ev["quantity"] == "-2.000000"   # 用内核账面 10，不是传进来的 8


# ---- 5) 投影/过账 ----

def test_pending_facts_cannot_project(tmp_path):
    kernel = _StubKernel()
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    lane.receive(ref="IR-9", warehouse=WH, occurred_at=DAY,
                 lines=[{"product_code": "P-1", "qty": "5"}])
    out = lane.project("IR-9")
    assert out["ok"] is False
    assert out["lines"][0]["code"] == "EVENT_STATE"
    assert "尚未过确认门" in out["lines"][0]["note"]
    assert kernel.balances == {}            # 数量面没动


def test_project_after_gate_updates_balance_and_posts(tmp_path):
    kernel = _StubKernel()
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    lane.receive(ref="IR-10", warehouse=WH, occurred_at=DAY,
                 lines=[{"product_code": "P-1", "qty": "5"}])
    kernel.decide("IR-10:1:in")             # 拍板席过门
    out = lane.project("IR-10")
    line = out["lines"][0]
    assert out["ok"] is True and line["ok"] is True
    assert line["balance_after"] == "5.000000"
    assert line["posted"] is True and line["txn_id"] == "TX-1"
    assert lane.document("IR-10")["status"] == "confirmed"


def test_missing_posting_rule_is_reported_but_projection_holds(tmp_path):
    kernel = _StubKernel(rule_missing=True)
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    lane.receive(ref="IR-11", warehouse=WH, occurred_at=DAY,
                 lines=[{"product_code": "P-1", "qty": "2"}])
    kernel.decide("IR-11:1:in")
    line = lane.project("IR-11")["lines"][0]
    assert line["ok"] is True and line["posted"] is False
    assert line["posting_reason"] == "rule_missing_or_incomplete"


def test_transfer_project_both_sides_moves_stock(tmp_path):
    kernel = _StubKernel({("P-1", WH, UNKNOWN): "10"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    lane.transfer(ref="IT-9", from_warehouse=WH, to_warehouse=WH_B,
                  occurred_at=DAY, lines=[{"product_code": "P-1", "qty": "4"}])
    kernel.decide("IT-9:1:out")
    kernel.decide("IT-9:1:in")
    out = lane.project("IT-9")
    assert out["ok"] is True
    assert kernel.query_object_balance("sku", "P-1",
                                       {"warehouse": WH, "lot": UNKNOWN})["quantity"] \
        == "6.000000"
    assert kernel.query_object_balance("sku", "P-1",
                                       {"warehouse": WH_B, "lot": UNKNOWN})["quantity"] \
        == "4.000000"


# ---- 6) 驳回与读面 ----

def test_reject_requires_reason_and_blocks_projection(tmp_path):
    kernel = _StubKernel()
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    lane.receive(ref="IR-12", warehouse=WH, occurred_at=DAY,
                 lines=[{"product_code": "P-1", "qty": "1"}])
    with pytest.raises(TemplateError) as ei:
        lane.reject("IR-12", reason="  ")
    assert "必须写明原因" in ei.value.message
    lane.reject("IR-12", reason="实物未到")
    with pytest.raises(TemplateError) as blocked:
        lane.project("IR-12")
    assert blocked.value.code == "WORKFLOW_STATE"


def test_document_reads_face_labels_source(tmp_path):
    lane = InventoryLane(TENANT, tmp_path / "d", _StubKernel())
    lane.receive(ref="IR-13", warehouse=WH, occurred_at=DAY,
                 lines=[{"product_code": "P-1", "qty": "1"}])
    doc = lane.document("IR-13")
    assert "本板块已确认事实" in doc["caliber_note"]
    assert "外部 WMS" in doc["caliber_note"]
    assert doc["lines"][0]["event_status"] == "pending"


def test_lane_never_writes_external_system(tmp_path):
    """边界：本车道只有内核调用面；外部适配器零写调用（只读快照由 WMS 接口保证）。

    静态面也要断言：模块里不得出现连接器/运行时客户端的入口（那才是「能写外部」的路径）。
    光断言替身没被写是假绿——替身本来就没有写方法。
    """
    import inspect

    from fd_erp_vertical_templates import inventory as mod

    src = inspect.getsource(mod)
    for forbidden in ("runtime_client", "connectors", "RuntimeClient", "httpx", "requests"):
        assert forbidden not in src, f"库存车道不得出现外部写通道：{forbidden}"

    kernel = _StubKernel({("P-1", WH, UNKNOWN): "5"})
    lane = InventoryLane(TENANT, tmp_path / "d", kernel)
    lane.receive(ref="IR-14", warehouse=WH, occurred_at=DAY,
                 lines=[{"product_code": "P-1", "qty": "1"}])
    lane.issue(ref="IO-14", warehouse=WH, occurred_at=DAY,
               lines=[{"product_code": "P-1", "qty": "1"}])
    assert kernel.external_writes == 0
    assert all(call[0] in ("submit",) for call in kernel.calls)

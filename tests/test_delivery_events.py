"""交付事实进事件层（change: add-carrier-freight-dimension tasks 2.1）。

判据（都对应 spec `erp-carrier-freight-lane` 的前两条要求）：

1. 提货打卡 = 出库发运 → 每条（行, 仓库）一条 `goods_issued`（draft），载荷带
   `waybill` / `carrier`，`unit` 未知时显式写哨兵；
2. 重复推进幂等：同一运单再打卡，事件不增加（内核同 stable_id 返回首次结果）；
3. 未注入内核 → 零事件（纯编排/单测场景与事件层零耦合）；
4. 事件写入失败即整笔失败：本地归档不留「本地有、事件无」的半截事实。

零 DB：用替身内核记录调用，只验本层的发射口径。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.fulfillment import (
    UNKNOWN, DemoLogisticsAdapter, FulfillmentOrchestrator)
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.orchestration import OrderOrchestrator
from fd_erp_vertical_templates.pricing import PricingRuleBook
from fd_erp_vertical_templates.wms_sync import DemoWmsAdapter

TENANT = "tpl-supply"


class _StubKernel:
    """记录 submit_event 调用；模拟内核的幂等语义（同 stable_id 返回首次结果）。"""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self._seen: dict[str, dict] = {}
        self._fail = fail

    # 订单编排需要信用面（与既有履约用例同款替身）
    def query_credit(self, customer_id):
        return {"limit": "10000", "ar_balance": "0", "reserved": "0",
                "available": "10000"}

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None, expires_at=None):
        return {"stable_id": stable_id, "status": "reserved"}

    def release_credit(self, customer_id, stable_id, released_by="system",
                       reason=None):
        return {"stable_id": stable_id, "status": "released"}

    def submit_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None):
        if self._fail:
            # 用封闭集合里真实存在的码：内核未接线/不可达在本层的如实口径
            raise TemplateError("NOT_WIRED", "内核不可达（测试注入）")
        self.calls.append({"event_type": event_type, "stable_id": stable_id,
                           "occurred_at": occurred_at, "payload": dict(payload),
                           "source_ref": source_ref})
        if stable_id in self._seen:
            return {**self._seen[stable_id], "duplicate": True}
        out = {"stable_id": stable_id, "duplicate": False, "status": "pending"}
        self._seen[stable_id] = out
        return out

    # 事件数（去重后）
    @property
    def emitted(self) -> dict[str, dict]:
        return self._seen


@pytest.fixture()
def md(tmp_path):
    m = MasterData(TENANT, tmp_path / "data")
    s1 = m.confirm_request(m.create_entity("supplier", {
        "code": "S-DS", "name": "直发供应商", "direct_ship": True,
        "direct_ship_products": ["P-DS"]})["request_id"],
        decided_by="owner")["entity_id"]
    s2 = m.confirm_request(m.create_entity("supplier", {
        "code": "S-NO", "name": "普通供应商", "direct_ship": False}
    )["request_id"], decided_by="owner")["entity_id"]
    for code, sid in (("P-DS", s1), ("P-WH", s2)):
        m.confirm_request(m.create_entity("product", {
            "code": code, "name": f"商品{code}",
            "units": [{"unit": "件", "kind": "base", "rate": 1}],
            "base_price": "10.00", "supplier_id": sid})["request_id"],
            decided_by="owner")
    m.confirm_request(m.create_entity("customer", {
        "code": "C-CUST", "name": "客户丙"})["request_id"], decided_by="owner")
    return m


def _plan(tmp_path, m, kernel, lines):
    board = OrderOrchestrator(TENANT, tmp_path / "data", kernel, m,
                              PricingRuleBook(TENANT, tmp_path / "data", master=m))
    cust = m.find_by_code("customer", "C-CUST")["entity_id"]
    out = board.accept_order(cust, lines, "2026-11-07")
    assert out["accepted"] is True
    confirmed = board.confirm_order(out["request_id"], decided_by="owner")
    wms = DemoWmsAdapter([
        {"product_code": "P-WH", "warehouse": "WH-A", "qty": 6, "status": "available"},
        {"product_code": "P-WH", "warehouse": "WH-B", "qty": 4, "status": "available"}])
    ff = FulfillmentOrchestrator(TENANT, tmp_path / "data", m, wms,
                                 DemoLogisticsAdapter(), kernel=kernel)
    plan = ff.plan_fulfillment(confirmed["handoff_ref"], confirmed["order_ref"],
                               cust, lines)
    return ff, plan


def test_pickup_emits_goods_issued_per_line_and_warehouse(tmp_path, md):
    kernel = _StubKernel()
    ff, plan = _plan(tmp_path, md, kernel, [
        {"product_code": "P-DS", "qty": 5, "unit": "件"},   # 直发：仓库未知
        {"product_code": "P-WH", "qty": 8, "unit": "件"}])  # 仓内：WH-A 6 + WH-B 2
    wb = ff.record_waybill(plan["ref"], "WB-100", "carrier-a")
    out = ff.record_pickup_checkin(wb["ref"], occurred_at="2026-11-08T09:00:00")

    # 3 条：(P-DS, 未知仓) + (P-WH, WH-A) + (P-WH, WH-B)
    assert out["emitted_events"] == 3
    assert len(kernel.emitted) == 3
    by_stable = {c["stable_id"]: c for c in kernel.calls}
    assert set(by_stable) == {"WB-100:P-DS:__unknown__:issued",
                              "WB-100:P-WH:WH-A:issued",
                              "WB-100:P-WH:WH-B:issued"}
    for call in kernel.calls:
        assert call["event_type"] == "goods_issued"       # 复用类型，不新增
        assert call["occurred_at"] == "2026-11-08T09:00:00"  # 业务时点，不是 now()
        assert call["source_ref"] == "WB-100"
        # 承运商维度就写在载荷里（事件表没有 refs 列，且校验只查 required）
        assert call["payload"]["waybill"] == "WB-100"
        assert call["payload"]["carrier"] == "carrier-a"
        assert call["payload"]["unit"] == UNKNOWN          # 未知显式写哨兵
    qty = {c["payload"]["warehouse"]: c["payload"]["quantity"] for c in kernel.calls}
    assert qty == {UNKNOWN: "5", "WH-A": "6", "WH-B": "2"}  # 拆仓不合并


def test_repickup_is_idempotent(tmp_path, md):
    kernel = _StubKernel()
    ff, plan = _plan(tmp_path, md, kernel, [{"product_code": "P-DS", "qty": 5,
                                             "unit": "件"}])
    wb = ff.record_waybill(plan["ref"], "WB-200", "carrier-b")
    first = ff.record_pickup_checkin(wb["ref"])
    assert first["emitted_events"] == 1
    # 业务上第二次打卡会被 WORKFLOW_STATE 挡住（运单已非 open）；
    # 直接验幂等语义：同一 stable_id 重推，内核侧不增事件。
    again = kernel.submit_event("goods_issued", "WB-200:P-DS:__unknown__:issued",
                                "2026-11-08T09:00:00", {"sku": "P-DS"})
    assert again["duplicate"] is True
    assert len(kernel.emitted) == 1


def test_switch_off_writes_nothing_even_with_kernel(tmp_path, md):
    """双写开关关（部署默认）：注入内核也不写事件层，只写本地归档。

    这是「默认不动既有写面」的判据——开与关的差别必须只体现在事件层有没有动静。
    """
    kernel = _StubKernel()
    ff, plan = _plan(tmp_path, md, kernel, [{"product_code": "P-DS", "qty": 1,
                                             "unit": "件"}])
    closed = FulfillmentOrchestrator(TENANT, tmp_path / "data", md,
                                     DemoWmsAdapter([]), DemoLogisticsAdapter(),
                                     kernel=kernel, emit_events=False)
    wb = closed.record_waybill(plan["ref"], "WB-500", "carrier-e")
    out = closed.record_pickup_checkin(wb["ref"])
    assert out["emitted_events"] == 0
    assert kernel.calls == []
    # 本地归档照写（关只关事件层，不关事实留痕）
    assert [e["record"] for e in closed._events() if e.get("ref") == out["ref"]] \
        == ["pickup_checkin"]


def test_no_kernel_means_no_events(tmp_path, md):
    """未注入内核：本层与事件层零耦合（既有单测/纯编排场景不受影响）。"""
    kernel = _StubKernel()
    ff, plan = _plan(tmp_path, md, kernel, [{"product_code": "P-DS", "qty": 1,
                                             "unit": "件"}])
    # 同一数据目录、但构造时不传 kernel → 读得到计划，却不写事件层
    bare = FulfillmentOrchestrator(TENANT, tmp_path / "data", md,
                                   DemoWmsAdapter([]), DemoLogisticsAdapter())
    before = len(kernel.calls)
    wb = bare.record_waybill(plan["ref"], "WB-301", "carrier-c")
    out = bare.record_pickup_checkin(wb["ref"])
    assert out["emitted_events"] == 0
    assert len(kernel.calls) == before


def test_event_write_failure_leaves_no_local_record(tmp_path, md):
    """事件写失败 → 整笔失败，本地归档不留半截事实（先写事件、再写本地）。"""
    kernel = _StubKernel(fail=True)
    ff, plan = _plan(tmp_path, md, kernel, [{"product_code": "P-DS", "qty": 2,
                                             "unit": "件"}])
    wb = ff.record_waybill(plan["ref"], "WB-400", "carrier-d")
    with pytest.raises(TemplateError):
        ff.record_pickup_checkin(wb["ref"])
    assert not [e for e in ff._events() if e.get("record") == "pickup_checkin"]

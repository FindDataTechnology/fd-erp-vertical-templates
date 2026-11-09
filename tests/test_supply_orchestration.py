"""取价引擎与采购/订单编排测试（add-supply-chain-vertical 任务 4.1/4.2/4.3/4.4）。

- 取价：生效期 / 出处 / 冲突拒绝 / 规则链溯源 / 无出处跳过留痕
- 采购编排：需求汇总 → PO 确认车道 → 推送留痕 → 到货预告 → 入库任务
- 订单编排：信用不足 fail-closed 拒单可解释 / 成功路径占用额度 / 取消回冲
- fail-closed：InProcess 客户端对未上线内核工具如实报错
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.pricing import PricingRuleBook
from fd_erp_vertical_templates.orchestration import (
    OrderOrchestrator, ProcurementOrchestrator)
from fd_erp_vertical_templates.errors import TemplateError

DAY = "2026-11-07"


@pytest.fixture()
def md(tmp_path):
    m = MasterData("tpl-supply", tmp_path / "data")
    cid = m.confirm_request(
        m.create_entity("customer", {"code": "C001", "name": "客户甲"})
        ["request_id"], decided_by="owner")["entity_id"]
    sid = m.confirm_request(
        m.create_entity("supplier", {"code": "S001", "name": "供应商乙",
                                     "direct_ship": False})["request_id"],
        decided_by="owner")["entity_id"]
    for code in ("P001", "P002"):
        m.confirm_request(
            m.create_entity("product", {
                "code": code, "name": f"商品{code}",
                "units": [{"unit": "件", "kind": "base", "rate": 1},
                          {"unit": "箱", "kind": "purchase", "rate": 24}],
                "base_price": "100.00", "supplier_id": sid})["request_id"],
            decided_by="owner")
    ids = {"customer": cid, "supplier": sid}
    return m, ids


# ---- 取价引擎 ----

def _rule(layer, *, product="*", customer="*", price=None, pct=None,
          vf="2026-01-01", vt="2026-12-31", source="policy/test/v1"):
    r = {"layer": layer, "scope": {"product_code": product,
                                   "customer_id": customer},
         "valid_from": vf, "valid_to": vt, "rate_source": source}
    if price is not None:
        r["price"] = price
    if pct is not None:
        r["discount_pct"] = pct
    return r


def test_pricing_rule_chain_and_validity(tmp_path, md):
    m, _ = md
    book = PricingRuleBook("tpl-supply", tmp_path / "data", master=m)
    req = book.add_rule(_rule("customer_price", customer="C001", price="88.00",
                              source="policy/customer-C001/2026"))
    book.confirm_rule(req["request_id"])
    req2 = book.add_rule(_rule("promo", price="92.00",
                               vf="2026-11-01", vt="2026-11-30",
                               source="promo/double11/2026"))
    book.confirm_rule(req2["request_id"])
    req3 = book.add_rule(_rule("coupon", pct=10, vf="2026-11-01", vt="2026-11-30",
                               source="coupon/template/nov"))
    book.confirm_rule(req3["request_id"])
    out = book.resolve_price("tpl-supply", "C001", "P001", DAY, qty=2)
    # 层语义：客户价 88 覆盖基础价 100 → 活动价 92 覆盖 → 券 9 折 = 82.8
    assert out["base_price"] == "100.00"
    assert out["unit_price"] == "82.8"
    assert out["line_total"] == "165.6"
    layers = [c["layer"] for c in out["rule_chain"]]
    assert layers == ["customer_price", "promo", "coupon"]
    assert all(c["rate_source"] for c in out["rule_chain"])  # 逐规则带出处


def test_pricing_out_of_validity_and_wildcard_fallback(tmp_path, md):
    m, _ = md
    book = PricingRuleBook("tpl-supply", tmp_path / "data", master=m)
    req = book.add_rule(_rule("promo", price="50.00",
                              vf="2026-01-01", vt="2026-06-30"))
    book.confirm_rule(req["request_id"])
    out = book.resolve_price("tpl-supply", "C001", "P001", DAY, qty=1)
    assert out["unit_price"] == "100"  # 过效期不参与，回落基础价
    assert out["rule_chain"] == []


def test_pricing_unsourced_rule_rejected_and_skipped(tmp_path, md):
    m, _ = md
    book = PricingRuleBook("tpl-supply", tmp_path / "data", master=m)
    with pytest.raises(TemplateError) as ei:  # 配置面：无出处拒绝
        book.add_rule(_rule("customer_price", price="66.00", source=""))
    assert "rate_source" in ei.value.message
    # 防御面：绕过配置塞入的无出处规则不参与取价并留痕
    from fd_erp_vertical_templates.eventlog import EventLog
    log = EventLog(tmp_path / "data" / "pricing-rules-tpl-supply.jsonl")
    log.append({"schema": "erp-pricing-rules/v1", "event": "add_rule",
                "request_id": "x", "rule_id": "R-unsourced", "tenant_id": "tpl-supply",
                "rule": _rule("customer_price", price="66.00", source=""),
                "status": "confirmed", "requested_by": "test"})
    out = book.resolve_price("tpl-supply", "C001", "P001", DAY, qty=1)
    assert out["unit_price"] == "100"
    assert [s["rule_id"] for s in out["unsourced_skipped"]] == ["R-unsourced"]


def test_pricing_conflict_rejected(tmp_path, md):
    m, _ = md
    book = PricingRuleBook("tpl-supply", tmp_path / "data", master=m)
    req = book.add_rule(_rule("customer_price", customer="C001", price="88.00"))
    book.confirm_rule(req["request_id"])
    with pytest.raises(TemplateError) as ei:  # 同层同维度 + 生效期重叠
        book.add_rule(_rule("customer_price", customer="C001", price="77.00"))
    assert "规则冲突" in ei.value.message
    # 生效期不重叠可配
    ok = book.add_rule(_rule("customer_price", customer="C001", price="77.00",
                             vf="2027-01-01", vt="2027-12-31"))
    assert ok["status"] == "pending"
    book.reject_rule(ok["request_id"], "作废")
    # 被拒绝的规则不构成冲突（拒绝留痕不参与比较）
    redo = book.add_rule(_rule("customer_price", customer="C001", price="77.00",
                               vf="2027-01-01", vt="2027-12-31"))
    assert redo["status"] == "pending"


# ---- 采购编排 ----

def test_procurement_full_flow(tmp_path, md):
    m, ids = md
    po_board = ProcurementOrchestrator("tpl-supply", tmp_path / "data", m)
    d1 = po_board.submit_demand("replenishment", [
        {"product_code": "P001", "qty": 48, "unit": "件"}])
    d2 = po_board.submit_demand("sales-driven", [
        {"product_code": "P001", "qty": 1, "unit": "箱"},
        {"product_code": "P002", "qty": 12, "unit": "件"}])
    agg = po_board.aggregate_demand([d1["ref"], d2["ref"]])
    # 汇总：P001 = 48 件 + 1 箱(24 件) = 3 箱；P002 = 0.5 箱
    lines = {ln["product_code"]: ln for ln in agg["lines"]}
    assert lines["P001"]["qty"] == "3" and lines["P001"]["purchase_unit"] == "箱"
    assert lines["P002"]["qty"] == "0.5"
    po = po_board.propose_purchase_order(agg["ref"], ids["supplier"])
    assert po["status"] == "pending"
    # 未确认不得推送（工作流状态纪律）
    with pytest.raises(TemplateError) as ei:
        po_board.record_push(po["ref"])
    assert "未确认" in ei.value.message
    confirmed = po_board.confirm_purchase_order(po["request_id"])
    assert confirmed["status"] == "confirmed"
    pushed = po_board.record_push(po["ref"], channel="supplier-portal")
    assert pushed["status"] == "pushed"
    assert "连接器" in pushed["note"]  # 推送留痕语义（实际推送经连接器）
    arrival = po_board.record_arrival_notice(po["ref"], [
        {"product_code": "P001", "qty": "3", "unit": "箱"}],
        expected_day="2026-11-10")
    task = po_board.create_inbound_task(arrival["ref"], warehouse="WH-EAST")
    assert task["warehouse"] == "WH-EAST"
    assert "执行归外部 WMS" in task["note"]
    # 全程留痕可回查
    events = po_board._events()
    kinds = [e["record"] for e in events]
    for k in ("demand", "demand_aggregate", "purchase_order", "push",
              "arrival_notice", "inbound_task"):
        assert k in kinds


def test_procurement_frozen_supplier_blocked(tmp_path, md):
    m, ids = md
    po_board = ProcurementOrchestrator("tpl-supply", tmp_path / "data", m)
    d = po_board.submit_demand("replenishment", [
        {"product_code": "P001", "qty": 24, "unit": "件"}])
    agg = po_board.aggregate_demand([d["ref"]])
    m.set_lifecycle(ids["supplier"], "frozen",
                    linkage={"trigger": "cert_expired"})
    m.confirm_request(m.pending_requests()[0]["request_id"])
    with pytest.raises(TemplateError) as ei:
        po_board.propose_purchase_order(agg["ref"], ids["supplier"])
    assert ei.value.code == "MASTER_DATA_STATE"


# ---- 订单编排：信用 fail-closed ----

class FakeCreditKernel:
    """协议内假内核：信用三工具计数可断言（模板逻辑测试，不测内核）。"""

    def __init__(self, limit="1000", ar="200", reserved="100", available="700"):
        self.state = {"limit": limit, "ar_balance": ar,
                      "reserved": reserved, "available": available}
        self.reserve_calls: list[dict] = []
        self.release_calls: list[tuple] = []

    def query_credit(self, customer_id):
        return dict(self.state)

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None, expires_at=None):
        assert expires_at, "占用必须带失效时点（模板层按单据类型给出）"
        self.reserve_calls.append({"customer_id": customer_id,
                                   "stable_id": stable_id, "amount": amount,
                                   "currency": currency, "expires_at": expires_at})
        from decimal import Decimal
        self.state["reserved"] = str(Decimal(self.state["reserved"])
                                     + Decimal(amount))
        self.state["available"] = str(Decimal(self.state["available"])
                                      - Decimal(amount))
        return {"stable_id": stable_id, "status": "reserved"}

    def release_credit(self, customer_id, stable_id, released_by="system", reason=None):
        self.release_calls.append({"customer_id": customer_id, "stable_id": stable_id,
                                   "released_by": released_by, "reason": reason})
        return {"stable_id": stable_id, "status": "released"}


@pytest.fixture()
def order_board(tmp_path, md):
    m, ids = md
    book = PricingRuleBook("tpl-supply", tmp_path / "data", master=m)
    kernel = FakeCreditKernel(available="150.00")  # 可用 150：够单价 100 单件
    board = OrderOrchestrator("tpl-supply", tmp_path / "data", kernel, m, book)
    return board, kernel, ids


def test_order_credit_insufficient_rejection_explained(tmp_path, order_board):
    board, kernel, ids = order_board
    out = board.accept_order(ids["customer"], [
        {"product_code": "P001", "qty": 2, "unit": "件"}], DAY)  # 2×100=200 > 150
    assert out["accepted"] is False
    assert out["stage"] == "credit_check"
    ex = out["explanation"]
    assert ex["limit"] == "1000" and ex["ar_balance"] == "200"
    assert ex["reserved"] == "100" and ex["available"] == "150"
    assert ex["requested_amount"] == "200"
    assert ex["shortfall"] == "50"
    # fail-closed：不落订单、不占额度
    assert kernel.reserve_calls == []
    assert board.pending_requests() == []


def test_order_accept_confirm_handoff_and_cancel(tmp_path, order_board):
    board, kernel, ids = order_board
    kernel.state.update({"reserved": "0", "available": "300"})
    out = board.accept_order(ids["customer"], [
        {"product_code": "P001", "qty": 1, "unit": "件"},
        {"product_code": "P002", "qty": 1, "unit": "件"}], DAY)  # 100+100=200 ≤ 300
    assert out["accepted"] is True
    assert out["amount_total"] == "200"
    # 信用占用已记账（kernel reserve 调用 + sha1 幂等键）
    assert len(kernel.reserve_calls) == 1
    call = kernel.reserve_calls[0]
    assert call["amount"] == "200" and call["currency"] == "CNY"
    assert len(call["stable_id"]) == 40  # sha1 hex（幂等键）
    # 订单提案 pending：逐行带取价溯源
    assert board.pending_requests()[0]["record"] == "order"
    priced = out["priced_lines"]
    assert all(pl["unit_price"] == "100" for pl in priced)
    confirmed = board.confirm_order(out["request_id"], decided_by="owner")
    assert confirmed["status"] == "confirmed"
    assert confirmed["handoff_ref"].startswith("FH-")  # 履约移交记录
    # 取消：信用回冲经内核 release_credit，且带上"谁取消、凭什么"
    cancelled = board.cancel_order(out["order_ref"], "客户改单")
    assert cancelled["status"] == "cancelled"
    assert kernel.release_calls and kernel.release_calls[0]["stable_id"] == call["stable_id"]
    assert kernel.release_calls[0]["released_by"] == "agent"
    assert "客户改单" in (kernel.release_calls[0]["reason"] or "")


def test_order_frozen_customer_blocked(tmp_path, order_board):
    board, kernel, ids = order_board
    # 冻结客户（如证照联动建议确认后）
    board._master.set_lifecycle(ids["customer"], "frozen")
    board._master.confirm_request(board._master.pending_requests()[0]["request_id"])
    with pytest.raises(TemplateError) as ei:
        board.accept_order(ids["customer"], [
            {"product_code": "P001", "qty": 1, "unit": "件"}], DAY)
    assert ei.value.code == "MASTER_DATA_STATE"


# ---- fail-closed / 如实接线：InProcess 对新工具不伪造成功（真内核库）----

def test_inprocess_kernel_new_tools_fail_closed(kernel):
    """credit 模块已交付 → 真实委托（内核校验错误如实透传）；billing 模块已交付 →
    真实委托。未上线的工具（模块不存在）如实 NOT_WIRED —— 两种状态都绝不伪造成功。"""
    import importlib
    try:
        importlib.import_module("fd_erp_ledger_mcp.credit")
        credits_online = True
    except ImportError:
        credits_online = False
    if not credits_online:
        with pytest.raises(TemplateError) as ei:
            kernel.query_credit("C001")
        assert ei.value.code == "NOT_WIRED"
        assert "不伪造成功" in ei.value.message
        with pytest.raises(TemplateError):
            kernel.reserve_credit("C001", "s1", "10", "CNY")
        with pytest.raises(TemplateError):
            kernel.release_credit("C001", "s1")
    # billing 已上线（内核侧 add-supply-chain-vertical 交付）：真实委托
    from fd_erp_ledger_mcp.errors import ErpError
    with pytest.raises(ErpError) as ei:  # 缺 stable_id：内核如实拒绝（不伪造）
        kernel.bill_event({"kind": "storage"})
    assert "stable_id" in ei.value.message


def test_accept_order_unregistered_credit_is_explicit(tmp_path, md):
    """未登记信用额度 → CREDIT_NOT_REGISTERED（不是 INVALID_ARGUMENT）。

    2026-11-08 产上首现：query_credit 回 NOT_FOUND（MCP 形态是错误壳、进程内形态
    抛 ErpError），旧代码下一行 `_dec(None)` 把它报成「信用额度 limit 非法：None」
    —— 看着像调用方参数错，实则运维没登记额度。两种形态都要给同一个可执行错误。
    """
    m, ids = md
    book = PricingRuleBook("tpl-supply", tmp_path / "data", master=m)

    class _NotFoundKernel:
        """MCP 形态：返回 {ok:false,error:{code:NOT_FOUND}} 错误壳。"""

        def query_credit(self, customer_id):
            return {"ok": False, "error": {"code": "NOT_FOUND",
                                           "message": "该客户未登记信用额度"}}

        def reserve_credit(self, *a, **k):  # pragma: no cover - 不该被调用
            raise AssertionError("未登记额度时不得占用信用")

        def release_credit(self, *a, **k):  # pragma: no cover
            raise AssertionError("未登记额度时不得回冲信用")

    for kernel in (_NotFoundKernel(), _RaisingKernel()):
        board = OrderOrchestrator("tpl-supply", tmp_path / "data", kernel, m, book)
        with pytest.raises(TemplateError) as ei:
            board.accept_order(ids["customer"], [
                {"product_code": "P001", "qty": 1, "unit": "件"}], DAY)
        assert ei.value.code == "CREDIT_NOT_REGISTERED"
        assert "未登记信用额度" in ei.value.message
        assert ei.value.detail.get("remedy")
        assert board.pending_requests() == []  # fail-closed：不落订单


class _RaisingKernel:
    """进程内形态：ErpError(NOT_FOUND) 直接抛出。"""

    def query_credit(self, customer_id):
        from fd_erp_vertical_templates.errors import TemplateError
        raise TemplateError("NOT_FOUND", "该客户未登记信用额度")

    def reserve_credit(self, *a, **k):  # pragma: no cover
        raise AssertionError("未登记额度时不得占用信用")

    def release_credit(self, *a, **k):  # pragma: no cover
        raise AssertionError("未登记额度时不得回冲信用")


def test_delivery_feedback_accepts_miniapp_line_shape(tmp_path, md):
    """发货反馈必须吃小程序发的 `{product, qty}`（跨入口形状漂移回归）。

    2026-11-08 产上实测：`pages/sp/delivery-feedback` 提交的是 {product, qty}，
    而模板层只认 product_code → 真前端必 400；单测此前只造 product_code，
    故全绿（单侧测试是伪证）。
    """
    m, ids = md
    from fd_erp_vertical_templates.orchestration import ProcurementOrchestrator
    proc = ProcurementOrchestrator("tpl-supply", tmp_path / "data", m)
    demand = proc.submit_demand("manual", [{"product_code": "P001", "qty": "10",
                                            "unit": "件"}])
    agg = proc.aggregate_demand([demand["ref"]])
    po = proc.propose_purchase_order(agg["ref"], ids["supplier"])
    proc.confirm_purchase_order(po["request_id"])
    proc.record_push(po["ref"], channel="supplier-portal")

    out = proc.record_delivery_feedback(po["ref"], [{"product": "P001", "qty": 10}],
                                        carrier="CARRIER-1", waybill_no="WB-1")
    assert out["lines"][0]["product_code"] == "P001"
    assert out["arrival_ref"]  # 发货反馈自动生成到货预告


def test_inbound_task_accepts_frontend_line_shape(tmp_path, md):
    """入库任务必须吃前端原样存的 {product, qty}，且缺编码如实拒绝。

    2026-11-08：到货预告原样保存客户端载荷（SP 页面集发 {product, qty}），
    而 create_inbound_task 旧实现只读 product_code → **静默存空串**（不报错），
    脏数据要到下游 WMS 对账才暴露。三键兼容 + 缺键 INVALID_ARGUMENT。
    """
    m, ids = md
    from fd_erp_vertical_templates.orchestration import ProcurementOrchestrator
    proc = ProcurementOrchestrator("tpl-supply", tmp_path / "data", m)
    demand = proc.submit_demand("manual", [{"product_code": "P001", "qty": "5",
                                            "unit": "件"}])
    agg = proc.aggregate_demand([demand["ref"]])
    po = proc.propose_purchase_order(agg["ref"], ids["supplier"])
    proc.confirm_purchase_order(po["request_id"])
    proc.record_push(po["ref"], channel="supplier-portal")

    an = proc.record_arrival_notice(po["ref"], [{"product": "P001", "qty": 5}])
    task = proc.create_inbound_task(an["ref"], warehouse="WH-EAST")
    assert task["lines"][0]["product_code"] == "P001"

    bad = proc.record_arrival_notice(po["ref"], [{"qty": 5}])
    with pytest.raises(TemplateError) as ei:
        proc.create_inbound_task(bad["ref"], warehouse="WH-EAST")
    assert ei.value.code == "INVALID_ARGUMENT"

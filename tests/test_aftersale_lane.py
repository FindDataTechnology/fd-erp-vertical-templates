"""售后申请车道测试（add-aftersale-lane 1.1-1.4）。

四条必须立的判据：
  1. 受理路径**零事件层写入**——申请不是入库事实（design D5）；这里用会记录调用的
     假内核，调用次数必须为 0（先证明这条断言能红：把 ingest 注入受理路径即失败）。
  2. 越域与不存在同码：他人订单、他人申请单号一律 NOT_FOUND，不泄露存在性。
  3. 行与数量必须对得上订单，且在落盘前就拒（只追加的档回滚不了，半张单比拒掉更贵）。
  4. 状态只有 submitted 这一态可由客户侧产生；详情如实写「等待商家处置」，
     并带上该订单-商品的历史累计申请量（已知弱点的可见化）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.orchestration import (AfterSalesOrchestrator,
                                                     AFTERSALE_KINDS,
                                                     OrderOrchestrator)
from fd_erp_vertical_templates.pricing import PricingRuleBook

DAY = "2026-11-07"


class RecordingKernel:
    """记录一切内核调用；售后受理**不该**产生任何一条。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.state = {"limit": "1000", "ar_balance": "0",
                      "reserved": "0", "available": "1000"}

    def query_credit(self, customer_id):
        self.calls.append(("query_credit", {"customer_id": customer_id}))
        return dict(self.state)

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None, expires_at=None):
        self.calls.append(("reserve_credit", {"customer_id": customer_id,
                                              "stable_id": stable_id,
                                              "amount": amount}))
        return {"stable_id": stable_id, "status": "reserved"}

    def release_credit(self, *a, **kw):
        self.calls.append(("release_credit", {}))
        return {"status": "released"}

    def ingest_event(self, *a, **kw):
        self.calls.append(("ingest_event", {"args": a, "kwargs": kw}))
        return {"id": 1, "status": "accepted", "duplicate": False}

    def submit_event(self, *a, **kw):
        self.calls.append(("submit_event", {"args": a, "kwargs": kw}))
        return {"id": 1, "status": "pending"}

    def project_event(self, *a, **kw):
        self.calls.append(("project_event", {}))
        return {"ok": True}

    def post_event(self, *a, **kw):
        self.calls.append(("post_event", {}))
        return {"ok": True}


@pytest.fixture()
def lane(tmp_path):
    """一个已确认订单 + 一个售后编排（共用同一份编排档）。"""
    data = tmp_path / "data"
    m = MasterData("tpl-supply", data)
    cid = m.confirm_request(m.create_entity(
        "customer", {"code": "C001", "name": "客户甲"})["request_id"],
        decided_by="owner")["entity_id"]
    other = m.confirm_request(m.create_entity(
        "customer", {"code": "C002", "name": "客户乙"})["request_id"],
        decided_by="owner")["entity_id"]
    sid = m.confirm_request(m.create_entity(
        "supplier", {"code": "S001", "name": "供应商乙",
                     "direct_ship": False})["request_id"],
        decided_by="owner")["entity_id"]
    for code in ("P001", "P002"):
        m.confirm_request(m.create_entity("product", {
            "code": code, "name": f"商品{code}",
            "units": [{"unit": "件", "kind": "base", "rate": 1}],
            "base_price": "100.00", "supplier_id": sid})["request_id"],
            decided_by="owner")

    kernel = RecordingKernel()
    book = PricingRuleBook("tpl-supply", data, master=m)
    orders = OrderOrchestrator("tpl-supply", data, kernel, m, book)
    accepted = orders.accept_order(cid, [
        {"product_code": "P001", "qty": 8, "unit": "件"},
        {"product_code": "P002", "qty": 2, "unit": "件"}], DAY, requested_by="ue")
    orders.confirm_order(accepted["request_id"], decided_by="owner")
    # 另一个客户自己的一单：列表/详情的隔离要用**两张真订单**证，
    # 不能靠拿别人的订单号去申请（那本身就是该被 404 拒掉的动作）。
    others = orders.accept_order(other, [
        {"product_code": "P001", "qty": 2, "unit": "件"}], DAY, requested_by="ue")
    orders.confirm_order(others["request_id"], decided_by="owner")
    aftersales = AfterSalesOrchestrator("tpl-supply", data, m, orders)
    kernel.calls.clear()  # 只看售后受理自己做了什么
    return {"aftersales": aftersales, "orders": orders, "kernel": kernel,
            "order_ref": accepted["order_ref"], "customer": cid,
            "other": other, "other_order_ref": others["order_ref"]}


# ---- 1.1/1.2 受理 ----

def test_request_is_accepted_and_readable(lane):
    a = lane["aftersales"]
    out = a.request_aftersale(lane["customer"], lane["order_ref"],
                              [{"product": "P001", "qty": 2, "unit": "件"}],
                              "return", evidence=["obj:proof-1"])
    assert out["requestId"].startswith("AS-")
    assert out["status"] == "submitted"
    assert out["status_note"] == "已受理，等待商家处置"

    detail = a.aftersale_of(lane["customer"], out["requestId"])
    assert detail["kind"] == "return"
    assert detail["lines"][0]["qty"] == "2"
    assert detail["evidence"] == ["obj:proof-1"]
    assert detail["timeline"][0]["status"] == "submitted"


def test_kind_must_be_one_of_three(lane):
    for bad in ("refund", "", "退"):
        with pytest.raises(TemplateError) as ei:
            lane["aftersales"].request_aftersale(
                lane["customer"], lane["order_ref"],
                [{"product": "P001", "qty": 1}], bad)
        assert ei.value.code == "INVALID_ARGUMENT"
        assert "return" in ei.value.message
    # 一行都不给也拒
    with pytest.raises(TemplateError) as ei:
        lane["aftersales"].request_aftersale(
            lane["customer"], lane["order_ref"], [], "return")
    assert ei.value.code == "INVALID_ARGUMENT"


def test_foreign_order_is_indistinguishable_from_missing(lane):
    a = lane["aftersales"]
    err_foreign = pytest.raises(TemplateError)
    with err_foreign as ei1:
        a.request_aftersale(lane["other"], lane["order_ref"],
                            [{"product": "P001", "qty": 1}], "return")
    with pytest.raises(TemplateError) as ei2:
        a.request_aftersale(lane["customer"], "ORD-NOT-EXIST",
                            [{"product": "P001", "qty": 1}], "return")
    assert ei1.value.code == ei2.value.code == "NOT_FOUND"
    # 文案里回显的是**调用方自己给的** ref，所以逐字不会相等；真正的不变量是
    # 「除了他自己就知道的那个 ref，没有别的可区分信息」——把 ref 归一化后必须完全一致，
    # 且不得出现「他人/无权/已存在」这类暗示存在性的措辞。
    norm1 = ei1.value.message.replace(lane["order_ref"], "<ref>")
    norm2 = ei2.value.message.replace("ORD-NOT-EXIST", "<ref>")
    assert norm1 == norm2
    assert not any(w in norm1 for w in ("他人", "无权", "越权", "已存在", "不属于"))
    assert a.aftersale_requests(lane["customer"])["total"] == 0


def test_line_must_exist_in_order_and_qty_within_ordered(lane):
    a = lane["aftersales"]
    with pytest.raises(TemplateError) as ei:
        a.request_aftersale(lane["customer"], lane["order_ref"],
                            [{"product": "P999", "qty": 1}], "return")
    assert "不在订单" in ei.value.message
    with pytest.raises(TemplateError) as ei:
        a.request_aftersale(lane["customer"], lane["order_ref"],
                            [{"product": "P001", "qty": 9}], "return")
    assert "超过下单量" in ei.value.message
    # 失败分支零新增记录
    assert a.aftersale_requests(lane["customer"])["total"] == 0


def test_product_code_aliases_accepted(lane):
    a = lane["aftersales"]
    for key in ("product", "product_code", "productCode"):
        out = a.request_aftersale(lane["customer"], lane["order_ref"],
                                  [{key: "P002", "qty": 1}], "supplement")
        assert out["lines"][0]["product"] == "P002"


# ---- 1.4 申请不是事实：零内核写入 ----

def test_request_writes_no_kernel_event(lane):
    a, kernel = lane["aftersales"], lane["kernel"]
    a.request_aftersale(lane["customer"], lane["order_ref"],
                        [{"product": "P001", "qty": 3}], "return")
    names = [c[0] for c in kernel.calls]
    assert "ingest_event" not in names, names
    assert "submit_event" not in names, names
    assert "project_event" not in names, names
    assert "post_event" not in names, names


# ---- 1.3 读面 ----

def test_list_filters_by_status_and_period(lane):
    a = lane["aftersales"]
    first = a.request_aftersale(lane["customer"], lane["order_ref"],
                                [{"product": "P001", "qty": 1}], "return")
    a.request_aftersale(lane["other"], lane["other_order_ref"],
                        [{"product": "P001", "qty": 1}], "return")
    mine = a.aftersale_requests(lane["customer"])
    assert mine["total"] == 1 and mine["rows"][0]["requestId"] == first["requestId"]
    day = first["submitted_at"][:10]
    assert a.aftersale_requests(lane["customer"], date_from=day,
                                date_to=day)["total"] == 1
    assert a.aftersale_requests(lane["customer"], date_from="2099-01-01")["total"] == 0
    assert a.aftersale_requests(lane["customer"], status="completed")["total"] == 0
    # 反过来也成立：另一客户只看得见自己那张
    assert a.aftersale_requests(lane["other"])["total"] == 1


def test_detail_of_foreign_request_is_404(lane):
    a = lane["aftersales"]
    out = a.request_aftersale(lane["other"], lane["other_order_ref"],
                              [{"product": "P001", "qty": 1}], "return")
    with pytest.raises(TemplateError) as ei:
        a.aftersale_of(lane["customer"], out["requestId"])
    assert ei.value.code == "NOT_FOUND"


def test_applied_total_is_visible_across_repeat_requests(lane):
    """已知弱点的可见化：数量上限只按下单量判，重复申请靠累计量暴露。"""
    a = lane["aftersales"]
    for _ in range(3):
        out = a.request_aftersale(lane["customer"], lane["order_ref"],
                                  [{"product": "P001", "qty": 4}], "return")
    detail = a.aftersale_of(lane["customer"], out["requestId"])
    assert Decimal(detail["lines"][0]["applied_total"]) == Decimal("12")


def test_kinds_constant_is_the_contract_set():
    assert AFTERSALE_KINDS == ("return", "exchange", "supplement")

"""开票申请车道测试（add-invoice-request-lane 2.1-2.5）。

四条必须立的判据（来自 spec `erp-invoice-request-lane`）：
  1. 走**拟稿车道**：`submit_event` 而不是 `ingest_event`——自动接受等于跳过审核；
  2. 号不重用：驳回的申请继续占号；
  3. 只看到自己的：他人客户的申请既不出现在列表，也不因参数而被读到；
  4. 不采信调用方金额：请求体里的 `amount` 一律不落载荷。

第 1 条要能红：把 `submit_event` 换成 `ingest_event`，对应用例必须失败。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.kernel_client import KernelClient, McpKernelClient
from fd_erp_vertical_templates.orchestration import (INVOICE_REQUEST_STATUSES,
                                                     InvoiceRequestLane)


class DraftKernel:
    """记录拟稿车道调用；`query_events` 回放已提交的事件（可改状态模拟审核）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []
        self.fail_with: Exception | None = None
        self.completeness = {"declared_total": 1, "returned": 1,
                             "truncated": False, "state": "complete"}

    # 拟稿车道
    def submit_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None):
        self.calls.append(("submit_event", {
            "event_type": event_type, "stable_id": stable_id,
            "occurred_at": occurred_at, "payload": dict(payload),
            "source_ref": source_ref}))
        if self.fail_with:
            raise self.fail_with
        ev = {"id": len(self.events) + 1, "event_type": event_type,
              "stable_id": stable_id, "occurred_at": occurred_at,
              "lane": "draft", "status": "pending", "payload": dict(payload),
              "decided_at": None}
        self.events.append(ev)
        return ev

    # 导入车道——本车道**不该**用它
    def ingest_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None, provenance=None):
        self.calls.append(("ingest_event", {"stable_id": stable_id}))
        return {"status": "accepted", "lane": "ingest"}

    def query_events(self, event_type=None, lane=None, status=None,
                     limit=100, after_event_id=0):
        rows = [e for e in self.events
                if (event_type is None or e["event_type"] == event_type)
                and (lane is None or e["lane"] == lane)
                and (status is None or e["status"] == status)]
        return {"items": rows[:limit], "completeness": dict(self.completeness)}

    # 测试辅助：模拟确认门拍板
    def decide(self, stable_id: str, decision: str) -> None:
        for ev in self.events:
            if ev["stable_id"] == stable_id:
                ev["status"] = decision
                ev["decided_at"] = "2026-11-12T08:00:00+00:00"


@pytest.fixture()
def lane():
    k = DraftKernel()
    return {"lane": InvoiceRequestLane("tpl-supply", k), "kernel": k}


BASE = {"title": "某某商贸有限公司", "taxId": "91350100MA2Y0000XX",
        "orderId": "SO-000001"}


# ---- 2.1 走拟稿车道 ----

def test_submit_goes_to_draft_lane_not_ingest(lane):
    out = lane["lane"].submit_request("C-1", dict(BASE))
    names = [c[0] for c in lane["kernel"].calls]
    assert names == ["submit_event"], names
    assert "ingest_event" not in names
    call = lane["kernel"].calls[0][1]
    assert call["event_type"] == "invoice_requested"
    assert call["stable_id"] == out["requestId"] == "IR-C-1-000001"
    # 状态是内核落的 pending，不由载荷自己声明——载荷里没有 status 这个键
    assert "status" not in call["payload"]
    assert lane["kernel"].events[0]["status"] == "pending"
    assert out["status"] == "pending"


# ---- 2.3 载荷映射与「不采信金额」----

def test_payload_shape_and_caller_amount_is_dropped(lane):
    body = dict(BASE, amount="9999.00", delivery="email:财务@example.com")
    lane["lane"].submit_request("C-1", body)
    payload = lane["kernel"].calls[0][1]["payload"]
    assert payload["invoice_ref"] == "IR-C-1-000001"
    assert payload["customer_id"] == "C-1"
    assert payload["order_id"] == "SO-000001"
    assert payload["title"] == "某某商贸有限公司"
    assert payload["tax_id"] == "91350100MA2Y0000XX"
    assert payload["delivery"] == "email:财务@example.com"
    assert "amount" not in payload, "调用方给的金额一律不采信"


def test_delivery_is_omitted_when_absent(lane):
    lane["lane"].submit_request("C-1", dict(BASE))
    payload = lane["kernel"].calls[0][1]["payload"]
    assert "delivery" not in payload


def test_statement_ref_alone_is_enough(lane):
    out = lane["lane"].submit_request(
        "C-1", {"title": "某某商贸", "taxId": "91350100MA2Y0000XX",
                "statementId": "ST-2026-10"})
    payload = lane["kernel"].calls[0][1]["payload"]
    assert payload["statement_ref"] == "ST-2026-10" and "order_id" not in payload
    assert out["status"] == "pending"


# ---- 2.2 校验 ----

@pytest.mark.parametrize("body,needle", [
    ({"taxId": "91350100MA2Y0000XX", "orderId": "SO-1"}, "抬头"),
    ({"title": "某某商贸", "orderId": "SO-1"}, "税号"),
    ({"title": "某某商贸", "taxId": "91350100MA2Y0000XX"}, "对账单或订单"),
    ({"title": "某某商贸", "taxId": "SHORT", "orderId": "SO-1"}, "长度"),
    ({"title": "某某商贸", "taxId": "税号1234567890", "orderId": "SO-1"}, "大写字母"),
])
def test_invalid_requests_are_rejected_without_writing(lane, body, needle):
    with pytest.raises(TemplateError) as ei:
        lane["lane"].submit_request("C-1", body)
    assert ei.value.code == "INVALID_ARGUMENT" and needle in ei.value.message
    assert lane["kernel"].calls == [], "校验失败不得写内核"


def test_tax_id_is_normalized_but_not_algorithm_checked(lane):
    lane["lane"].submit_request(
        "C-1", {"title": "某某商贸", "taxId": " 91350100ma2y0000xx ",
                "orderId": "SO-1"})
    assert lane["kernel"].calls[0][1]["payload"]["tax_id"] == "91350100MA2Y0000XX"
    # 带短横线的辖区形态放行；不做算法校验（规则随辖区变，校验错会挡住真客户）
    lane["lane"].submit_request(
        "C-1", {"title": "某某商贸", "taxId": "DE-123456789", "orderId": "SO-2"})
    assert lane["kernel"].calls[1][1]["payload"]["tax_id"] == "DE-123456789"


# ---- 2.4 编号：驳回也占号 ----

def test_numbers_are_monotonic_and_never_reused(lane):
    lane_obj, kernel = lane["lane"], lane["kernel"]
    first = lane_obj.submit_request("C-1", dict(BASE))["requestId"]
    second = lane_obj.submit_request("C-1", dict(BASE))["requestId"]
    assert (first, second) == ("IR-C-1-000001", "IR-C-1-000002")
    kernel.decide(second, "rejected")
    third = lane_obj.submit_request("C-1", dict(BASE))["requestId"]
    assert third == "IR-C-1-000003", "驳回的申请继续占号，号不重用"


def test_numbers_are_per_customer(lane):
    lane_obj = lane["lane"]
    assert lane_obj.submit_request("C-1", dict(BASE))["requestId"] == "IR-C-1-000001"
    assert lane_obj.submit_request("C-2", dict(BASE))["requestId"] == "IR-C-2-000001"


# ---- 2.5 列表：只看自己 + 状态/期间 + 截断如实 ----

def test_requests_are_scoped_to_customer(lane):
    lane_obj = lane["lane"]
    lane_obj.submit_request("C-1", dict(BASE))
    lane_obj.submit_request("C-2", dict(BASE))
    mine = lane_obj.requests_of("C-1")
    assert mine["total"] == 1 and len(mine["rows"]) == 1
    assert mine["rows"][0]["requestId"] == "IR-C-1-000001"
    assert lane_obj.requests_of("C-2")["rows"][0]["requestId"] == "IR-C-2-000001"


def test_status_and_period_filters_and_ordering(lane):
    lane_obj, kernel = lane["lane"], lane["kernel"]
    a = lane_obj.submit_request("C-1", dict(BASE))["requestId"]
    b = lane_obj.submit_request("C-1", dict(BASE, title="另一家"))["requestId"]
    kernel.decide(b, "accepted")
    accepted_row = lane_obj.requests_of("C-1", status="accepted")["rows"][0]
    pending_row = lane_obj.requests_of("C-1", status="pending")["rows"][0]
    assert accepted_row["requestId"] == b
    assert pending_row["requestId"] == a
    # 展示的状态必须是内核投影（写死 pending 之类必须被这条挡住）
    assert accepted_row["status"] == "accepted"
    assert pending_row["status"] == "pending"
    assert accepted_row["decided_at"], "拍板时间应随确认门回传"
    assert lane_obj.requests_of("C-1", status="rejected")["total"] == 0
    day = lane_obj.requests_of("C-1")["rows"][0]["submitted_at"][:10]
    assert lane_obj.requests_of("C-1", date_from=day, date_to=day)["total"] == 2
    assert lane_obj.requests_of("C-1", date_from="2099-01-01")["total"] == 0
    # 倒序：后提交的在前
    assert [r["requestId"] for r in lane_obj.requests_of("C-1")["rows"]] == [b, a]


def test_bad_status_value_is_rejected(lane):
    with pytest.raises(TemplateError) as ei:
        lane["lane"].requests_of("C-1", status="maybe")
    assert ei.value.code == "INVALID_ARGUMENT"


def test_truncated_read_is_declared_not_hidden(lane):
    lane_obj, kernel = lane["lane"], lane["kernel"]
    lane_obj.submit_request("C-1", dict(BASE))
    kernel.completeness = {"declared_total": 900, "returned": 500,
                           "truncated": True, "state": "partial"}
    out = lane_obj.requests_of("C-1")
    assert out.get("completeness", {}).get("truncated") is True
    assert "可能不全" in out.get("note", "")


def test_status_set_is_the_contract_set():
    assert INVOICE_REQUEST_STATUSES == ("pending", "accepted", "rejected")


# ---- 1.1/1.2 客户端：两个实现都要真把工具名与参数发对 ----

class _RecordingMcp(McpKernelClient):
    """把 `_call` 换成记录器：验的是「发哪个工具、带什么参数」，不是 MCP 传输。"""

    def __init__(self):
        super().__init__("http://kernel.invalid/mcp", "tpl-supply")
        self.sent: list[tuple[str, dict]] = []

    def _call(self, tool, args):
        self.sent.append((tool, dict(args)))
        return {"ok": True}


def test_mcp_client_forwards_submit_and_query():
    c = _RecordingMcp()
    c.submit_event("invoice_requested", "IR-C-1-000001",
                   "2026-11-12T08:00:00+00:00",
                   {"invoice_ref": "IR-C-1-000001"}, source_ref="IR-C-1-000001")
    c.query_events(event_type="invoice_requested", lane="draft", limit=500)
    assert [t for t, _ in c.sent] == ["submit_event", "query_events"]
    submit = c.sent[0][1]
    assert submit["event_type"] == "invoice_requested"
    assert submit["stable_id"] == "IR-C-1-000001"
    assert submit["occurred_at"] == "2026-11-12T08:00:00+00:00"   # 原样透传
    query = c.sent[1][1]
    assert query["lane"] == "draft" and query["limit"] == 500
    # 租户由真实 `_call` 统一 setdefault 补上；本用例替掉了 `_call`，
    # 所以这里只验「方法自己组了哪些参数」——不替自己的假实现声明租户行为。


def test_protocol_declares_the_two_lane_methods():
    """协议缺方法时，生产实现会静默少一个能力：这里按名字钉住。"""
    names = dir(KernelClient)
    for needed in ("submit_event", "query_events", "ingest_event"):
        assert needed in names, needed

"""门店主数据确认车道（任务 3.1 / 5.1）。"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.stores import StoreMasterData
from fd_erp_vertical_templates.errors import TemplateError


@pytest.fixture()
def md(tmp_path):
    return StoreMasterData("tpl-e2e", tmp_path / "data")


def test_store_requires_operation_mode(md):
    with pytest.raises(TemplateError) as ei:
        md.create_entity("store", {"name": "一号店"})
    assert "operation_mode" in ei.value.message


def test_create_confirm_lifecycle(md):
    req = md.create_entity("store", {"name": "一号店", "operation_mode": "franchise",
                                     "city": "杭州"})
    assert req["status"] == "pending"
    assert md.get_entity(req["entity_id"]) is None  # 未确认不生效
    md.confirm_request(req["request_id"], decided_by="owner")
    snap = md.get_entity(req["entity_id"])
    assert snap["payload"]["operation_mode"] == "franchise"  # 加盟标注
    md.set_lifecycle(req["entity_id"], "open", requested_by="agent")
    # lifecycle 也走确认车道
    pending = md.pending_requests()
    assert len(pending) == 1
    md.confirm_request(pending[0]["request_id"], decided_by="owner")
    assert md.get_entity(req["entity_id"])["state"] == "open"
    entities = md.list_entities(kind="store", operation_mode="franchise")
    assert len(entities) == 1


def test_reject_leaves_trace_without_effect(md):
    req = md.create_entity("store", {"name": "废店", "operation_mode": "direct"})
    md.reject_request(req["request_id"], "重复建档", decided_by="owner")
    assert md.get_entity(req["entity_id"]) is None
    # 拒绝留痕：请求档仍可查（append-only 档）
    events = md._events()
    assert any(e["request_id"] == req["request_id"] and e["status"] == "rejected"
               for e in events)


def test_closed_store_blocks_update_and_nonopen_states(md):
    req = md.create_entity("store", {"name": "二号店", "operation_mode": "direct"})
    md.confirm_request(req["request_id"])
    md.set_lifecycle(req["entity_id"], "open")
    md.confirm_request(md.pending_requests()[0]["request_id"])
    md.set_lifecycle(req["entity_id"], "closed")
    md.confirm_request(md.pending_requests()[0]["request_id"])
    assert md.get_entity(req["entity_id"])["state"] == "closed"
    with pytest.raises(TemplateError) as ei:
        md.update_entity(req["entity_id"], {"name": "改名"})
    assert ei.value.code == "MASTER_DATA_STATE"
    with pytest.raises(TemplateError) as ei:
        md.set_lifecycle(req["entity_id"], "suspended")
    assert "只能复业" in ei.value.message
    md.set_lifecycle(req["entity_id"], "open")  # 复业可行
    md.confirm_request(md.pending_requests()[0]["request_id"])


def test_pos_terminal_and_channel_accounts(md):
    store = md.create_entity("store", {"name": "三号店", "operation_mode": "franchise"})
    md.confirm_request(store["request_id"])
    term = md.create_entity("pos_terminal", {"store_id": store["entity_id"],
                                             "vendor": "export-file"})
    chan = md.create_entity("payment_channel_account",
                            {"store_id": store["entity_id"], "channel": "wechat-pay",
                             "mch_id": "1900000001"})
    deliv = md.create_entity("delivery_shop", {"store_id": store["entity_id"],
                                               "platform": "meituan"})
    for r in (term, chan, deliv):
        md.confirm_request(r["request_id"])
    kinds = {md.get_entity(r["entity_id"])["kind"] for r in (term, chan, deliv)}
    assert kinds == {"pos_terminal", "payment_channel_account", "delivery_shop"}

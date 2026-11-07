"""主数据三档测试（add-supply-chain-vertical 任务 2.1/2.2/2.3）。

覆盖：三档确认车道 / 编码唯一 / 多单位换算 / 证照预警与过期联动 / 商品池
多级组织与授权隔离 / 定制池。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.errors import TemplateError


@pytest.fixture()
def md(tmp_path):
    return MasterData("tpl-supply", tmp_path / "data")


def _confirm(md, req, by="owner"):
    return md.confirm_request(req["request_id"], decided_by=by)


def _product(md, code, *, supplier_id=None, **extra):
    payload = {"code": code, "name": f"商品{code}",
               "units": [{"unit": "件", "kind": "base", "rate": 1},
                         {"unit": "箱", "kind": "purchase", "rate": 24},
                         {"unit": "组", "kind": "sales", "rate": 6},
                         {"unit": "托", "kind": "warehouse", "rate": 144}],
               "barcode": "6900000001", "sku_specs": {"color": "black"},
               "base_price": "100.00"}
    if supplier_id:
        payload["supplier_id"] = supplier_id
    payload.update(extra)
    return _confirm(md, md.create_entity("product", payload))["entity_id"]


# ---- 三档确认车道 ----

def test_three_kinds_confirm_lane(md):
    req = md.create_entity("customer", {"code": "C001", "name": "锚点实例客户甲"})
    assert req["status"] == "pending"
    assert md.get_entity(req["entity_id"]) is None  # 未确认不生效
    _confirm(md, req)
    assert md.get_entity(req["entity_id"])["state"] == "active"

    sup = md.create_entity("supplier", {"code": "S001", "name": "锚点实例供应商乙",
                                        "direct_ship": False})
    _confirm(md, sup)
    prod = _product(md, "P001")
    kinds = {md.get_entity(e["entity_id"])["kind"]
             for e in md.list_entities()}
    assert kinds == {"customer", "supplier", "product"}


def test_code_unique_across_confirm(md):
    _confirm(md, md.create_entity("customer", {"code": "C001", "name": "客户甲"}))
    with pytest.raises(TemplateError) as ei:
        md.create_entity("customer", {"code": "C001", "name": "重复建档"})
    assert "编码已存在" in ei.value.message
    # 两笔 pending 同码：先确认者得，后确认者拒绝
    a = md.create_entity("supplier", {"code": "S9", "name": "先"})
    b = md.create_entity("supplier", {"code": "S9", "name": "后"})
    _confirm(md, a)
    with pytest.raises(TemplateError):
        _confirm(md, b)


def test_reject_leaves_trace(md):
    req = md.create_entity("customer", {"code": "CX", "name": "废档"})
    md.reject_request(req["request_id"], "重复", decided_by="owner")
    assert md.get_entity(req["entity_id"]) is None
    # 拒绝留痕：请求档仍可查（append-only 档）
    assert any(e["request_id"] == req["request_id"] and e["status"] == "rejected"
               for e in md._events())


# ---- 多单位换算 ----

def test_multi_unit_validation_and_conversion(md):
    with pytest.raises(TemplateError) as ei:
        md.create_entity("product", {"code": "PBad", "name": "坏商品",
                                     "units": [{"unit": "件", "kind": "base", "rate": 2}]})
    assert "恒为 1" in ei.value.message
    with pytest.raises(TemplateError):
        md.create_entity("product", {"code": "PBad2", "name": "缺基本单位",
                                     "units": [{"unit": "箱", "kind": "purchase", "rate": 24}]})
    eid = _product(md, "P002")
    assert eid
    # 件→箱：24 件 = 1 箱；箱→托：144/24 = 6 箱/托
    assert md.convert_qty("P002", 24, "件", "箱") == "1"
    assert md.convert_qty("P002", 1, "托", "箱") == "6"
    assert md.convert_qty("P002", 2.5, "箱", "件") == "60"
    with pytest.raises(TemplateError):
        md.convert_qty("P002", 1, "箱", "不存在单位")


# ---- 证照到期预警与联动 ----

def _supplier_with_cert(md, code, expires_at, cert_type="business_license"):
    return _confirm(md, md.create_entity("supplier", {
        "code": code, "name": f"供应商{code}",
        "certificates": [{"cert_type": cert_type, "expires_at": expires_at,
                          "status": "valid"}]}))["entity_id"]


def test_certificate_expiry_warnings(md):
    _supplier_with_cert(md, "S101", "2026-11-20")   # 13 天后到期
    _supplier_with_cert(md, "S102", "2027-06-30")   # 未到期
    _product(md, "P101", certificates=[
        {"cert_type": "inspection_report", "expires_at": "2026-11-01",
         "status": "valid"}])  # 已过期
    warnings = md.expiring_certificates(within_days=30, today="2026-11-07")
    expired = [w for w in warnings if w["level"] == "expired"]
    expiring = [w for w in warnings if w["level"] == "expiring"]
    assert [w["code"] for w in expired] == ["P101"]
    assert [w["code"] for w in expiring] == ["S101"]
    assert expiring[0]["days_left"] == 13


def test_expiry_linkage_product_stop_selling(md):
    pid = _product(md, "P201", certificates=[
        {"cert_type": "inspection_report", "expires_at": "2026-11-01",
         "status": "valid"}])
    sug = md.propose_expiry_control(pid, today="2026-11-07")
    assert sug["proposed"] is True and sug["target_state"] == "stop_selling"
    assert sug["linkage"]["trigger"] == "cert_expired"
    # 联动建议 pending：确认后才生效（不静默改状态）
    assert md.get_entity(pid)["state"] == "active"
    req = next(r for r in md.pending_requests()
               if r["request_id"] == sug["request_id"])
    assert req["event"] == "lifecycle"
    md.confirm_request(sug["request_id"], decided_by="owner")
    assert md.get_entity(pid)["state"] == "stop_selling"
    # 停售商品不得直接改档案（先解除管控态）
    with pytest.raises(TemplateError) as ei:
        md.update_entity(pid, {"name": "改名"})
    assert ei.value.code == "MASTER_DATA_STATE"
    # 留痕：lifecycle 请求带 linkage
    confirmed = [e for e in md._events()
                 if e["event"] == "lifecycle" and e["status"] == "confirmed"]
    assert any(e["payload"].get("linkage", {}).get("trigger") == "cert_expired"
               for e in confirmed)


def test_expiry_linkage_supplier_frozen(md):
    sid = _supplier_with_cert(md, "S301", "2026-10-01")
    sug = md.propose_expiry_control(sid, today="2026-11-07")
    assert sug["target_state"] == "frozen"
    md.confirm_request(sug["request_id"], decided_by="owner")
    assert md.get_entity(sid)["state"] == "frozen"


def test_expiry_linkage_no_op_when_valid(md):
    sid = _supplier_with_cert(md, "S401", "2027-12-31")
    sug = md.propose_expiry_control(sid, today="2026-11-07")
    assert sug["proposed"] is False


# ---- 商品池：多级组织 / 授权 / 定制 ----

def test_product_pool_org_and_authorization(md):
    p1 = _product(md, "P501")
    p2 = _product(md, "P502")
    pool = md.create_pool({"name": "华东标品池", "visibility": "org",
                           "org_path": "/集团/华东",
                           "product_codes": ["P501"]})
    _confirm(md, pool)
    auth_pool = md.create_pool({"name": "授权池", "visibility": "authorized",
                                "org_path": "/集团",
                                "product_codes": ["P502"]})
    _confirm(md, auth_pool)
    # org 池：华东子树可见，华北不可见
    east = md.visible_product_codes(org_node="/集团/华东/杭州")
    assert "P501" in east and "P502" not in east
    north = md.visible_product_codes(org_node="/集团/华北")
    assert north == []
    # authorized 池：授权客户可见，未授权不可见
    assert "P502" not in md.visible_product_codes(org_node="/集团", customer_id="C-X")
    req = md.authorize_pool(auth_pool["entity_id"], customers=["C-X"],
                            channels=["ch-wechat"])
    _confirm(md, req)
    assert "P502" in md.visible_product_codes(org_node="/集团", customer_id="C-X")
    assert "P502" in md.visible_product_codes(org_node="/集团", channel_id="ch-wechat")
    # 授权变更同样走确认车道（pending 留痕）
    assert any(e["event"] == "authorize" for e in md._events())


def test_custom_pool_visible_to_designated_only(md):
    _product(md, "P601")
    pool = md.create_pool({"name": "定制池", "visibility": "custom",
                           "org_path": "/集团",
                           "product_codes": ["P601"],
                           "allowed_customers": ["C-VIP"]})
    _confirm(md, pool)
    assert "P601" in md.visible_product_codes(customer_id="C-VIP")
    assert "P601" not in md.visible_product_codes(customer_id="C-OTHER")
    with pytest.raises(TemplateError):
        md.create_pool({"name": "坏定制池", "visibility": "custom",
                        "org_path": "/集团", "product_codes": ["P601"]})

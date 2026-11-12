"""CRM 线索/拜访车道测试（add-crm-leads-visits 1.1-1.6）。

五条必须立的判据：
  1. 归属：记录永远带 owner_staff，查询只返回本人的；他人客户/线索与「不存在」同码同文案。
  2. 拜访纪要必填、主体至少一个——「谁也没见的拜访」没有业务意义。
  3. 定位只存调用方给的引用：没给就连 location_ref 这个键都不出现（「没有位置」
     与「位置未知」在数据上可区分），服务端不采集不补默认。
  4. 新建线索只能是 new：推进没有车道，允许直接建 following 等于跳过那次真实拜访。
  5. 全程不写事件层——CRM 记录不是事件层事实（lead 系名字在注册侧被黑名单拒绝）。

第 5 条要能红：把 `ingest_event` 注入 `create_lead` 一次，对应用例必须失败。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.orchestration import (CRM_LEAD_STATUSES,
                                                     CrmOrchestrator)

S1, S2 = "S-001", "S-002"


@pytest.fixture()
def crm(tmp_path):
    """两位业务员，各自名下一位客户；客户归属靠主档 sales_rep。"""
    data = tmp_path / "data"
    m = MasterData("tpl-supply", data)
    cust_a = m.confirm_request(m.create_entity(
        "customer", {"code": "C-A", "name": "甲业务员的客户",
                     "sales_rep": S1})["request_id"],
        decided_by="owner")["entity_id"]
    cust_b = m.confirm_request(m.create_entity(
        "customer", {"code": "C-B", "name": "乙业务员的客户",
                     "sales_rep": S2})["request_id"],
        decided_by="owner")["entity_id"]
    return {"crm": CrmOrchestrator("tpl-supply", data, m),
            "cust_a": cust_a, "cust_b": cust_b}

# ---- 1.1/1.2 建线索 ----

def test_lead_is_created_with_staff_prefix_and_default_status(crm):
    c = crm["crm"]
    out = c.create_lead(S1, {"name": "某连锁超市", "phone": "13800000000",
                             "company": "某某商贸", "source": "展会"})
    assert out["leadId"] == "LD-S001-000001"
    assert out["status"] == "new"
    assert out["owner_staff"] == S1

    pool = c.leads_of(S1)
    assert pool["total"] == 1
    row = pool["rows"][0]
    assert row["name"] == "某连锁超市" and row["company"] == "某某商贸"
    assert row["source"] == "展会" and row["phone"] == "13800000000"


def test_lead_requires_name_and_rejects_advanced_status(crm):
    c = crm["crm"]
    with pytest.raises(TemplateError) as ei:
        c.create_lead(S1, {"phone": "13800000000"})
    assert ei.value.code == "INVALID_ARGUMENT" and "name" in ei.value.message

    for bad in ("following", "converted", "dropped", "won"):
        with pytest.raises(TemplateError) as ei:
            c.create_lead(S1, {"name": "x", "status": bad})
        assert ei.value.code == "INVALID_ARGUMENT", bad
    assert c.leads_of(S1)["total"] == 0, "失败分支不得落库"


def test_lead_numbers_are_per_staff(crm):
    c = crm["crm"]
    assert c.create_lead(S1, {"name": "甲1"})["leadId"] == "LD-S001-000001"
    assert c.create_lead(S1, {"name": "甲2"})["leadId"] == "LD-S001-000002"
    assert c.create_lead(S2, {"name": "乙1"})["leadId"] == "LD-S002-000001"


def test_lead_pool_is_scoped_and_filterable(crm):
    c = crm["crm"]
    c.create_lead(S1, {"name": "甲的线索"})
    c.create_lead(S2, {"name": "乙的线索"})
    mine = c.leads_of(S1)
    assert mine["total"] == 1 and mine["rows"][0]["name"] == "甲的线索"
    assert c.leads_of(S1, status="new")["total"] == 1
    assert c.leads_of(S1, status="converted")["total"] == 0
    assert c.leads_of(S1, offset=1)["returned"] == 0


# ---- 1.3/1.4 拜访 ----

def test_visit_requires_note_and_subject(crm):
    c = crm["crm"]
    with pytest.raises(TemplateError) as ei:
        c.record_visit(S1, customer_id=crm["cust_a"], note="  ")
    assert "纪要" in ei.value.message
    with pytest.raises(TemplateError) as ei:
        c.record_visit(S1, note="谈得挺好")
    assert "客户或线索" in ei.value.message
    assert c.visits_of(S1)["total"] == 0


def test_visit_foreign_customer_is_same_404_as_missing(crm):
    c = crm["crm"]
    with pytest.raises(TemplateError) as ei1:
        c.record_visit(S1, customer_id=crm["cust_b"], note="跑错门了")
    with pytest.raises(TemplateError) as ei2:
        c.record_visit(S1, customer_id="C-NOT-EXIST", note="不存在")
    assert ei1.value.code == ei2.value.code == "NOT_FOUND"
    assert ei1.value.message.replace(crm["cust_b"], "<ref>") == \
        ei2.value.message.replace("C-NOT-EXIST", "<ref>")
    assert not any(w in ei1.value.message for w in ("他人", "无权", "不属于"))


def test_visit_foreign_lead_is_404(crm):
    c = crm["crm"]
    other = c.create_lead(S2, {"name": "乙的线索"})["leadId"]
    with pytest.raises(TemplateError) as ei:
        c.record_visit(S1, lead_ref=other, note="越域")
    assert ei.value.code == "NOT_FOUND"


def test_visit_without_location_omits_the_key(crm):
    c = crm["crm"]
    c.record_visit(S1, customer_id=crm["cust_a"], note="送样")
    row = c.visits_of(S1)["rows"][0]
    assert row["location"] is None
    raw = [e for e in c._events() if e.get("record") == "visit"][0]
    assert "location_ref" not in raw["payload"], (
        "没给定位时连键都不该出现——否则「没有位置」被写成「位置为空」")


def test_visit_with_location_stores_reference_verbatim(crm):
    c = crm["crm"]
    out = c.record_visit(S1, customer_id=crm["cust_a"], note="现场",
                         location="geo:30.2741,120.1551")
    assert out["location"] == "geo:30.2741,120.1551"
    raw = [e for e in c._events() if e.get("record") == "visit"][0]
    assert raw["payload"]["location_ref"] == "geo:30.2741,120.1551"


def test_visit_records_day_and_filters_by_period(crm):
    c = crm["crm"]
    c.record_visit(S1, customer_id=crm["cust_a"], note="第一次")
    day = c.visits_of(S1)["rows"][0]["visited_at"][:10]
    assert c.visits_of(S1, date_from=day, date_to=day)["total"] == 1
    assert c.visits_of(S1, date_from="2099-01-01")["total"] == 0
    assert c.today_visit_count(S1, day) == 1
    assert c.today_visit_count(S1, "2099-01-01") == 0
    # 另一位业务员数不到别人的拜访
    assert c.today_visit_count(S2, day) == 0


def test_visits_are_scoped_to_staff(crm):
    c = crm["crm"]
    c.record_visit(S1, customer_id=crm["cust_a"], note="甲的拜访")
    c.record_visit(S2, customer_id=crm["cust_b"], note="乙的拜访")
    assert c.visits_of(S1)["total"] == 1
    assert c.visits_of(S1)["rows"][0]["note"] == "甲的拜访"
    assert c.visits_of(S2)["rows"][0]["note"] == "乙的拜访"


# ---- 1.6 CRM 记录不是事件层事实 ----

def test_crm_lane_has_no_kernel_write_path(crm):
    """结构守卫：CRM 车道里不存在任何通往事件层的路径。

    先前版本往 fixture 里塞一个 RecordingKernel 就断言它「没被调用」——但它从未
    接进 CrmOrchestrator，那条断言永远绿（空转）。这里改判两件真正可被注入推翻的
    事实：类源码里没有内核写入调用；实例依赖只有租户/主档/编排档三样。
    注入一次 `self._kernel.ingest_event(...)` 到 create_lead，本用例即红。
    端到端的「事件条数不变」由 `fd-erp-vertical-api/tests/test_crm_api.py` 实测。
    """
    import inspect

    src = inspect.getsource(CrmOrchestrator)
    assert "ingest_event" not in src
    assert "submit_event" not in src
    assert set(vars(crm["crm"])) == {"_tenant", "_master", "_log"}, vars(crm["crm"])


def test_crm_writes_only_orchestration_log(crm):
    """写的是编排档：一条 lead、一条 visit 各落一行，文件仍是 JSONL 事件档。"""
    c = crm["crm"]
    c.create_lead(S1, {"name": "线索"})
    c.record_visit(S1, customer_id=crm["cust_a"], note="纪要")
    records = [e.get("record") for e in c._events()]
    assert records == ["lead", "visit"]


def test_lead_statuses_constant_is_the_contract_set():
    assert CRM_LEAD_STATUSES == ("new", "following", "converted", "dropped")

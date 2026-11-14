"""全链 e2e（add-supply-chain-vertical 任务 8.2 可测面）：一条合成全链
（租户内、经 InProcess 内核、真库 erp_test_b）：

三主档建档确认 → 采购需求 → PO 确认 → 到货预告 → 入库任务 → 订单受理
（信用 reserve → 取价 → 订单确认）→ 履约两路径各一笔（越库直发 + 仓内出库）
→ 出库落账 → 入库/出库事件计费（bill_event → 费用明细）→ 日结费用 proposal
确认落账 → 应收/应付记账（1300/4000 等白名单科目，经确认门）→ 金蝶凭证推送 +
日终对账（recon.kingdee 结算腿）→ 断言：各段可计数（分录数/费用数/差异清单数/
额度占用回冲）、全程零不平分录、append-only 无 UPDATE/DELETE（分录快照只增不改）、
最终余额与手工累加一致。

模板运行时账务只经 KernelClient 工具面；本测试扮演账务面（proposal 确认门）
与内核运维面（信用额度登记 set_credit_limit 为模块级 API，非工具面）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.fulfillment import (
    DemoLogisticsAdapter, FulfillmentOrchestrator)
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.orchestration import (
    OrderOrchestrator, ProcurementOrchestrator)
from fd_erp_vertical_templates.pricing import PricingRuleBook
from fd_erp_vertical_templates.recon.kingdee import DemoKingdeeGateway, SettlementRecon
from fd_erp_vertical_templates.wms_sync import DemoWmsAdapter

#: 夹具业务日来自 `book_day` 夹具（= 内核 `book_today` 的同一算法）。
#: **不能写 `datetime.now(timezone.utc).date()` 自己算**：账面时区是 Asia/Shanghai，
#: UTC 16:00 之后两者差一天，按业务日收凭证就会收到空集（2026-11-15 凌晨实测红）；
#: 也不能写死成具体某天（2026-11-12 已有一次「全链 voucher_count 5 → 0」的实历）。


def _post(kernel, tenant: str, stable_id: str, lines: list[dict],
          source_ref: str, *, day: str) -> str:
    """账务面：提交 proposal → 确认门落账，返回事务号。

    `booked_on` 显式传：账期是业务日期，不该绑在运行那一刻的时钟上。
    """
    prop = kernel.submit_proposal(stable_id=stable_id, lines=lines,
                                  source_ref=source_ref, tenant_id=tenant,
                                  booked_on=day)
    confirmed = kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                        confirmed_by="owner")
    assert confirmed["status"] == "confirmed"
    return confirmed["txn_id"]


def _count_entries(kernel, tenant: str) -> list[dict]:
    return list(kernel.query_entries(tenant_id=tenant)["entries"])


def test_supply_full_chain_e2e(kernel, reset_db, db_settings, tmp_path, book_day):
    tenant = reset_db.tenant_id
    DAY = book_day
    counts: dict[str, int] = {}

    # ---- 段 0：三主档建档确认（客户/供应商×2/商品×2，确认车道）----
    from fd_erp_ledger_mcp import credit as kernel_credit
    from fd_erp_ledger_mcp.db import connection

    md = MasterData(tenant, tmp_path / "data")
    cid = md.confirm_request(
        md.create_entity("customer", {"code": "C-E2E", "name": "锚点客户"})[
            "request_id"], decided_by="owner")["entity_id"]
    s_wh = md.confirm_request(
        md.create_entity("supplier", {"code": "S-WH", "name": "仓配供应商",
                                      "direct_ship": False})["request_id"],
        decided_by="owner")["entity_id"]
    s_ds = md.confirm_request(
        md.create_entity("supplier", {"code": "S-DS", "name": "直发供应商",
                                      "direct_ship": True})["request_id"],
        decided_by="owner")["entity_id"]
    for code, price, supplier in (("P-DS", "100.00", s_ds),
                                  ("P-WH", "60.00", s_wh)):
        md.confirm_request(
            md.create_entity("product", {
                "code": code, "name": f"商品{code}",
                "units": [{"unit": "件", "kind": "base", "rate": 1},
                          {"unit": "箱", "kind": "purchase", "rate": 24}],
                "base_price": price, "supplier_id": supplier})["request_id"],
            decided_by="owner")
    assert len(md.list_entities(kind="customer", state="active")) == 1
    assert len(md.list_entities(kind="supplier", state="active")) == 2
    assert len(md.list_entities(kind="product", state="active")) == 2
    counts["masterdata_entities"] = 5

    # ---- 段 1：采购需求 → 汇总 → PO 确认 → 推送留痕 → 到货预告 → 入库任务 ----
    po_board = ProcurementOrchestrator(tenant, tmp_path / "data", md)
    demand = po_board.submit_demand("replenishment",
                                    [{"product_code": "P-WH", "qty": 24,
                                      "unit": "件"}])
    agg = po_board.aggregate_demand([demand["ref"]])
    assert agg["lines"][0]["qty"] == "1"  # 24 件 → 1 箱（采购单位换算）
    po = po_board.propose_purchase_order(agg["ref"], s_wh)
    assert po["status"] == "pending"
    confirmed_po = po_board.confirm_purchase_order(po["request_id"],
                                                   decided_by="owner")
    assert confirmed_po["status"] == "confirmed"
    push = po_board.record_push(po["ref"], channel="supplier-portal")
    assert push["status"] == "pushed"  # 推送留痕（实际推送经连接器）
    arrival = po_board.record_arrival_notice(po["ref"], [
        {"product_code": "P-WH", "qty": "1", "unit": "箱"}],
        expected_day=DAY)
    inbound_task = po_board.create_inbound_task(arrival["ref"], "WH-EAST")
    assert inbound_task["warehouse"] == "WH-EAST"
    counts["procurement"] = 5  # demand/aggregate/PO/push/arrival(+task)

    # ---- 段 2：入库落账（账务面：借 1200 存货 / 贷 2000 应付，确认门）----
    txn_inbound = _post(
        kernel, tenant, "e2e-inbound-001",
        [{"account_code": "1200", "direction": "debit", "amount": "1440.00",
          "currency": "CNY"},
         {"account_code": "2000", "direction": "credit", "amount": "1440.00",
          "currency": "CNY"}],
        source_ref="inv:P-WH:WH-EAST", day=DAY)
    assert len(_count_entries(kernel, tenant)) == 2

    # ---- 段 3：订单受理（信用校验 → 取价 → 信用占用 → 订单确认）----
    with connection(db_settings) as conn:  # 内核运维面：额度登记（非工具面 API）
        kernel_credit.set_credit_limit(conn, tenant_id=tenant, customer_id=cid,
                                       limit_amount="1000", currency="CNY",
                                       source_ref="credit-policy/e2e-2026")
    book = PricingRuleBook(tenant, tmp_path / "data", master=md)
    rule_req = book.add_rule({
        "layer": "customer_price",
        "scope": {"product_code": "P-DS", "customer_id": cid},
        "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "price": "95.00", "rate_source": "policy/customer-C-E2E/2026"})
    book.confirm_rule(rule_req["request_id"])

    order_board = OrderOrchestrator(tenant, tmp_path / "data", kernel, md, book)
    order = order_board.accept_order(cid, [
        {"product_code": "P-DS", "qty": 2, "unit": "件"},   # 95×2=190（客户价）
        {"product_code": "P-WH", "qty": 5, "unit": "件"}],  # 60×5=300（基础价）
        DAY)
    assert order["accepted"] is True
    assert order["amount_total"] == "490"
    assert {pl["product"]: pl["rule_chain"] for pl in order["priced_lines"]}[  # 取价溯源
        "P-DS"][0]["layer"] == "customer_price"
    res_stable = order["credit_reservation"]["stable_id"]
    pos = kernel.query_credit(cid)
    assert pos["reserved"] == "490.000000"          # 信用占用已记账
    assert pos["available"] == "510.000000"
    confirmed_order = order_board.confirm_order(order["request_id"],
                                                decided_by="owner")
    assert confirmed_order["status"] == "confirmed"
    order_ref = confirmed_order["order_ref"]
    handoff_ref = confirmed_order["handoff_ref"]
    counts["order"] = 1

    # ---- 段 4：履约两路径（直发 + 仓内出库）+ 运单/签收回流 ----
    wms = DemoWmsAdapter([{"product_code": "P-WH", "warehouse": "WH-EAST",
                           "qty": "24", "status": "available",
                           "occurred_at": f"{DAY}T08:00:00"}])
    logistics = DemoLogisticsAdapter(
        tracks={"WB-E2E-001": [{"at": f"{DAY}T09:00:00", "note": "已揽收"},
                               {"at": f"{DAY}T11:00:00", "note": "派送中"}]},
        pods={"WB-E2E-001": {"signed_by": "锚点客户",
                             "at": f"{DAY}T12:00:00"}})
    ff = FulfillmentOrchestrator(tenant, tmp_path / "data", md, wms, logistics)
    plan = ff.plan_fulfillment(handoff_ref, order_ref, cid, [
        {"product_code": "P-DS", "qty": "2"},
        {"product_code": "P-WH", "qty": "5"}])
    paths = {ln["product_code"]: ln["path"] for ln in plan["lines"]}
    assert paths == {"P-DS": "dropship", "P-WH": "warehouse"}  # 两路径各一笔
    wh_line = next(ln for ln in plan["lines"] if ln["product_code"] == "P-WH")
    assert wh_line["allocations"] == [{"warehouse": "WH-EAST", "qty": "5"}]
    ff.record_waybill(plan["ref"], "WB-E2E-001", "carrier-x")
    track = ff.track_waybill("WB-E2E-001")
    assert len(track["events"]) == 2
    ff.record_pod("WB-E2E-001")
    view = ff.fulfillment_view(order_ref)
    assert len(view["plans"][0]["dropship_instructions"]) == 1
    assert len(view["plans"][0]["outbound_instructions"]) == 1
    assert view["plans"][0]["waybills"][0]["pod"]["signed_by"] == "锚点客户"
    counts["fulfillment_instructions"] = 2

    # ---- 段 5：出库落账（借 5000 营业成本 / 贷 1200 存货 5×60）----
    txn_cogs = _post(
        kernel, tenant, "e2e-cogs-001",
        [{"account_code": "5000", "direction": "debit", "amount": "300.00",
          "currency": "CNY"},
         {"account_code": "1200", "direction": "credit", "amount": "300.00",
          "currency": "CNY"}],
        source_ref="inv:P-WH:WH-EAST", day=DAY)
    entries_after_ops = _count_entries(kernel, tenant)
    assert len(entries_after_ops) == 4
    # append-only 快照（此后任何阶段不得改写既有分录，只许追加）
    snapshot = sorted((e["txn_id"], e["line_no"], e["amount"], e["amount_base"])
                      for e in entries_after_ops)

    # ---- 段 6：入库/出库事件计费（bill_event → 费用明细 → 日结确认落账）----
    for item, kind, price in (("inbound-handling", "inbound", "1.00"),
                              ("outbound-handling", "outbound", "1.50")):
        rule = kernel.submit_billing_rule(
            {"fee_item": item, "event_kind": kind, "mode": "unit",
             "unit_price": price, "measure": "quantity", "fee_type": "revenue",
             "rule_key": item, "rate_source": "价目表/仓费-2026-001"},
            source_ref="价目表/仓费-2026-001")
        approved = kernel.approve_billing_rule(rule["rule_id"], rule["version"])
        assert approved["status"] == "active"
    bill_in = kernel.bill_event({
        "stable_id": "e2e-evt-inbound-001", "kind": "inbound",
        "occurred_at": DAY, "measures": {"quantity": 24},
        "refs": {"inbound_task": inbound_task["ref"], "po_ref": po["ref"]}})
    assert bill_in["duplicate"] is False
    assert Decimal(bill_in["fees"][0]["amount"]) == Decimal("24")  # 1.00×24
    bill_out = kernel.bill_event({
        "stable_id": "e2e-evt-outbound-001", "kind": "outbound",
        "occurred_at": DAY, "measures": {"quantity": 5},
        "refs": {"order_ref": order_ref}})
    assert Decimal(bill_out["fees"][0]["amount"]) == Decimal("7.5")  # 1.50×5
    dup = kernel.bill_event({  # 幂等：同事件重推返回首次结果（不重不漏）
        "stable_id": "e2e-evt-inbound-001", "kind": "inbound",
        "occurred_at": DAY, "measures": {"quantity": 24}, "refs": {}})
    assert dup["duplicate"] is True and len(dup["fees"]) == 1
    fee_ids = [bill_in["fees"][0]["fee_id"], bill_out["fees"][0]["fee_id"]]
    for fee_id in fee_ids:  # 日结费用 proposal → 确认门落账（1300/4000 机器车道）
        detail = kernel.get_fee_detail(fee_id)
        assert detail["snapshot_replay"]["matches"] is True   # 快照重算一致
        assert detail["chain"]["rule"]["rate_source"] == "价目表/仓费-2026-001"
        assert detail["chain"]["document"]  # 四层溯源：业务单据引用在链
        kernel.confirm_proposal(proposal_id=detail["fee"]["journal_proposal_id"],
                                confirmed_by="owner")
    fees = kernel.list_fees()["fees"]
    assert len(fees) == 2
    counts["fees"] = 2

    # ---- 段 7：应收记账（1300/4000 白名单）→ 信用占用回冲 ----
    txn_revenue = _post(
        kernel, tenant, f"e2e-revenue-{order_ref}",
        [{"account_code": "1300", "direction": "debit", "amount": "490.00",
          "currency": "CNY"},
         {"account_code": "4000", "direction": "credit", "amount": "490.00",
          "currency": "CNY"}],
        source_ref=f"order:{order_ref}", day=DAY)
    release = kernel.release_credit(cid, res_stable)
    assert release["ok"] is True and release["already_released"] is False
    pos2 = kernel.query_credit(cid)
    assert pos2["reserved"] == "0.000000"           # 额度占用回冲
    assert pos2["ar_balance"] == "521.500000"       # 490 收入 + 24 + 7.5 仓费
    assert pos2["available"] == "478.500000"        # 1000 − 521.5 − 0

    # ---- 段 8：金蝶凭证推送 + 日终双边对账（结算腿，任务 A 模块）----
    gateway = DemoKingdeeGateway()
    settlement = SettlementRecon(tenant, kernel, gateway)
    close = settlement.run_settlement_close(DAY, tenant_id=tenant)
    assert close["push"]["voucher_count"] == 5  # 入库/成本/收入/仓费×2 凭证面
    assert close["push"]["duplicate_count"] == 0
    recon = close["recon"]
    assert recon["matched_count"] == 5
    assert recon["diffs"] == []                  # 全链自洽：差异清单为零
    assert len(close["writeback_proposals"]) == 1  # 零差异 → 确认记录回写
    assert close["writeback_proposals"][0]["status"] == "pending"
    counts["settlement_vouchers"] = 5
    counts["settlement_diffs"] = 0

    # ---- 段 9：全程断言 ----
    # 9a) 各段可计数汇总
    assert counts == {"masterdata_entities": 5, "procurement": 5, "order": 1,
                      "fulfillment_instructions": 2, "fees": 2,
                      "settlement_vouchers": 5, "settlement_diffs": 0}

    # 9b) 分录数 + 零不平分录（逐事务借==贷，本位币口径）
    entries = _count_entries(kernel, tenant)
    assert len(entries) == 10  # 5 张凭证 × 2 行
    by_txn: dict[str, list[dict]] = {}
    for e in entries:
        by_txn.setdefault(e["txn_id"], []).append(e)
    assert len(by_txn) == 5
    for txn_id, lines in by_txn.items():
        debit = sum(Decimal(str(l["amount_base"])) for l in lines
                    if l["direction"] == "debit")
        credit = sum(Decimal(str(l["amount_base"])) for l in lines
                     if l["direction"] == "credit")
        assert debit == credit, f"不平分录：{txn_id}"
        assert all(e["is_reversal"] is False for e in lines)
    assert {txn_inbound, txn_cogs, txn_revenue} <= set(by_txn)

    # 9c) append-only：操作后快照逐行原样仍在（无 UPDATE/DELETE 路径实证）
    final_snapshot = {(e["txn_id"], e["line_no"], e["amount"], e["amount_base"])
                      for e in entries}
    for row in snapshot:
        assert row in final_snapshot  # 既有分录零改动，只增不改

    # 9d) 最终余额与手工累加一致（query_balance vs 分录逐行累加）
    manual: dict[str, dict[str, Decimal]] = {}
    for e in entries:
        code = e["account_code"]
        bucket = manual.setdefault(code, {"debit": Decimal(0),
                                          "credit": Decimal(0)})
        bucket["debit" if e["direction"] == "debit" else "credit"] += \
            Decimal(str(e["amount_base"]))
    balances = kernel.query_balance(tenant_id=tenant)["data"]
    assert {b["account_code"] for b in balances} == set(manual)
    for b in balances:
        m = manual[b["account_code"]]
        assert Decimal(b["balance"]) == m["debit"] - m["credit"]
        assert Decimal(b["debit_total"]) == m["debit"]
        assert Decimal(b["credit_total"]) == m["credit"]
    by_code = {b["account_code"]: b for b in balances}
    assert Decimal(by_code["1200"]["balance"]) == Decimal("1140")    # 1440−300
    assert Decimal(by_code["2000"]["balance"]) == Decimal("-1440")   # 应付
    assert Decimal(by_code["1300"]["balance"]) == Decimal("521.5")   # 应收
    assert Decimal(by_code["4000"]["balance"]) == Decimal("-521.5")  # 收入
    assert Decimal(by_code["5000"]["balance"]) == Decimal("300")     # 成本

    # 9e) 确认记录未落账（pending）：余额不受影响；确认门只属拍板岗
    assert close["writeback_proposals"][0]["status"] == "pending"


def test_supply_chain_credit_fail_closed_in_chain(kernel, reset_db,
                                                  db_settings, tmp_path,
                                                  book_day):
    """全链守界补例：额度不足 fail-closed 拒单（不落订单、不占额度、可解释）。"""
    from fd_erp_ledger_mcp import credit as kernel_credit
    from fd_erp_ledger_mcp.db import connection

    md = MasterData(reset_db.tenant_id, tmp_path / "data")
    cid = md.confirm_request(
        md.create_entity("customer", {"code": "C-POOR", "name": "小额度客户"})[
            "request_id"], decided_by="owner")["entity_id"]
    s = md.confirm_request(
        md.create_entity("supplier", {"code": "S-P", "name": "供应商"})[
            "request_id"], decided_by="owner")["entity_id"]
    md.confirm_request(
        md.create_entity("product", {
            "code": "P-P", "name": "商品P",
            "units": [{"unit": "件", "kind": "base", "rate": 1}],
            "base_price": "100.00", "supplier_id": s})["request_id"],
        decided_by="owner")
    with connection(db_settings) as conn:
        kernel_credit.set_credit_limit(conn, tenant_id=reset_db.tenant_id,
                                       customer_id=cid, limit_amount="50",
                                       currency="CNY",
                                       source_ref="credit-policy/e2e-2026")
    board = OrderOrchestrator(reset_db.tenant_id, tmp_path / "data", kernel, md,
                              PricingRuleBook(reset_db.tenant_id,
                                              tmp_path / "data", master=md))
    out = board.accept_order(cid, [{"product_code": "P-P", "qty": 1,
                                    "unit": "件"}], book_day)  # 100 > 50
    assert out["accepted"] is False and out["stage"] == "credit_check"
    assert out["explanation"]["shortfall"] == "50"
    assert board.pending_requests() == []
    from fd_erp_ledger_mcp.errors import ErpError
    with pytest.raises(ErpError) as ei:
        kernel.query_credit("no-such-customer")  # 未登记额度如实 NOT_FOUND
    assert ei.value.code == "NOT_FOUND"

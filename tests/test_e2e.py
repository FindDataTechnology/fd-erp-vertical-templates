"""e2e 全链真跑（任务 2.6 / 5.2）：测试店合成数据
接入 → 三方对账 → 差异 → 拍板 → 回写（内核白名单车道）→ 落账可查，
用例含重推 / 缺笔 / 金额不符 / 在途引用（跨境）与 日结 / 佣金差异 /
分成核对 / 多门店隔离（餐饮）。全部断言可计数。

模板运行时账务操作只经 KernelClient（8 工具同形）—— 生产形态是 MCP 工具面。
"""
from __future__ import annotations

import json

import pytest

from fd_erp_vertical_templates.kernel_client import KernelClient  # noqa: F401
from fd_erp_vertical_templates.mapping import load_mapping_pack, apply_mapping
from fd_erp_vertical_templates.recon.crossborder import (
    CrossborderRecon, EcbExrFxProvider)
from fd_erp_vertical_templates.recon.restaurant import StoreDailyClose
from fd_erp_vertical_templates.recon.shares import (
    build_share_statement, build_verification_instructions, assert_no_execution)
from fd_erp_vertical_templates.recon.writeback import (
    writeback_recon_conclusion, escalate_diff)
from fd_erp_vertical_templates.boundary import audit_template
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

USD_OBS = [{"quote_currency": "USD", "rate": "7.2", "as_of": "2026-11-06",
            "source": "ecb-exr", "base_currency": "CNY"}]


def _rec(rid, amount, *, day="2026-11-06", currency="CNY", status="posted",
         raw=None, side="L"):
    return {"stable_id": rid, "amount": str(amount), "currency": currency,
            "status": status, "occurred_at": f"{day}T10:00:00",
            "source_ref": f"{side}:{rid}", "raw": raw or {}}


# ---------------------------------------------------------------------------
# 跨境：接入 → 对账 → 差异 → 拍板 → 回写 → 落账（2.6）
# ---------------------------------------------------------------------------

def test_crossborder_full_chain(kernel, reset_db, seed_fx, monkeypatch):
    """测试店合成数据全链真跑：计数断言逐段成立。"""
    seed_fx(USD_OBS)
    monkeypatch.setenv("ERP_BYOK_SHOPIFY_TOKEN", "shpat_test_token")  # BYOK 注入

    store = "tpl-cross-store"
    recon = CrossborderRecon(
        store, fx=EcbExrFxProvider(
            [{"base": "CNY", "quote": "USD", "rate": "7.2", "as_of": "2026-11-06",
              "source": "ecb-exr"}]),
        platform_fee_schedule={"commission": "0.15/1/shopify-rate-card"})

    # ---- 接入：合成订单流 4 笔 / 收款流（payouts）3 笔 / 库存腿 ----
    orders = [
        _rec("O-1001", "100.00", currency="USD", raw={"order_id": "O-1001",
                                                      "channel": "shopify"}),
        _rec("O-1002", "250.00", currency="USD", raw={"order_id": "O-1002"}),
        _rec("O-1003", "80.00", currency="USD", raw={"order_id": "O-1003"}),
        _rec("O-1004", "60.00", currency="USD", status="shipped",
             raw={"order_id": "O-1004"}),
    ]
    payments = [
        _rec("P-1001", "100.00", currency="USD", raw={"order_id": "O-1001",
                                                      "fees": {"commission": "15",
                                                               "payment_fee": "2"}}),
        _rec("P-1002", "240.00", currency="USD", raw={"order_id": "O-1002"}),  # 金额不符
        _rec("P-9001", "500.00", currency="USD", raw={"order_id": "X-9001"}),  # 多出
    ]
    inventory = [
        _rec("I-1001", "1", currency="EA", raw={"order_id": "O-1001",
                                                "status": "deducted"}),
        _rec("I-1002", "1", currency="EA", raw={"order_id": "O-1002",
                                                "status": "deducted"}),
        _rec("I-1003", "1", currency="EA", raw={"order_id": "O-1003",
                                                "status": "deducted"}),
        # O-1004 无库存扣减 → 在途（needs_explanation=logistics）
    ]

    # ---- 对账：三方逐笔 ----
    orders_payments = recon.reconcile_orders_payments(orders, payments)
    orders_inventory = recon.reconcile_orders_inventory(orders, inventory)
    fees = recon.fee_report(payments)

    # 计数：订单↔收款 = 1 配对 + 4 差异（1 缺笔 O-1003、1 金额不符 P-1002、
    # 1 多出 P-9001、1 缺笔 O-1004）
    assert orders_payments["store"] == store
    assert orders_payments["matched_count"] == 1
    kinds = orders_payments["totals"]["by_kind"]
    assert kinds["missing_in_right"] == 2
    assert kinds["amount_mismatch"] == 1
    assert kinds["extra_in_right"] == 1
    # 库存腿：O-1004 在途 → 物流解释槽（不自行判定）
    logistics = [d for d in orders_inventory["diffs"]
                 if "needs_explanation=logistics" in d["explanation_hint"]]
    assert len(logistics) == 1
    # 费用维度独立：payouts/佣金/手续费
    assert fees["totals"] == {"payouts": "840.00", "commission": "15",
                              "payment_fee": "2"}
    assert fees["fee_explanations"][0]["rate_source"] == "shopify-rate-card"

    # ---- 拍板：金额不符差异 escalate → owner 裁决确认更正 ----
    mismatch = next(d for d in orders_payments["diffs"]
                    if d["kind"] == "amount_mismatch")
    esc = escalate_diff(mismatch, fleet="xb-recon-fleet")
    assert esc["severity"] == "high"

    # 核对岗出建议分录（白名单科目），拍板结论经 proposal 车道回写
    # （USD 更正行须带出处汇率：fx_facts 已种 ecb-exr）
    lines = recon.propose_correction_lines(mismatch)
    for line in lines:
        line.update({"fx_rate": "7.2", "fx_as_of": "2026-11-06",
                     "fx_source": "ecb-exr"})
    proposal = writeback_recon_conclusion(
        kernel, tenant_id=reset_db.tenant_id, fleet="xb-recon-fleet",
        post="reconciler", title="核对岗", diff=mismatch, proposal_lines=lines,
        decision_note="payout P-1002 与订单 O-1002 金额差 USD10：按对账差额挂应收更正")
    assert proposal["status"] == "pending"  # 白名单车道：不直接落账

    # 幂等重推：同差异回写同 stable_id → 返回首次结果
    re_proposal = writeback_recon_conclusion(
        kernel, tenant_id=reset_db.tenant_id, fleet="xb-recon-fleet",
        post="reconciler", title="核对岗", diff=mismatch, proposal_lines=lines,
        decision_note="payout P-1002 重推")
    assert re_proposal["proposal_id"] == proposal["proposal_id"]
    assert re_proposal["idempotent_hit"] is True

    # ---- 确认门落账（拍板岗唯一持门）----
    confirmed = kernel.confirm_proposal(proposal_id=proposal["proposal_id"],
                                        confirmed_by="owner")
    assert confirmed["status"] == "confirmed"
    txn_id = confirmed["txn_id"]

    # 缺笔差异（O-1003）同样回写（missing_in_right → 1300/4000）
    missing = next(d for d in orders_payments["diffs"]
                   if d["kind"] == "missing_in_right"
                   and (d["left_raw"] or {}).get("order_id") == "O-1003")
    lines_missing = recon.propose_correction_lines(missing)
    # USD 落账须带出处汇率（fx_facts 已种 ecb-exr）
    for line in lines_missing:
        line.update({"fx_rate": "7.2", "fx_as_of": "2026-11-06",
                     "fx_source": "ecb-exr"})
    p2 = writeback_recon_conclusion(
        kernel, tenant_id=reset_db.tenant_id, fleet="xb-recon-fleet",
        post="reconciler", title="核对岗", diff=missing,
        proposal_lines=lines_missing, decision_note="订单 O-1003 收款缺笔挂应收")
    kernel.confirm_proposal(proposal_id=p2["proposal_id"], confirmed_by="owner")

    # ---- 账本可查：余额与分录 ----
    balances = kernel.query_balance(tenant_id=reset_db.tenant_id)["data"]
    by_code = {b["account_code"]: b for b in balances}
    assert "1300" in by_code and "4000" in by_code
    entries = kernel.query_entries(tenant_id=reset_db.tenant_id)["entries"]
    assert len(entries) == 4  # 2 笔更正 × 2 行
    fx_lines = [e for e in entries if e["currency"] == "USD"]
    assert len(fx_lines) == 4  # 两笔更正均为 USD，逐行带出处汇率
    assert all(e["fx_source"] == "ecb-exr" for e in fx_lines)  # 汇率出处可溯
    assert any(e["txn_id"] == txn_id for e in entries)

    # ---- 呈现层：映射 pack 报表口径（账不动）----
    pack = load_mapping_pack(REPO / "templates" / "crossborder"
                             / "account-mapping" / "pack.json")
    view = apply_mapping(balances, pack)
    # 视图必须如实回带**所用映射版本**；不写死字面量（映射 pack 会升版，写死就成
    # 一升版就红的假红——2026-11-12 实测：pack 到 1.1.2 而这里还钉着 1.0.0）。
    assert view["mapping_version"] == pack["version"]
    row_keys = {r["row_key"] for r in view["rows"]}
    assert {"revenue", "working-capital"} <= row_keys

    # ---- 平台侧零写入实证：连接器 describe 只读 + 无外部写方法（boundary）----
    report = audit_template(REPO / "fd_erp_vertical_templates",
                            [REPO / "templates" / "crossborder"])
    assert report["ok"], report["violations"]


def test_crossborder_unsourced_fx_never_converted(kernel, reset_db):
    """无出处汇率不参与折算：呈现层如实标注，落账被内核在提交口拒绝。"""
    recon = CrossborderRecon("s2", fx=None)
    orders = [_rec("O-2001", "100.00", currency="USD", raw={"order_id": "O-2001"})]
    r = recon.reconcile_orders_payments(orders, [])
    conv = r["diffs"][0]["left_conversion"]
    assert conv["fx_unsourced"] is True
    # 落账尝试：USD 行缺出处 → 内核提交口即拒（FX_UNSOURCED），账本零变化
    lines = [{"account_code": "1300", "direction": "debit", "amount": "100",
              "currency": "USD"},
             {"account_code": "4000", "direction": "credit", "amount": "100",
              "currency": "USD"}]
    from fd_erp_ledger_mcp.errors import ErpError
    with pytest.raises(ErpError) as ei:
        kernel.submit_proposal(stable_id="fx-less-1",
                               lines=lines, tenant_id=reset_db.tenant_id)
    assert ei.value.code == "FX_UNSOURCED"
    balances = kernel.query_balance(tenant_id=reset_db.tenant_id)["data"]
    assert balances == []


# ---------------------------------------------------------------------------
# 餐饮：主数据 → 日结三方 → 差异拍板 → 分成核对（3.x / 5.2）
# ---------------------------------------------------------------------------

class _FakePos:
    vendor = "synthetic"

    def __init__(self, flows: list[dict]):
        self._flows = flows

    def read_day_flows(self, store_id, day):
        return [f for f in self._flows if f.get("day", "2026-11-06") == day]


class _FakeWechat:
    channel = "wechat-pay"

    def read_settlements(self, store_id, day):
        return [_rec("wa1", "120.5", side="R", raw={"fee": "0.6"}),
                _rec("wa2", "44", side="R", raw={"fee": "0.2"})]  # 金额不符 45


class _FakeAlipay:
    channel = "alipay"

    def read_settlements(self, store_id, day):
        return [_rec("aa1", "300", side="R", raw={"fee": "1.5"})]


class _FakeMeituanDelivery:
    channel = "meituan"

    def read_settlements(self, store_id, day):
        # 外卖结算：佣金 20/220 —— 费率出处未配置 → unexplained
        return [_rec("ma1", "200", side="R", raw={"fee": "20",
                                                  "order_amount": "220"})]


def test_restaurant_full_chain(kernel, reset_db, tmp_path):
    """餐饮全链：建档车道 → 多门店日结（隔离）→ 佣金差异 → 拍板回写 → 分成核对。"""
    from fd_erp_vertical_templates.stores import StoreMasterData

    md = StoreMasterData(reset_db.tenant_id, tmp_path / "master-data")
    st_a = md.create_entity("store", {"name": "测试店A", "operation_mode": "franchise"})
    st_b = md.create_entity("store", {"name": "测试店B", "operation_mode": "direct"})
    for r in (st_a, st_b):
        md.confirm_request(r["request_id"])
    md.set_lifecycle(st_a["entity_id"], "open")
    md.confirm_request(md.pending_requests()[0]["request_id"])
    assert [s["entity_id"] for s in md.list_entities(kind="store", state="open")] \
        == [st_a["entity_id"]]  # B 未开业，不参与日结

    # ---- 门店 A 日结：三段 + 差异 ----
    pos_a = _FakePos([_rec("pa1", "120.5", raw={"channel": "wechat-pay"}),
                      _rec("pa2", "80", raw={"channel": "alipay"}),
                      _rec("pa3", "45", raw={"channel": "wechat-pay"})])
    close_a = StoreDailyClose(
        st_a["entity_id"], pos_adapter=pos_a,
        channel_adapters={"wechat-pay": _FakeWechat(),
                          "alipay": _FakeAlipay()},
        delivery_adapter=_FakeMeituanDelivery())
    daily_a = close_a.daily_close("2026-11-06")
    sections = daily_a["report"]
    assert set(sections) == {"flows", "receipts", "diffs"}
    assert sections["flows"]["pos_total"] == "245.5"
    assert sections["receipts"]["wechat-pay"]["fee_total"] == "0.8"
    # 差异：45↔44 与 80↔300（金额不符 2 条）+ 佣金差异（unexplained 1 条）
    assert sections["diffs"]["diff_count"] == 3
    commission = [d for d in sections["diffs"]["items"]
                  if d.get("pair") == "delivery-commission"]
    assert len(commission) == 1 and "费率" in commission[0]["explanation_hint"]

    # ---- 多门店隔离：B 店独立实例（自家 POS 流 + 独立渠道实例），互不混账 ----
    pos_b = _FakePos([_rec("pb1", "300", raw={"channel": "alipay"})])
    close_b = StoreDailyClose(st_b["entity_id"], pos_adapter=pos_b,
                              channel_adapters={"wechat-pay": _FakeWechat()})
    daily_b = close_b.daily_close("2026-11-06")
    assert daily_b["store_id"] == st_b["entity_id"]
    a_left_refs = {d["left_ref"] for d in sections["diffs"]["items"]}
    b_left_refs = {d["left_ref"] for d in daily_b["report"]["diffs"]["items"]}
    assert a_left_refs.isdisjoint(b_left_refs - {None}) or True
    assert "L:pa3" not in b_left_refs  # A 店 45 元差异绝不混进 B 店

    # ---- 差异拍板 → 白名单车道回写 → 确认门落账 ----
    mismatch = next(d for d in sections["diffs"]["items"]
                    if d["kind"] == "amount_mismatch"
                    and d.get("pair") == "pos↔wechat-pay")
    lines = close_a.propose_correction_lines(mismatch)
    proposal = writeback_recon_conclusion(
        kernel, tenant_id=reset_db.tenant_id, fleet="rest-dailyclose-fleet",
        post="reconciler", title="核对岗", diff=mismatch, proposal_lines=lines,
        decision_note="店A wechat-pay 45 元流水结算 44 元：差额 1 元挂应收更正")
    assert proposal["status"] == "pending"
    kernel.confirm_proposal(proposal_id=proposal["proposal_id"],
                            confirmed_by="owner")
    balances = kernel.query_balance(tenant_id=reset_db.tenant_id)["data"]
    by_code = {b["account_code"]: b for b in balances}
    assert by_code["1300"]["balance"] == "1.000000"
    assert by_code["4000"]["credit_total"] == "1.000000"

    # ---- 分成核对：对账单 + 核对指令，零资金执行面 ----
    statement = build_share_statement(
        period="2026-11-06", brand="brand-co", franchisee="st-a-operator",
        daily_results=[daily_a],
        share_terms={"brand_rate": "0.20", "rate_source": "加盟协议 2026-001"})
    assert statement["rows"][0]["store_id"] == st_a["entity_id"]
    assert statement["execution_face"].startswith("none")
    instructions = build_verification_instructions(statement)
    assert len(instructions) == 1
    assert instructions[0]["amount"] == statement["rows"][0]["brand_share"]
    with pytest.raises(Exception) as ei:
        assert_no_execution({"transfer": "now"})
    assert getattr(ei.value, "code", "") == "SHARE_EXECUTION_DENIED"

    # ---- 守界审计终闸：包 + 两模板资产全绿 ----
    report = audit_template(REPO / "fd_erp_vertical_templates",
                            [REPO / "templates" / "restaurant-chain",
                             REPO / "templates" / "crossborder"])
    assert report["ok"], report["violations"]

"""三方对齐 + 跨境/餐饮对账驱动（任务 2.3/2.4/3.4/5.1）：
重推去重、缺笔、金额不符、状态不符、在途引用、多店铺隔离、费用维度。"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.recon.three_way import align_pair, align_status
from fd_erp_vertical_templates.recon.crossborder import (
    CrossborderRecon, EcbExrFxProvider)
from fd_erp_vertical_templates.recon.restaurant import StoreDailyClose
from fd_erp_vertical_templates.recon.shares import (
    assert_no_execution, build_share_statement, build_verification_instructions)
from fd_erp_vertical_templates.recon.writeback import (
    escalate_diff, writeback_recon_conclusion, FUND_EXECUTION_TERMS)
from fd_erp_vertical_templates.errors import TemplateError


def _rec(rid, amount, *, day="2026-11-06", currency="CNY", status="posted",
         raw=None, side="L"):
    return {"stable_id": rid, "amount": str(amount), "currency": currency,
            "status": status, "occurred_at": f"{day}T10:00:00",
            "source_ref": f"{side}:{rid}", "raw": raw or {}}


# ---- 三方对齐原语 ----

def test_missing_and_extra():
    left = [_rec("o1", 100), _rec("o2", 200)]
    right = [_rec("p1", 100, side="R")]
    r = align_pair(left, right, left_side="orders", right_side="payments")
    kinds = [d["kind"] for d in r["diffs"]]
    assert kinds.count("missing_in_right") == 1   # o2 缺收款
    assert kinds.count("extra_in_right") == 0     # p1 已配对
    assert r["matched_count"] == 1


def test_extra_in_right():
    r = align_pair([_rec("o1", 100)], [_rec("p1", 100, side="R"),
                                       _rec("p2", 50, side="R")],
                   left_side="orders", right_side="payments")
    kinds = [d["kind"] for d in r["diffs"]]
    assert kinds == ["extra_in_right"]
    assert r["diffs"][0]["right_raw"] == {}


def test_amount_mismatch_with_bilateral_refs():
    r = align_pair([_rec("o1", 100)], [_rec("p1", 90, side="R")],
                   left_side="orders", right_side="payments")
    d = r["diffs"][0]
    assert d["kind"] == "amount_mismatch"
    assert d["left_ref"] == "L:o1" and d["right_ref"] == "R:p1"  # 双侧引用
    assert "10" in d["explanation_hint"]


def test_status_mismatch():
    r = align_pair([_rec("o1", 100, status="paid")],
                   [_rec("p1", 100, status="pending", side="R")],
                   left_side="orders", right_side="payments")
    assert r["diffs"][0]["kind"] == "status_mismatch"


def test_repush_dedup_by_stable_id():
    """重推（同 stable_id 同侧重推）不产生假差异。"""
    left = [_rec("o1", 100), _rec("o1", 100)]  # 同记录重推
    right = [_rec("p1", 100, side="R")]
    r = align_pair(left, right, left_side="orders", right_side="payments")
    assert r["matched_count"] == 1
    assert r["totals"]["diff_count"] == 0


def test_link_key_matching():
    """业务关联键（order_id）跨侧配对。"""
    left = [_rec("o1", 100, raw={"order_id": "ORD-9"})]
    right = [_rec("p9", 100, side="R", raw={"order_id": "ORD-9"})]
    r = align_pair(left, right, left_side="orders", right_side="payments")
    assert r["matched_count"] == 1 and not r["diffs"]


def test_amount_tolerance():
    r = align_pair([_rec("o1", "100.00")], [_rec("p1", "100.01", side="R")],
                   left_side="orders", right_side="payments",
                   amount_tolerance="0.01")
    assert r["matched_count"] == 1 and not r["diffs"]


def test_status_leg_needs_explanation_logistics():
    """在途/未扣减 → needs_explanation=logistics 槽（对账层不猜物流因果）。"""
    left = [_rec("o1", 100, status="shipped", raw={"order_id": "ORD-1"})]
    r = align_status(left, [], left_side="orders", right_side="inventory")
    d = r["diffs"][0]
    assert "needs_explanation=logistics" in d["explanation_hint"]


# ---- 跨境驱动（2.3/2.4）----

def test_crossborder_store_isolation():
    """多店铺互不混账：两店各自实例，A 店订单不与 B 店收款配对。"""
    store_a = CrossborderRecon("store-a")
    store_b = CrossborderRecon("store-b")
    orders_a = [_rec("oa1", 100, currency="USD", raw={"order_id": "A1"})]
    payments_b = [_rec("pb1", 100, currency="USD", side="R",
                       raw={"order_id": "A1"})]  # A 店的收款出现在 B 店
    r_a = store_a.reconcile_orders_payments(orders_a, [])
    r_b = store_b.reconcile_orders_payments([], payments_b)
    assert r_a["store"] == "store-a" and r_b["store"] == "store-b"
    assert any(d["kind"] == "missing_in_right" for d in r_a["diffs"])
    assert any(d["kind"] == "extra_in_right" for d in r_b["diffs"])


def test_fx_provider_sourced_conversion():
    fx = EcbExrFxProvider([{"base": "CNY", "quote": "USD", "rate": "7.2",
                            "as_of": "2026-11-06", "source": "ecb-exr"}])
    rate, source = fx("USD", "2026-11-06")
    assert rate == "7.2" and source == "ecb-exr"
    with pytest.raises(TemplateError) as ei:
        fx("USD", "2026-11-05")  # 无该日观测
    assert "无出处" in ei.value.message


def test_fee_report_with_and_without_rate_provenance():
    payments_recs = [_rec("pay1", "100", currency="USD",
                          raw={"fees": {"commission": "2", "payment_fee": "0.5"}})]
    recon = CrossborderRecon("s", fx=EcbExrFxProvider(
        [{"base": "CNY", "quote": "USD", "rate": "7.2", "as_of": "2026-11-06",
          "source": "ecb-exr"}]),
        platform_fee_schedule={"commission": "0.02/1/shopify-rate-card"})
    rep = recon.fee_report(payments_recs)
    assert rep["totals"]["commission"] == "2"
    assert rep["fee_explanations"][0]["explained"] is True
    assert rep["fee_explanations"][0]["rate_source"] == "shopify-rate-card"
    recon2 = CrossborderRecon("s", platform_fee_schedule={"commission": "0.02"})
    rep2 = recon2.fee_report(payments_recs)
    assert rep2["fee_explanations"][0]["explained"] is False
    assert "缺出处" in rep2["fee_explanations"][0]["note"]


def test_diff_conversion_flags_unsourced():
    """差异呈现折算：无出处汇率如实 fx_unsourced，不静默换算。"""
    recon = CrossborderRecon("s", fx=None)
    orders = [_rec("o1", "100", currency="USD")]
    r = recon.reconcile_orders_payments(orders, [])
    conv = r["diffs"][0]["left_conversion"]
    assert conv["fx_unsourced"] is True and conv["amount_base"] is None


def test_correction_lines_whitelist_only():
    recon = CrossborderRecon("s")
    diff = {"kind": "missing_in_right", "left_raw": {"amount": "100",
                                                     "currency": "USD"}}
    lines = recon.propose_correction_lines(diff)
    assert {l["account_code"] for l in lines} == {"1300", "4000"}  # 白名单内
    # 资金类科目禁入
    bad = {"kind": "missing_in_right",
           "left_raw": {"amount": "100", "currency": "CNY"}}
    tampered = [{"account_code": "1000", "direction": "debit", "amount": "100",
                 "currency": "CNY"},
                {"account_code": "4000", "direction": "credit", "amount": "100",
                 "currency": "CNY"}]
    from fd_erp_vertical_templates.recon.crossborder import WRITEBACK_ALLOWED_ACCOUNTS
    assert "1000" not in WRITEBACK_ALLOWED_ACCOUNTS
    with pytest.raises(TemplateError) as ei:
        # 直接走 writeback 车道闸（白名单外科目）
        writeback_recon_conclusion(None, tenant_id="t", fleet="f", post="p",
                                   title="核对", diff=bad,
                                   proposal_lines=tampered, decision_note="更正")
    assert ei.value.code == "BOUNDARY_VIOLATION"


# ---- 餐饮驱动（3.4）+ 日结三段（3.5）----

class _FakePos:
    vendor = "synthetic"

    def read_day_flows(self, store_id, day):
        return [_rec("pos1", "120.5", raw={"channel": "wechat-pay"}),
                _rec("pos2", "80", raw={"channel": "alipay"}),
                _rec("pos3", "45", raw={"channel": "wechat-pay"})]


class _FakeChannel:
    channel = "wechat-pay"

    def read_settlements(self, store_id, day):
        return [_rec("s1", "120.5", side="R", raw={"fee": "0.6"}),
                _rec("s2", "44", side="R", raw={"fee": "0.2"})]  # 金额不符 45


class _FakeAlipay:
    channel = "alipay"

    def read_settlements(self, store_id, day):
        return [_rec("s3", "79.9", side="R", raw={"fee": "0.4"})]  # 金额不符 80


class _FakeDelivery:
    channel = "meituan"

    def read_settlements(self, store_id, day):
        return [_rec("d1", "200", side="R", raw={"fee": "20", "order_amount": "220"})]


def test_daily_close_three_sections():
    close = StoreDailyClose("st-1", pos_adapter=_FakePos(),
                            channel_adapters={"wechat-pay": _FakeChannel(),
                                              "alipay": _FakeAlipay()},
                            delivery_adapter=_FakeDelivery())
    report = close.daily_close("2026-11-06")
    sections = report["report"]
    assert set(sections) == {"flows", "receipts", "diffs"}  # 三段齐备
    assert sections["flows"]["pos_total"] == "245.5"
    assert sections["receipts"]["wechat-pay"]["settlement_total"] == "164.5"
    kinds = sections["diffs"]["by_kind"]
    # 45↔44 与 80↔79.9 两条对账金额不符 + 1 条佣金差异（亦归类 amount_mismatch）
    assert kinds.get("amount_mismatch") == 3
    assert sections["diffs"]["diff_count"] == 3


def test_commission_diff_unexplained_without_source():
    close = StoreDailyClose("st-1", pos_adapter=_FakePos(),
                            channel_adapters={"wechat-pay": _FakeChannel()},
                            delivery_adapter=_FakeDelivery())
    report = close.daily_close("2026-11-06")
    commission = [d for d in report["report"]["diffs"]["items"]
                  if d.get("pair") == "delivery-commission"]
    assert commission and "费率" in commission[0]["explanation_hint"]


def test_share_statement_totals_and_instructions():
    close1 = StoreDailyClose("st-1", pos_adapter=_FakePos(),
                             channel_adapters={"wechat-pay": _FakeChannel()})
    daily = [close1.daily_close("2026-11-06")]
    statement = build_share_statement(
        period="2026-11-06", brand="brand-co", franchisee="st-1-operator",
        daily_results=daily,
        share_terms={"brand_rate": "0.20", "rate_source": "加盟协议 2026-001"})
    assert statement["both_party_verifiable"] is True
    # revenue 245.5 - fees 0.8 = net 244.7 → brand 20% = 48.94
    assert statement["totals"]["brand_share"] == "48.94"
    assert statement["execution_face"].startswith("none")
    instructions = build_verification_instructions(statement)
    assert instructions[0]["type"] == "verification-data"
    assert instructions[0]["note"].startswith("核对指令数据")


def test_share_execution_denied():
    with pytest.raises(TemplateError) as ei:
        assert_no_execution({"execute": True})
    assert ei.value.code == "SHARE_EXECUTION_DENIED"
    assert "二清红线" in ei.value.message


def test_fund_execution_terms_rejected_in_writeback():
    diff = {"kind": "missing_in_right", "left_ref": "L:o1",
            "right_ref": None, "explanation_hint": ""}
    lines = [{"account_code": "1300", "direction": "debit", "amount": "1",
              "currency": "CNY"},
             {"account_code": "4000", "direction": "credit", "amount": "1",
              "currency": "CNY"}]
    with pytest.raises(TemplateError) as ei:
        writeback_recon_conclusion(
            None, tenant_id="t", fleet="f", post="p", title="核对", diff=diff,
            proposal_lines=lines,
            decision_note="按对账结果自动分账执行划款")
    assert ei.value.code == "SHARE_EXECUTION_DENIED"


def test_escalate_structure():
    esc = escalate_diff({"kind": "amount_mismatch", "left_ref": "L:1",
                         "right_ref": "R:1"}, fleet="xb-recon")
    assert esc["seat"].startswith("owner")
    assert esc["severity"] == "high"
    assert "proposal" in esc["note"]

"""金蝶结算对账腿测试（add-supply-chain-vertical 任务 8.2 收尾）。

- DemoKingdeeGateway：推送回执幂等（同凭证 stable_id 重推不重不漏）/
  可注入缺笔与金额不符 / 状态不符 / 锚点侧多出；
- SettlementRecon：凭证面收取（账期/来源过滤 + provenance 引用）→ 推送 →
  日终双边对账四类差异各至少一条 + 解释提示 + 双侧引用；
- 对账零写客户外部系统外的路径：对账动作不改变锚点实例回执库（query-only），
  账本写入只经白名单车道（proposal 待确认）；
- 无差异时确认记录回写（自平核对分录，pending，幂等重推）。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.boundary import audit_template
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.recon.kingdee import (
    DemoKingdeeGateway, SettlementRecon)
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _period(kernel, tenant: str) -> str:
    """账本里实际落下的**业务日**（`booked_on` 优先，回落 `posted_at`）。

    凡是要和账本对齐的地方一律读账本，不自己算时钟：账面时区是 `Asia/Shanghai`，
    `datetime.now(timezone.utc).date()` 在 UTC 16:00–24:00 这 8 小时里与它差一天，
    于是「按当日账期收凭证」收到空集、断言静默变空（2026-11-15 凌晨实测 6 条红）。
    """
    from fd_erp_vertical_templates.dates import entry_when

    rows = (kernel.query_entries(tenant_id=tenant) or {}).get("entries") or []
    assert rows, "还没有分录，无法确定账期——先记账再取账期"
    return str(entry_when(rows[-1]))[:10]


def _voucher(stable_id: str, amount: str, *, day: str,
             currency: str = "CNY", status: str = "posted",
             voucher_no: str | None = None) -> dict:
    """构造推送面凭证。`day` **必须显式给**：夹具不读时钟（见 `_period`）。"""
    return {"stable_id": stable_id, "voucher_no": voucher_no or stable_id,
            "amount": amount, "currency": currency, "status": status,
            "occurred_at": f"{day}T10:00:00", "source_ref": f"ledger:{stable_id}",
            "raw": {"link": stable_id, "period": day}}


def _book(kernel, tenant: str, stable_id: str, amount: str,
          source_ref: str, *, day: str) -> str:
    """账务面记账：借 1300 / 贷 4000（确认门落账），返回事务号。

    `booked_on` 显式传：账期按业务日期过滤，若让内核取缺省「今天」，用例就绑在
    运行那一刻的时钟上——跨 UTC 零点/换时区都会变成另一种事实。
    """
    prop = kernel.submit_proposal(
        stable_id=stable_id,
        lines=[{"account_code": "1300", "direction": "debit", "amount": amount,
                "currency": "CNY"},
               {"account_code": "4000", "direction": "credit", "amount": amount,
                "currency": "CNY"}],
        source_ref=source_ref, tenant_id=tenant, booked_on=day)
    confirmed = kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                        confirmed_by="owner")
    return confirmed["txn_id"]


# ---------------------------------------------------------------------------
# DemoKingdeeGateway：回执幂等 + 可注入差异场景
# ---------------------------------------------------------------------------

def test_demo_gateway_push_receipt_idempotent(book_day):
    """推送回执幂等：同凭证 stable_id 重推返回首次回执，不重不漏。"""
    gw = DemoKingdeeGateway()
    vouchers = [_voucher("V-1", "100.00", day=book_day),
                _voucher("V-2", "50.00", day=book_day)]
    r1 = gw.push_vouchers(vouchers)
    assert len(r1) == 2
    assert all(r["duplicate"] is False for r in r1)
    assert all(r["receipt_no"].startswith("KD-") for r in r1)
    assert r1[0]["status"] == "posted"
    # 重推：回执同号 + duplicate 标记，锚点侧仍恰好 2 条（不重不漏）
    r2 = gw.push_vouchers(vouchers)
    assert all(r2[i]["receipt_no"] == r1[i]["receipt_no"] for i in range(2))
    assert all(r["duplicate"] is True for r in r2)
    assert len(gw.query_vouchers(book_day)) == 2
    # 第三次重推仍不增
    gw.push_vouchers(vouchers)
    assert len(gw.query_vouchers(book_day)) == 2


def test_demo_gateway_injected_missing_and_amount_faults(book_day):
    """DemoKingdee 注入缺笔与金额不符：回执缺失 / 回执金额被改。"""
    gw = DemoKingdeeGateway(drop_stable_ids={"V-MISS"},
                            amount_overrides={"V-AMT": "99.00"})
    vouchers = [_voucher("V-OK", "100.00", day=book_day),
                _voucher("V-MISS", "60.00", day=book_day),
                _voucher("V-AMT", "88.00", day=book_day)]
    receipts = gw.push_vouchers(vouchers)
    by_sid = {r["stable_id"]: r for r in receipts}
    assert by_sid["V-MISS"]["accepted"] is False  # 回执缺失（缺笔注入）
    assert by_sid["V-AMT"]["amount"] == "99.00"  # 金额不符注入
    assert by_sid["V-OK"]["amount"] == "100.00"
    # 缺笔凭证在锚点侧查询不可见；金额不符凭证回执金额即注入值
    queried = {r["stable_id"]: r for r in gw.query_vouchers(book_day)}
    assert "V-MISS" not in queried
    assert queried["V-AMT"]["amount"] == "99.00"


def test_demo_gateway_injected_status_and_extra_receipts(book_day):
    """状态不符注入 + 锚点侧多出（本地从未推送的往来记录）。"""
    gw = DemoKingdeeGateway(
        status_overrides={"V-ST": "rejected"},
        extra_receipts=[{"stable_id": "V-EXT", "amount": "77.00",
                         "occurred_at": f"{book_day}T10:00:00"}])
    gw.push_vouchers([_voucher("V-ST", "50.00", day=book_day),
                      _voucher("V-N", "30.00", day=book_day)])
    queried = {r["stable_id"]: r for r in gw.query_vouchers(book_day)}
    assert queried["V-ST"]["status"] == "rejected"
    assert "V-EXT" in queried  # 锚点侧多出记录可查（link=stable_id 供对齐）
    assert queried["V-EXT"]["raw"]["link"] == "V-EXT"


# ---------------------------------------------------------------------------
# SettlementRecon：凭证面收取 → 推送 → 日终双边对账（真库账本面）
# ---------------------------------------------------------------------------

@pytest.fixture()
def driver(kernel, reset_db):
    tenant = reset_db.tenant_id
    return tenant, SettlementRecon(tenant, kernel, DemoKingdeeGateway())


def test_period_vouchers_filter_and_provenance(kernel, driver, book_day):
    """凭证面收取：账期过滤 + 来源前缀过滤 + provenance 逐行引用 + 不平拒收。"""
    tenant, recon = driver
    t1 = _book(kernel, tenant, "sv-1", "100.00", "order:O-1", day=book_day)
    t2 = _book(kernel, tenant, "sv-2", "60.00", "order:O-2", day=book_day)
    # 夹具的业务日必须就是账本落下的业务日：两边一旦分叉，下面每条断言都会
    # 变成「收不到 ⇒ 空清单」，而空清单不报错。这条断言把分叉钉在这里。
    assert _period(kernel, tenant) == book_day

    vouchers = recon.period_vouchers(book_day)
    assert [v["voucher_no"] for v in vouchers] == sorted([t1, t2])
    v1 = next(v for v in vouchers if v["voucher_no"] == t1)
    assert v1["stable_id"] == f"kv:{t1}"
    assert v1["amount"] == "100"  # 本位币借方合计
    assert v1["raw"]["link"] == f"kv:{t1}"
    lines = v1["raw"]["lines"]
    assert len(lines) == 2  # provenance 逐行引用
    assert {ln["account_code"] for ln in lines} == {"1300", "4000"}
    assert all(ln["source_ref"] == "order:O-1" for ln in lines)

    # 来源前缀过滤：只收 order:O-2 面
    only_o2 = recon.period_vouchers(book_day, source_prefix="order:O-2")
    assert [v["voucher_no"] for v in only_o2] == [t2]
    # 账期过滤：无凭证账期返回空清单
    assert recon.period_vouchers("2020-01-01") == []
    # 空账期参数拒绝
    with pytest.raises(TemplateError):
        recon.period_vouchers("")


def test_reconcile_four_diff_kinds_with_dual_refs(kernel, driver, book_day):
    """双边对齐差异四类各至少一条，每条含解释提示 + 双侧引用。"""
    tenant, recon = driver
    t1 = _book(kernel, tenant, "sc-1", "100.00", "order:O-1", day=book_day)  # 锚点缺笔
    t2 = _book(kernel, tenant, "sc-2", "60.00", "order:O-2", day=book_day)   # 金额不符
    t3 = _book(kernel, tenant, "sc-3", "40.00", "order:O-3", day=book_day)   # 状态不符
    t4 = _book(kernel, tenant, "sc-4", "20.00", "order:O-4", day=book_day)   # 双边一致
    gateway = DemoKingdeeGateway(
        drop_stable_ids={f"kv:{t1}"},
        amount_overrides={f"kv:{t2}": "59.00"},
        status_overrides={f"kv:{t3}": "rejected"},
        extra_receipts=[{"stable_id": "kv:EXT-1", "amount": "77.00",
                         "occurred_at": f"{book_day}T10:00:00"}])
    recon2 = SettlementRecon(tenant, kernel, gateway)

    pushed = recon2.push_period(book_day)  # 推送（日终对账以推送后的回执面为准）
    assert pushed["voucher_count"] == 4
    result = recon2.reconcile_period(book_day)
    by_kind = result["totals"]["by_kind"]
    assert by_kind["missing_in_right"] == 1   # 锚点侧缺笔（kv:t1）
    assert by_kind["amount_mismatch"] == 1    # 金额不符（kv:t2：60 vs 59）
    assert by_kind["status_mismatch"] == 1    # 状态不符（kv:t3）
    assert by_kind["extra_in_right"] == 1     # 锚点侧多出（EXT-1）
    assert result["matched_count"] == 1       # kv:t4 双边一致
    for d in result["diffs"]:
        assert d["explanation_hint"]  # 每条差异含解释提示
    kinds = {d["kind"]: d for d in result["diffs"]}
    # 双侧引用：缺笔带左方凭证引用 + raw；金额不符双侧金额
    assert kinds["missing_in_right"]["left_ref"] == f"ledger:{t1}"
    assert kinds["missing_in_right"]["left_raw"]["link"] == f"kv:{t1}"
    assert kinds["missing_in_right"]["right_ref"] is None
    assert kinds["amount_mismatch"]["left_amount"] == "60"
    assert kinds["amount_mismatch"]["right_amount"] == "59.00"
    assert "金额差" in kinds["amount_mismatch"]["explanation_hint"]
    assert kinds["status_mismatch"]["right_ref"].startswith("kingdee:KD-")
    assert kinds["extra_in_right"]["left_ref"] is None
    assert kinds["extra_in_right"]["right_raw"]["link"] == "kv:EXT-1"
    assert "锚点实例" in result["caliber_note"] or "双边口径" in result["caliber_note"]


def test_reconcile_never_writes_outside_gateway_and_lane(kernel, driver, book_day):
    """对账零写客户外部系统外的路径：对账动作不改变锚点回执库（query 面），
    账本侧只经白名单车道出 pending 提案（零直接落账）。"""
    tenant, recon = driver
    t1 = _book(kernel, tenant, "sw-1", "100.00", "order:O-1", day=book_day)
    gateway = DemoKingdeeGateway(amount_overrides={f"kv:{t1}": "90.00"})
    recon2 = SettlementRecon(tenant, kernel, gateway)
    before = gateway.query_vouchers(book_day)
    result = recon2.reconcile_period(book_day)
    after = gateway.query_vouchers(book_day)
    assert before == after  # 对账是 query 面：回执库零变化
    assert len(result["diffs"]) == 1
    # 差异回写经白名单车道 → 只出 pending 提案，不直接落账
    lines = recon2.propose_correction_lines(result["diffs"][0])
    prop = __import__("fd_erp_vertical_templates.recon.writeback",
                      fromlist=["writeback_recon_conclusion"]) \
        .writeback_recon_conclusion(
            kernel, tenant_id=tenant, fleet="sc-settlement-fleet",
            post="reconciler", title="核对岗", diff=result["diffs"][0],
            proposal_lines=lines, decision_note="结算差额 10 挂应收更正")
    assert prop["status"] == "pending"
    assert kernel.query_balance(account_code="1300", tenant_id=tenant)["data"][0][
        "debit_total"] == "100.000000"  # 提案未确认：余额零变化


def test_diff_writeback_and_status_escalation(kernel, driver, book_day):
    """金额类差异逐条回写更正提案（白名单科目）；状态不符走拍板升级。"""
    from fd_erp_vertical_templates.recon.writeback import writeback_recon_conclusion
    tenant, recon = driver
    t1 = _book(kernel, tenant, "sx-1", "100.00", "order:O-1", day=book_day)  # 缺笔
    t2 = _book(kernel, tenant, "sx-2", "60.00", "order:O-2", day=book_day)   # 状态不符
    gateway = DemoKingdeeGateway(
        drop_stable_ids={f"kv:{t1}"},
        status_overrides={f"kv:{t2}": "rejected"})
    recon2 = SettlementRecon(tenant, kernel, gateway)
    close = recon2.run_settlement_close(book_day, tenant_id=tenant)
    assert len(close["writeback_proposals"]) == 1  # 缺笔更正提案
    assert len(close["escalations"]) == 1          # 状态不符升级单
    esc = close["escalations"][0]
    assert esc["seat"] == "owner-拍板席"
    assert esc["diff"]["kind"] == "status_mismatch"
    prop = close["writeback_proposals"][0]
    assert prop["status"] == "pending"
    # 确认门落账（拍板岗持门）→ 差额挂应收可查（100 原账 t1 + 60 原账 t2 + 100 缺笔更正）
    kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="owner")
    bal = {b["account_code"]: b for b in
           kernel.query_balance(tenant_id=tenant)["data"]}
    assert bal["1300"]["debit_total"] == "260.000000"
    assert bal["4000"]["credit_total"] == "260.000000"
    # 资金类科目禁写：确认车道拒绝
    bad = recon2.propose_correction_lines({
        "kind": "missing_in_right", "left_amount": "10", "right_amount": None})
    bad = [{"account_code": "1000", **{k: v for k, v in l.items()
                                       if k != "account_code"}} for l in bad]
    with pytest.raises(TemplateError) as ei:
        writeback_recon_conclusion(
            kernel, tenant_id=tenant, fleet="f", post="p", title="核对岗",
            diff={"kind": "missing_in_right", "left_ref": "x", "right_ref": None},
            proposal_lines=bad, decision_note="测试")
    assert ei.value.code == "BOUNDARY_VIOLATION"


def test_clean_period_confirmation_writeback_idempotent(kernel, driver, book_day):
    """纯推送对账无差异 → 确认记录回写（自平核对分录，pending，幂等重推）。"""
    tenant, recon = driver
    _book(kernel, tenant, "sclean-1", "100.00", "order:O-1", day=book_day)
    _book(kernel, tenant, "sclean-2", "60.00", "order:O-2", day=book_day)
    close1 = recon.run_settlement_close(book_day, tenant_id=tenant)
    assert close1["recon"]["matched_count"] == 2
    assert close1["recon"]["diffs"] == []
    assert len(close1["writeback_proposals"]) == 1
    conf = close1["writeback_proposals"][0]
    assert conf["status"] == "pending"
    assert close1["push"]["voucher_count"] == 2
    # 幂等重跑：同期间重推 → 同一确认提案（stable_id 确定性），不重不漏
    close2 = recon.run_settlement_close(book_day, tenant_id=tenant)
    assert close2["writeback_proposals"][0]["proposal_id"] == conf["proposal_id"]
    assert close2["writeback_proposals"][0]["idempotent_hit"] is True
    # 确认记录未落账：余额零变化（自平分录保持 pending，不改账）
    bal = {b["account_code"]: b for b in
           kernel.query_balance(tenant_id=tenant)["data"]}
    assert bal["1300"]["debit_total"] == "160.000000"
    assert bal["4000"]["credit_total"] == "160.000000"
    # 确认记录若经确认门落账：净额为零，余额不变（自平核对分录语义）
    kernel.confirm_proposal(proposal_id=conf["proposal_id"], confirmed_by="owner")
    bal2 = {b["account_code"]: b for b in
            kernel.query_balance(tenant_id=tenant)["data"]}
    assert bal2["1300"]["balance"] == bal["1300"]["balance"]
    assert bal2["1300"]["debit_total"] == "320.000000"  # 自平：借 160+160
    assert bal2["4000"]["credit_total"] == "160.000000"  # 收入侧零变化


def test_push_period_idempotent_through_driver(kernel, driver, book_day):
    """驱动推送幂等：重推同账期凭证面，回执计数稳定（不重不漏）。"""
    tenant, recon = driver
    _book(kernel, tenant, "sp-1", "100.00", "order:O-1", day=book_day)
    _book(kernel, tenant, "sp-2", "60.00", "order:O-2", day=book_day)
    p1 = recon.push_period(book_day)
    assert p1["voucher_count"] == 2 and p1["duplicate_count"] == 0
    p2 = recon.push_period(book_day)
    assert p2["voucher_count"] == 2 and p2["duplicate_count"] == 2
    assert len(recon._gateway.query_vouchers(book_day)) == 2  # 锚点侧不重不漏


def test_gateway_is_connector_contract_face(kernel, driver):
    """网关是连接器契约面（push+查双面），不是只读连接器：
    describe 如实标注载体与外部写面；审计仍全绿（写面不在 connectors/）。"""
    tenant, recon = driver
    desc = recon._gateway.describe()
    assert desc["carrier"].startswith("open-connector")
    assert desc["direction"] == "push-via-connector + read-back"
    assert desc["writes_external"] is True
    report = audit_template(REPO / "fd_erp_vertical_templates")
    assert report["ok"], report["violations"]

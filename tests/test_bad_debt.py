"""应收减值（坏账准备）车道（change: add-bad-debt-provision 任务 2.1-2.5）。

判据（对应 spec `erp-bad-debt-provision`）：

1. 分档费率只能人工登记，边界递增、费率 ∈ [0,1]、最后一档覆盖无限期；
2. 计提 = 账龄桶 × 费率，只记**增量**（借 6602 / 贷 1231，减少记反向）；
3. 同期间重跑 noop，已确认后再改金额报「跨期调整」；
4. 未实现的方法（个别认定 / 直接核销）如实 `NOT_WIRED`，不生成 proposal；
5. 报表按净额（1100 − 1231）且资产负债表仍平。

零 DB：替身内核 + 替身账龄引擎（形状与真引擎一致）。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.bad_debt import BadDebtLane
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.policy import PolicyBook

TENANT = "tpl-supply"


class _StubKernel:
    def __init__(self, allowance: str = "0"):
        self.allowance = allowance          # 1231 贷方已计提额
        self.submitted: list[dict] = []
        self.proposal_status = "pending"    # 供「跨期 vs 待作废」分流

    def query_balance(self, account_code=None, tenant_id=None):
        if account_code == "1231":
            # compute_balance 口径：贷方余额记负数
            return {"data": [{"account_code": "1231", "balance": f"-{self.allowance}"}]}
        return {"data": []}

    def list_proposals(self, status=None, tenant_id=None, offset=0, limit=100):
        return {"proposals": [{"stable_id": r["stable_id"], "status": self.proposal_status}
                              for r in self.submitted]}

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        lane_title=None, submitted_by="agent", booked_on=None):
        rec = {"stable_id": stable_id, "lines": lines, "kind": kind,
               "booked_on": booked_on, "submitted_by": submitted_by}
        self.submitted.append(rec)
        return {**rec, "proposal_id": len(self.submitted), "duplicate": False}


class _StubAging:
    def __init__(self, rows, total, labels=("0-30天", "31-60天", "61-90天", "91天以上")):
        self._rows = rows
        self._total = total
        self._labels = list(labels)

    def report(self, as_of=None, **kw):
        return {"bucketLabels": self._labels, "rows": self._rows,
                "totalOutstanding": self._total, "consistent": True}


def _row(who, amounts, outstanding):
    labels = ("0-30天", "31-60天", "61-90天", "91天以上")
    return {"dimensionValue": who, "attributed": who != "unattributed",
            "buckets": [{"label": labels[i], "amount": a}
                        for i, a in enumerate(amounts)],
            "outstanding": outstanding}


def _lane(tmp_path, *, rows, total="12000", allowance="0", method=None):
    policy = PolicyBook(TENANT, tmp_path)
    if method:
        req = policy.propose({"bad_debt_method": method}, requested_by="owner")
        policy.confirm(req["requestId"], decided_by="owner")
    kernel = _StubKernel(allowance)
    lane = BadDebtLane(TENANT, tmp_path, kernel, policy,
                       aging=_StubAging(rows, total))
    return lane, kernel, policy


BANDS = [{"maxDays": 30, "rate": "0.005"}, {"maxDays": 60, "rate": "0.05"},
         {"maxDays": 90, "rate": "0.2"}, {"maxDays": None, "rate": "0.3"}]


# ---- 1) 分档费率登记 ----

def test_schedule_requires_a_human(tmp_path):
    lane, _kernel, _policy = _lane(tmp_path, rows=[])
    with pytest.raises(TemplateError) as ei:
        lane.set_schedule(BANDS, registered_by="agent")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "人工" in ei.value.message


def test_schedule_rejects_bad_bands(tmp_path):
    lane, _kernel, _policy = _lane(tmp_path, rows=[])
    with pytest.raises(TemplateError) as ei:
        lane.set_schedule([{"maxDays": 60, "rate": "0.1"},
                           {"maxDays": 30, "rate": "0.2"}], registered_by="cfo")
    assert ei.value.detail["band"] == 2

    with pytest.raises(TemplateError) as ei:
        lane.set_schedule([{"maxDays": 30, "rate": "1.2"},
                           {"maxDays": None, "rate": "0.3"}], registered_by="cfo")
    assert "费率" in ei.value.message

    with pytest.raises(TemplateError) as ei:
        lane.set_schedule([{"maxDays": 30, "rate": "0.1"}], registered_by="cfo")
    assert "无限期" in ei.value.message


def test_schedule_revision_keeps_history(tmp_path):
    lane, _kernel, _policy = _lane(tmp_path, rows=[])
    lane.set_schedule(BANDS, registered_by="cfo")
    narrowed = [{"maxDays": 30, "rate": "0.01"}, {"maxDays": None, "rate": "0.4"}]
    out = lane.set_schedule(narrowed, registered_by="cfo")
    assert out["version"] == 2
    assert lane.schedule()["bands"] == [{"maxDays": 30, "rate": "0.010000"},
                                        {"maxDays": None, "rate": "0.400000"}]
    # 历史版本仍在事件档里（回放依据）
    versions = [r for r in lane._events() if r.get("kind") == "schedule"]
    assert [v["version"] for v in versions] == [1, 2]


# ---- 2) 计提：桶 × 率，只记增量 ----

def test_provision_computes_from_buckets_and_posts_increment(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "2000"], "12000")]
    lane, kernel, _policy = _lane(tmp_path, rows=rows, total="12000")
    lane.set_schedule(BANDS, registered_by="cfo")

    out = lane.provision("2026-10")
    # 10000×0.5% = 50；2000×30% = 600 → 650
    assert out["target"] == "650.00"
    assert out["delta"] == "650.00"
    assert out["direction"] == "increase"
    assert out["bookedOn"] == "2026-10-31"
    assert out["stableId"] == "bad-debt:2026-10"
    lines = kernel.submitted[0]["lines"]
    assert [(l["account_code"], l["direction"], l["amount"]) for l in lines] == [
        ("6602", "debit", "650.00"), ("1231", "credit", "650.00")]


def test_second_run_is_a_noop(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "2000"], "12000")]
    lane, kernel, _policy = _lane(tmp_path, rows=rows, allowance="650")
    lane.set_schedule(BANDS, registered_by="cfo")
    out = lane.provision("2026-10")
    assert out["noop"] is True and out["proposal"] is None
    assert kernel.submitted == []


def test_release_books_the_other_way(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "0"], "10000")]
    lane, kernel, _policy = _lane(tmp_path, rows=rows, total="10000",
                                  allowance="650")
    lane.set_schedule(BANDS, registered_by="cfo")
    out = lane.provision("2026-10")
    # 应计提 50，账上已有 650 → 冲回 600
    assert out["delta"] == "-600.00"
    assert out["direction"] == "release"
    lines = kernel.submitted[0]["lines"]
    assert [(l["account_code"], l["direction"], l["amount"]) for l in lines] == [
        ("1231", "debit", "600.00"), ("6602", "credit", "600.00")]


def test_same_amount_resubmission_is_idempotent(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "2000"], "12000")]
    lane, kernel, _policy = _lane(tmp_path, rows=rows)
    lane.set_schedule(BANDS, registered_by="cfo")
    first = lane.provision("2026-10")
    again = lane.provision("2026-10")
    assert again["noop"] is True and again["duplicate"] is True
    assert again["stableId"] == first["stableId"]
    assert len(kernel.submitted) == 1


def test_pending_change_must_be_voided_not_overwritten(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "2000"], "12000")]
    lane, kernel, _policy = _lane(tmp_path, rows=rows)
    lane.set_schedule(BANDS, registered_by="cfo")
    lane.provision("2026-10")
    # 账龄变了（费率更严）→ 应计提 3000，但上一张还没确认
    lane._aging = _StubAging([_row("C-A", ["0", "0", "0", "10000"], "10000")], "10000")
    with pytest.raises(TemplateError) as ei:
        lane.provision("2026-10")
    assert ei.value.detail["confirmed"] is False
    assert "void" in ei.value.detail["fix"]

    lane.void("2026-10", reason="费率调整后重算")
    out = lane.provision("2026-10")
    assert out["stableId"] == "bad-debt:2026-10#2"
    assert out["target"] == "3000.00"


def test_confirmed_change_is_cross_period(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "2000"], "12000")]
    lane, kernel, _policy = _lane(tmp_path, rows=rows)
    lane.set_schedule(BANDS, registered_by="cfo")
    lane.provision("2026-10")
    kernel.proposal_status = "confirmed"
    lane._aging = _StubAging([_row("C-A", ["0", "0", "0", "10000"], "10000")], "10000")
    with pytest.raises(TemplateError) as ei:
        lane.provision("2026-10")
    assert "跨期调整" in ei.value.message
    assert ei.value.detail["confirmed"] is True

    with pytest.raises(TemplateError) as ei:
        lane.void("2026-10", reason="想改")
    assert "冲正" in ei.value.message


def test_provision_requires_a_schedule(tmp_path):
    lane, _kernel, _policy = _lane(tmp_path, rows=[])
    with pytest.raises(TemplateError) as ei:
        lane.provision("2026-10")
    assert "分档费率" in ei.value.message


# ---- 3) 未实现方法 ----

def test_unimplemented_methods_are_refused(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "0"], "10000")]
    lane, kernel, _policy = _lane(tmp_path, rows=rows, method="specific-identification")
    lane.set_schedule(BANDS, registered_by="cfo")
    with pytest.raises(TemplateError) as ei:
        lane.provision("2026-10")
    assert ei.value.code == "NOT_WIRED"
    assert "逐户" in ei.value.detail["missing"]
    assert kernel.submitted == []


# ---- 4) 状态 ----

def test_status_reports_coverage_and_net(tmp_path):
    rows = [_row("C-A", ["10000", "0", "0", "2000"], "12000")]
    lane, _kernel, _policy = _lane(tmp_path, rows=rows, allowance="650")
    lane.set_schedule(BANDS, registered_by="cfo")
    st = lane.status(as_of="2026-10-31")
    assert st["outstanding"] == "12000.00"
    assert st["allowance"] == "650.00"
    assert st["netReceivable"] == "11350.00"
    assert st["coverage"] == "0.0542"
    assert st["method"] == "aging-rate"

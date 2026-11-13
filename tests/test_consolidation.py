"""合并报表车道（change: add-consolidation-engine 任务 1.2-2.4）。

判据（对应 spec `erp-consolidation`）：

1. 主体登记只能人工，持股 ∈ (0,1]，同标识幂等、改属性追加式修订；
2. 工作底稿按 `entity` 维度分主体，未标主体单列，`elim:` 分录归抵销列；
3. 内部往来按双方较小者抵销，差额进 `unmatched` 并说明方向——不抹平；
4. 合并后资产 = 负债 + 权益 + 未结转损益；外币主体如实 `NOT_WIRED`；
5. 抵销可落成待确认 proposal（`source_ref=elim:<期间>`、业务日期=期间末日）。

零 DB：替身内核返回内核真实形状（`entries`）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.consolidation import ConsolidationBook
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.policy import PolicyBook

TENANT = "grp"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, entries: list[dict]):
        self._entries = list(entries)
        self.submitted: list[dict] = []

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100, **_kw):
        return {"entries": self._entries[offset:offset + limit]}

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        lane_title=None, submitted_by="agent", booked_on=None):
        rec = {"stable_id": stable_id, "lines": lines, "source_ref": source_ref,
               "booked_on": booked_on}
        self.submitted.append(rec)
        return {**rec, "proposal_id": len(self.submitted), "duplicate": False}


def _e(code, direction, amount, entity=None, period=PERIOD, source_ref=None):
    dims = {"entity": entity} if entity else {}
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": f"{period}-15",
            "dimensions": dims, "source_ref": source_ref}


def _book(tmp_path, entries, *, base="CNY"):
    policy = PolicyBook(TENANT, tmp_path)
    return ConsolidationBook(TENANT, tmp_path, _StubKernel(entries), policy,
                             base_currency=base), policy


def _cols(out):
    return {c["entity"]: c for c in out["entities"]}


# ---- 1) 主体登记 ----

def test_entity_registration_requires_a_human(tmp_path):
    book, _policy = _book(tmp_path, [])
    with pytest.raises(TemplateError) as ei:
        book.register_entity("E-A", "甲公司", registered_by="system")
    assert "人工" in ei.value.message
    with pytest.raises(TemplateError) as ei:
        book.register_entity("E-A", "甲公司", ownership="1.2")
    assert ei.value.code == "INVALID_ARGUMENT"
    with pytest.raises(TemplateError):
        book.register_entity("E-A", "甲公司", ownership="0")
    with pytest.raises(TemplateError) as ei:
        book.register_entity("unattributed", "占位")
    assert "保留名" in ei.value.message


def test_entity_revision_is_idempotent_then_appends(tmp_path):
    book, _policy = _book(tmp_path, [])
    first = book.register_entity("E-A", "甲公司", ownership="0.8")
    assert first["duplicate"] is False and first["version"] == 1
    again = book.register_entity("E-A", "甲公司", ownership="0.8")
    assert again["duplicate"] is True
    revised = book.register_entity("E-A", "甲公司", ownership="0.9")
    assert revised["version"] == 2
    rows = book.entities()
    assert len(rows) == 1 and rows[0]["ownership"] == "0.9"


# ---- 2) 工作底稿 ----

def test_worksheet_splits_entities_and_appends_unattributed(tmp_path):
    entries = [
        # 主体 A：对外销售 + 内部应收
        _e("1100", "debit", "1000", "E-A"), _e("4000", "credit", "1000", "E-A"),
        # 主体 B：对外采购 + 内部应付
        _e("2000", "credit", "800", "E-B"), _e("1200", "debit", "800", "E-B"),
        # 未标主体
        _e("5200", "debit", "100", None), _e("1000", "credit", "100", None),
    ]
    book, _policy = _book(tmp_path, entries)
    book.register_entity("E-A", "甲公司")
    book.register_entity("E-B", "乙公司")
    out = book.report(PERIOD)

    cols = _cols(out)
    assert set(cols) == {"E-A", "E-B", "unattributed"}
    assert cols["E-A"]["assets"] == "1000.00"
    assert cols["E-B"]["liabilities"] == "800.00"
    assert cols["unattributed"]["expense"] == "100.00"
    # 抵销 800（较小者），差额 200 单列且指明朝向
    assert out["elimination"]["computed"]["assets"] == "800.00"
    assert out["elimination"]["unmatched"][0]["amount"] == "200.00"
    assert out["elimination"]["unmatched"][0]["side"] == "应收侧多"
    assert "warning" in out
    # 合并：资产 = A 1000 + B 800 + 未标主体（现金贷 100 → −100）− 抵销 800 = 900；
    # 负债 800 − 800 = 0。未标主体那笔现金支出同时进了费用，所以未结转损益也是 900——
    # 合并后仍然平，这正是「不摊未标注分录、但也绝不漏掉它」的效果。
    assert out["combined"]["assets"] == "900.00"
    assert out["combined"]["liabilities"] == "0.00"
    assert out["combined"]["retained"] == "900.00"
    assert out["balanced"] is True, out.get("error")


def test_elimination_rows_go_to_the_elimination_column(tmp_path):
    entries = [
        _e("1100", "debit", "500", "E-A"),
        _e("2000", "credit", "500", "E-B"),
        # 已落账的抵销分录：不算进任何主体
        _e("2000", "debit", "500", None, source_ref="elim:2026-11"),
        _e("1100", "credit", "500", None, source_ref="elim:2026-11"),
    ]
    book, _policy = _book(tmp_path, entries)
    book.register_entity("E-A", "甲")
    book.register_entity("E-B", "乙")
    out = book.report(PERIOD)
    assert _cols(out)["E-A"]["assets"] == "500.00"
    assert out["elimination"]["posted"]["assets"] == "-500.00"
    # 已落账 500 已把两侧压平，**剩余**计算抵销必须为 0——否则第二次合并又抵一次，
    # 资产越并越小而报表照样平（负债同步变小），看不出错。
    assert out["elimination"]["computed"]["assets"] == "0.00"


def test_balanced_after_elimination(tmp_path):
    entries = [
        _e("1000", "debit", "2000", "E-A"), _e("3000", "credit", "2000", "E-A"),
        _e("1200", "debit", "900", "E-B"), _e("2000", "credit", "900", "E-B"),
        _e("1100", "debit", "900", "E-B"), _e("4000", "credit", "900", "E-B"),
    ]
    book, _policy = _book(tmp_path, entries)
    book.register_entity("E-A", "甲")
    book.register_entity("E-B", "乙")
    out = book.report(PERIOD)
    assert out["balanced"] is True, out.get("error")
    assert out["elimination"]["unmatched"] == []


def test_foreign_currency_entity_is_refused(tmp_path):
    book, _policy = _book(tmp_path, [_e("1000", "debit", "100", "E-U")])
    book.register_entity("E-U", "海外子公司", functional_currency="USD")
    with pytest.raises(TemplateError) as ei:
        book.report(PERIOD)
    assert ei.value.code == "NOT_WIRED"
    assert "折算率" in ei.value.detail["missing"]


def test_unimplemented_scope_and_model_are_refused(tmp_path):
    book, policy = _book(tmp_path, [])
    req = policy.propose({"elimination_scope": "plus-equity"}, requested_by="owner")
    policy.confirm(req["requestId"], decided_by="owner")
    with pytest.raises(TemplateError) as ei:
        book.report(PERIOD)
    assert ei.value.code == "NOT_WIRED"

    req = policy.propose({"elimination_scope": "intercompany-ar-ap",
                          "entity_model": "tenant-per-entity"}, requested_by="owner")
    policy.confirm(req["requestId"], decided_by="owner")
    with pytest.raises(TemplateError) as ei:
        book.report(PERIOD)
    assert "跨租户" in ei.value.message


# ---- 3) 抵销入账 ----

def test_propose_eliminations_posts_a_pending_entry(tmp_path):
    entries = [
        _e("1100", "debit", "1000", "E-A"),
        _e("2000", "credit", "800", "E-B"),
    ]
    book, _policy = _book(tmp_path, entries)
    book.register_entity("E-A", "甲")
    book.register_entity("E-B", "乙")
    out = book.propose_eliminations(PERIOD)
    assert out["amount"] == "800.00"
    assert out["stableId"] == "elim:2026-11"
    assert out["bookedOn"] == "2026-11-30"
    lines = out["proposal"]["lines"]
    assert [(l["account_code"], l["direction"], l["amount"]) for l in lines] == [
        ("2000", "debit", "800.00"), ("1100", "credit", "800.00")]
    assert out["proposal"]["source_ref"] == "elim:2026-11"
    assert out["unmatched"][0]["amount"] == "200.00"


def test_no_intercompany_means_no_proposal(tmp_path):
    book, _policy = _book(tmp_path, [_e("1000", "debit", "100", "E-A")])
    book.register_entity("E-A", "甲")
    out = book.propose_eliminations(PERIOD)
    assert out["noop"] is True
    assert out["amount"] == "0.00"

"""合并 × 外币折算（change: add-fx-translation 任务 3.3）。

判据（对应 spec `erp-consolidation` 的「合并结果自带平衡断言」与 `erp-fx-translation`）：

1. 口径 `none` 仍拒外币主体（既有场景不改）；`closing-rate` 才折算并入；
2. 折算进合并后仍平，且平衡里含折算差额（CTA 进权益，不进损益）；
3. 内部往来按**折算后**金额抵销——跨币种直接比大小会把抵销额算错；
4. `propose_eliminations` 与 `report` 同源（同一条折算路径），不出现两个抵销数；
5. 口径开了但汇率没登记 → 拒并点名缺什么。

零 DB：替身内核 + 真汇率登记表 + 真口径册。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.consolidation import ConsolidationBook
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.fx_revaluation import FxRevaluation
from fd_erp_vertical_templates.policy import PolicyBook

TENANT = "grp-fx"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, entries: list[dict]):
        self._entries = list(entries)
        self.submitted: list[dict] = []

    def query_entries(self, offset=0, limit=100, **_kw):
        return {"entries": self._entries[offset:offset + limit]}

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        lane_title=None, submitted_by="agent", booked_on=None):
        rec = {"stable_id": stable_id, "lines": lines, "source_ref": source_ref,
               "booked_on": booked_on}
        self.submitted.append(rec)
        return {**rec, "proposal_id": len(self.submitted), "duplicate": False}


def _e(code, direction, amount, entity, *, currency="CNY", period=PERIOD):
    dims = {"entity": entity} if entity else {}
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "currency": currency,
            "booked_on": f"{period}-15", "dimensions": dims}


def _sub_usd() -> list[dict]:
    """USD 主体自平一期：资产 1000（现金 900 + 应收 100）= 预收 400 + 权益 300 + 未结转 300。

    负债用 2100（预收暂收，对外）而不是 2000：内部往来抵销按科目识别，2000 只放真正的
    内部应付（在母公司那侧），免得把子公司的对外应付也一并抵掉。
    """
    return [
        _e("1000", "debit", "900", "SUB", currency="USD"),
        _e("1100", "debit", "100", "SUB", currency="USD"),
        _e("2100", "credit", "400", "SUB", currency="USD"),
        _e("3000", "credit", "300", "SUB", currency="USD"),
        _e("4000", "credit", "500", "SUB", currency="USD"),
        _e("5200", "debit", "200", "SUB", currency="USD"),
    ]


def _parent_cny() -> list[dict]:
    """CNY 母公司：现金 1000 = 应付 700 + 实收 300（应付里 700 是欠子公司的）。"""
    return [
        _e("1000", "debit", "1000", "PARENT"),
        _e("2000", "credit", "700", "PARENT"),
        _e("3000", "credit", "300", "PARENT"),
    ]


def _book(tmp_path, entries, *, mode="closing-rate", rates=True):
    policy = PolicyBook(TENANT, tmp_path)
    if mode != "none":
        req = policy.propose({"fx_translation": mode}, requested_by="owner")
        policy.confirm(req["requestId"], decided_by="owner")
    fx = FxRevaluation(TENANT, tmp_path, _StubKernel([]), base_currency="CNY")
    if rates:
        fx.register_rates(PERIOD, {"USD": "7.20"}, source="PBOC 2026-11-30")
        fx.register_rates(PERIOD, {"USD": "7.10"}, source="PBOC 日均",
                          kind="average")
    book = ConsolidationBook(TENANT, tmp_path, _StubKernel(entries), policy,
                             base_currency="CNY", fx=fx)
    book.register_entity("SUB", "海外子公司", functional_currency="USD")
    book.register_entity("PARENT", "母公司", functional_currency="CNY")
    return book, policy


# ---- 1) 口径决定拒还是折算 ----

def test_none_still_refuses_and_closing_rate_translates(tmp_path):
    entries = _sub_usd() + _parent_cny()

    refused, _p = _book(tmp_path / "a", entries, mode="none")
    with pytest.raises(TemplateError) as ei:
        refused.report(PERIOD)
    assert ei.value.code == "NOT_WIRED"
    assert "fx_translation" in ei.value.detail["fix"]

    ok, _p2 = _book(tmp_path / "b", entries)
    out = ok.report(PERIOD)
    assert out["fxTranslation"] == "closing-rate"
    assert out["translation"]["byEntity"][0]["entity"] == "SUB"
    assert out["translation"]["byEntity"][0]["difference"] == "30.00"


def test_temporal_is_refused_honestly(tmp_path):
    book, _p = _book(tmp_path, _sub_usd(), mode="temporal")
    with pytest.raises(TemplateError) as ei:
        book.report(PERIOD)
    assert ei.value.code == "NOT_WIRED"
    assert "时态法" in ei.value.message or "temporal" in ei.value.message


def test_missing_rates_are_refused_when_the_policy_is_on(tmp_path):
    book, _p = _book(tmp_path, _sub_usd(), rates=False)
    with pytest.raises(TemplateError) as ei:
        book.report(PERIOD)
    assert ei.value.code == "NOT_WIRED"
    assert ei.value.detail["currency"] == "USD"


# ---- 2) 折算后仍平，CTA 单列 ----

def test_translated_entities_consolidate_and_still_balance(tmp_path):
    book, _p = _book(tmp_path, _sub_usd() + _parent_cny())
    out = book.report(PERIOD)
    assert out["balanced"] is True, out.get("error")
    sub = next(c for c in out["entities"] if c["entity"] == "SUB")
    assert sub["assets"] == "7200.00" and sub["equity"] == "2160.00"
    assert sub["translationDifference"] == "30.00"
    assert sub["balanced"] is True
    # 折算差额进合并权益，不进未结转损益
    assert out["combined"]["translationDifference"] == "30.00"
    assert out["combined"]["retained"] == "2130.00"
    assert (Decimal(out["combined"]["assets"])
            == Decimal(out["combined"]["liabilities"])
            + Decimal(out["combined"]["equity"])
            + Decimal(out["combined"]["translationDifference"])
            + Decimal(out["combined"]["retained"]))


# ---- 3) 跨币种内部往来按折算后金额抵销 ----

def test_intercompany_is_eliminated_at_the_translated_amount(tmp_path):
    """子公司在 USD 账上挂应收 100（折 720），母公司挂应付 700：抵 700，差 20 单列。"""
    book, _p = _book(tmp_path, _sub_usd() + _parent_cny())
    out = book.report(PERIOD)
    assert out["elimination"]["computed"]["assets"] == "700.00"
    unmatched = out["elimination"]["unmatched"]
    assert unmatched and unmatched[0]["receivable"] == "720.00"
    assert unmatched[0]["payable"] == "700.00"
    assert unmatched[0]["amount"] == "20.00"
    # 抵销后在平
    assert out["balanced"] is True, out.get("error")
    assert Decimal(out["combined"]["assets"]) == Decimal("7500.00")


# ---- 4) 两条路径同源 ----

def test_propose_eliminations_uses_the_same_translated_amount(tmp_path):
    book, _p = _book(tmp_path, _sub_usd() + _parent_cny())
    reported = book.report(PERIOD)["elimination"]["computed"]["assets"]
    posted = book.propose_eliminations(PERIOD)
    assert posted["amount"] == reported == "700.00"
    assert posted["proposal"]["source_ref"] == "elim:2026-11"
    lines = {(l["account_code"], l["direction"]): l["amount"]
             for l in posted["proposal"]["lines"]}
    assert lines == {("2000", "debit"): "700.00", ("1100", "credit"): "700.00"}

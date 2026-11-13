"""外币报表折算（change: add-fx-translation 任务 1.2 / 2.3）。

判据（对应 spec `erp-fx-translation`）：

1. 期末与平均汇率同表追加式、各自独立修订；只登记期末时平均为空（不冒充）；
2. 三档口径各按各的汇率：资产/负债期末、损益平均、权益发生期期末；
3. 折算差额单列，不并入损益；折算后自平；
4. 缺平均汇率退回期末**并自报**（`averageFallback`）；
5. 缺汇率、混币分录一律拒并点名；主体账簿本身不平时 `balanced=false` 并报残差，
   不拿折算差额兜住真实错误。

零 DB：真 `FxRevaluation`（汇率登记）+ 替身内核。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.fx_revaluation import FxRevaluation
from fd_erp_vertical_templates.translation import TranslationBook

TENANT = "tpl-fx"
PERIOD = "2026-11"


class _StubKernel:
    def query_entries(self, offset=0, limit=100, **_kw):
        return {"entries": [], "rows_returned": 0}


def _fx(tmp_path) -> FxRevaluation:
    return FxRevaluation(TENANT, tmp_path / "data", _StubKernel(),
                         base_currency="CNY")


def _row(code, direction, amount, currency="USD", booked_on="2026-11-10"):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "currency": currency, "booked_on": booked_on,
            "posted_at": f"{booked_on}T09:00:00+08:00",
            "txn_id": f"t-{code}-{direction}-{amount}", "line_no": 1,
            "dimensions": {"entity": "SUB-US"}}


def _balanced_book() -> list[dict]:
    """USD 主体自平的一期：资产 1000 = 负债 400 + 权益 300 + 未结转 300。"""
    return [
        _row("1000", "debit", "1000"),
        _row("2000", "credit", "400"),
        _row("3000", "credit", "300"),
        _row("4000", "credit", "500"),
        _row("5200", "debit", "200"),
    ]


def _book(tmp_path, *, closing="7.20", average="7.10") -> tuple[TranslationBook, FxRevaluation]:
    fx = _fx(tmp_path)
    if closing is not None:
        fx.register_rates(PERIOD, {"USD": closing}, source="PBOC 2026-11-30")
    if average is not None:
        fx.register_rates(PERIOD, {"USD": average}, source="PBOC 2026-11 日均",
                          kind="average")
    return TranslationBook(TENANT, fx, base_currency="CNY"), fx


# ---- 1) 两类汇率 ----

def test_average_rate_is_empty_when_only_closing_is_registered(tmp_path):
    fx = _fx(tmp_path)
    fx.register_rates(PERIOD, {"USD": "7.15"}, source="PBOC")
    assert fx.rates(PERIOD)["rates"]["USD"]["rate"] == "7.15"
    assert str(fx.rates(PERIOD)["rates"]["USD"]["rate"]) == "7.15"
    # 平均汇率独立取数：空集，不拿期末汇率冒充
    assert fx.rates(PERIOD, kind="average")["rates"] == {}
    assert fx.rate_of(PERIOD, "USD", kind="average") is None


def test_average_rate_revisions_are_independent_of_closing(tmp_path):
    fx = _fx(tmp_path)
    fx.register_rates(PERIOD, {"USD": "7.20"}, source="PBOC")
    fx.register_rates(PERIOD, {"USD": "7.10"}, source="PBOC avg-1", kind="average")
    out = fx.register_rates(PERIOD, {"USD": "7.12"}, source="PBOC avg-2",
                            kind="average")
    assert out["kind"] == "average" and out["rates"]["USD"]["rate"] == "7.12"
    assert fx.rates(PERIOD, kind="average")["revisions"] == 2
    assert fx.rates(PERIOD)["revisions"] == 1              # 期末不被平均的修订污染
    assert fx.rates(PERIOD)["rates"]["USD"]["rate"] == "7.20"


def test_unknown_rate_kind_is_refused(tmp_path):
    with pytest.raises(TemplateError) as ei:
        _fx(tmp_path).register_rates(PERIOD, {"USD": "7.1"}, source="PBOC",
                                     kind="spot")
    assert ei.value.detail["allowed"] == ["closing", "average"]


# ---- 2) 三档口径 ----

def test_three_rate_calibers_and_the_translation_difference(tmp_path):
    book, _fx_ = _book(tmp_path)
    out = book.translate("SUB-US", PERIOD, _balanced_book(),
                         functional_currency="USD")
    assert out["balanced"] is True
    assert out["totals"]["assets"] == "7200.00"          # 1000 × 7.20（期末）
    assert out["totals"]["liabilities"] == "2880.00"     # 400 × 7.20
    assert out["totals"]["equity"] == "2160.00"          # 300 × 7.20（发生于本期）
    assert out["totals"]["revenue"] == "3550.00"         # 500 × 7.10（平均）
    assert out["totals"]["expense"] == "1420.00"         # 200 × 7.10
    assert out["totals"]["retained"] == "2130.00"
    assert out["totals"]["translationDifference"] == "30.00"
    assert out["averageFallback"] == []
    # 折算后每行都带原币与原额，可追溯
    first = out["rows"][0]
    assert first["originalCurrency"] == "USD"
    assert first["originalAmount"] == "1000"
    assert first["amount_base"] == "7200.00"


def test_translation_difference_is_not_pushed_into_profit(tmp_path):
    book, _fx_ = _book(tmp_path)
    out = book.translate("SUB-US", PERIOD, _balanced_book(),
                         functional_currency="USD")
    # 未结转损益只由损益科目决定，CTA 不进损益
    assert out["totals"]["retained"] == "2130.00"
    assert Decimal(out["totals"]["translationDifference"]) != 0
    assert (Decimal(out["totals"]["assets"])
            == Decimal(out["totals"]["liabilities"]) + Decimal(out["totals"]["equity"])
            + Decimal(out["totals"]["retained"])
            + Decimal(out["totals"]["translationDifference"]))


# ---- 3) 退回与拒绝 ----

def test_missing_average_falls_back_to_closing_and_says_so(tmp_path):
    book, _fx_ = _book(tmp_path, average=None)
    out = book.translate("SUB-US", PERIOD, _balanced_book(),
                         functional_currency="USD")
    assert out["totals"]["revenue"] == "3600.00"         # 500 × 7.20（退回期末）
    assert out["averageFallback"] == [{"currency": "USD", "period": PERIOD,
                                       "used": "7.20", "kind": "closing"}]
    assert "近似" in out["warning"]


def test_missing_closing_rate_is_refused(tmp_path):
    book, _fx_ = _book(tmp_path, closing=None, average="7.10")
    with pytest.raises(TemplateError) as ei:
        book.translate("SUB-US", PERIOD, _balanced_book(),
                       functional_currency="USD")
    assert ei.value.code == "NOT_WIRED"
    assert ei.value.detail["currency"] == "USD"


def test_mixed_currency_rows_are_refused_with_the_fix(tmp_path):
    book, _fx_ = _book(tmp_path)
    rows = _balanced_book() + [_row("1100", "debit", "50", currency="EUR")]
    with pytest.raises(TemplateError) as ei:
        book.translate("SUB-US", PERIOD, rows, functional_currency="USD")
    assert ei.value.code == "NOT_WIRED"
    assert ei.value.detail["mixedCurrency"] == {"EUR": 1}
    assert "调汇" in ei.value.message


def test_unmapped_account_is_refused(tmp_path):
    book, _fx_ = _book(tmp_path)
    with pytest.raises(TemplateError) as ei:
        book.translate("SUB-US", PERIOD,
                       _balanced_book() + [_row("9999", "debit", "1")],
                       functional_currency="USD")
    assert ei.value.detail["unclassified"] == ["9999"]


def test_an_unbalanced_entity_book_is_named_not_absorbed(tmp_path):
    book, _fx_ = _book(tmp_path)
    rows = [_row("1000", "debit", "1000"), _row("2000", "credit", "400"),
            _row("3000", "credit", "300"), _row("4000", "credit", "500"),
            _row("5200", "debit", "250")]                 # 费用多 50，书本身不平
    out = book.translate("SUB-US", PERIOD, rows, functional_currency="USD")
    assert out["balanced"] is False
    assert out["untranslated"]["residual"] == "50.00"
    assert "账簿本身不平" in out["error"]


# ---- 4) 多主体 ----

def test_translate_all_skips_entities_already_in_base_currency(tmp_path):
    book, _fx_ = _book(tmp_path)
    entities = [{"entityId": "SUB-US", "functionalCurrency": "USD"},
                {"entityId": "PARENT", "functionalCurrency": "CNY"}]
    out = book.translate_all(PERIOD, entities,
                             {"SUB-US": _balanced_book(),
                              "PARENT": [_row("1000", "debit", "10", currency="CNY")]})
    by = {e["entity"]: e for e in out["entities"]}
    assert by["SUB-US"]["translated"] is True
    assert by["PARENT"]["translated"] is False
    assert by["PARENT"]["rows"][0]["amount"] == "10"
    assert out["translationDifference"] == "30.00" and out["balanced"] is True


def test_translating_a_base_currency_entity_is_an_argument_error(tmp_path):
    book, _fx_ = _book(tmp_path)
    with pytest.raises(TemplateError):
        book.translate("PARENT", PERIOD, _balanced_book(),
                       functional_currency="CNY")

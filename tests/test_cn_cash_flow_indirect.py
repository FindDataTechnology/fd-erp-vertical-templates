"""补充资料（间接法）在准则口径下的自证（change: add-fi-indirect-and-fx-endpoints）。

判据（对应 spec `erp-cash-flow` 新增要求）：

1. 起点是**净利润**（含营业外收支与所得税费用），不是营业利润；
2. 两法互证：`indirectOperating == directOperating`、`difference` 为零、`consistent` 为真；
3. **与通用引擎那版同一组数**：两套科目码各算一遍必须相等（这条放在测试里，
   不放运行时——报表里对两遍只会让人以为真的比过）；
4. 营运资本用「借 − 贷」：应付增加是**来源**（减项），不是占用；
5. 调汇分录不计入营运资本（已通过汇兑损益加回），否则同一个数既加回又减掉；
6. 落不进归位表的科目让 `consistent` 为假并点名。

零 DB：替身内核按内核真实形状返回 entries。
"""
from __future__ import annotations

from fd_erp_vertical_templates.cn_statements import CnStatements
from fd_erp_vertical_templates.statements import StatementBook

TENANT = "tpl-cn"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, rows):
        self.rows = list(rows)

    def query_entries(self, offset=0, limit=500, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}


def _e(code, direction, amount, when="2026-11-10", txn=None, *, source_ref=None,
       currency=None):
    row = {"account_code": code, "direction": direction, "amount": amount,
           "amount_base": amount, "booked_on": when,
           "posted_at": f"{when}T09:00:00+08:00",
           "txn_id": txn or f"t-{code}-{direction}-{amount}", "line_no": 1,
           "dimensions": {}}
    if source_ref:
        row["source_ref"] = source_ref
    if currency:
        row["currency"] = currency
    return row


def _cash_month(extra=None):
    """现销 1130（净 1000 + 销项 130）+ 付现管理费用 200 + 计提未缴附加税 30。

    这三笔合起来才验得动间接法：有现金流入、有付现费用、还有**计提但没付**的税
    （利润已减、现金未动——漏了应交税费那一族，两条路径必然差一个税钱）。
    """
    rows = [
        _e("1000", "debit", "1130", txn="t-cash-sale"),
        _e("4000", "credit", "1000", txn="t-cash-sale"),
        _e("2221.01.01", "credit", "130", txn="t-cash-sale"),
        _e("5200", "debit", "200", txn="t-cash-exp"),
        _e("1000", "credit", "200", txn="t-cash-exp"),
        _e("5403", "debit", "30", txn="t-surcharge"),
        _e("2221.12", "credit", "30", txn="t-surcharge"),
    ]
    return rows + list(extra or [])


def _cn(rows=None) -> CnStatements:
    return CnStatements(TENANT, _StubKernel(_cash_month() if rows is None else rows))


def _gen(rows=None) -> StatementBook:
    return StatementBook(TENANT, _StubKernel(_cash_month() if rows is None else rows))


# ---- 1) 起点与两法互证 ----

def test_indirect_starts_from_net_profit_and_reconciles():
    out = _cn().cash_flow_indirect(PERIOD)
    # 净利润 = 1000 − 200 − 30 = 770
    assert out["netProfit"] == "770.00"
    assert out["indirectOperating"] == "930.00"      # 1130 − 200 的现金净流入
    assert out["directOperating"] == "930.00"
    assert out["difference"] == "0.00"
    assert out["consistent"] is True
    assert out["chart"] == "cn-small-business"


def test_net_profit_is_program_readable_not_label_read():
    """净利润取 `totals.netProfit`，不是从 `lines` 的中文行名里捞。"""
    isr = _cn().income_statement(PERIOD)
    assert isr["totals"]["netProfit"] == "770.00"
    net_line = [l for l in isr["lines"] if "净利润" in str(l.get("label"))]
    assert net_line and net_line[0]["amount"] == isr["totals"]["netProfit"]


def test_both_engines_agree_on_the_same_ledger():
    """两套科目码各算一遍：CN 版与通用版的经营净额必须相等。

    这条刻意放在测试而不是运行时（报表里算两遍只会让人以为真的比过）。
    """
    cn = _cn().cash_flow_indirect(PERIOD)
    gen = _gen().cash_flow_indirect(PERIOD)
    assert cn["indirectOperating"] == gen["indirectOperating"]
    assert cn["directOperating"] == gen["directOperating"]


# ---- 2) 营运资本的符号 ----

def test_payables_are_a_source_not_a_use():
    """应付增加 500：利润不变、现金不变 ⇒ 经营净额仍等于直接法。"""
    rows = _cash_month() + [
        _e("5200", "debit", "500", txn="t-ap"),
        _e("2000", "credit", "500", txn="t-ap"),
    ]
    out = _cn(rows).cash_flow_indirect(PERIOD)
    ap_row = next(r for r in out["workingCapital"] if r["accountCode"] == "2000")
    assert ap_row["change"] == "-500.00"             # 借 − 贷：负债增加为负
    assert out["difference"] == "0.00"
    assert out["consistent"] is True


def test_receivables_are_a_use():
    rows = _cash_month() + [
        _e("1100", "debit", "300", txn="t-ar"),
        _e("4000", "credit", "300", txn="t-ar"),
    ]
    out = _cn(rows).cash_flow_indirect(PERIOD)
    ar_row = next(r for r in out["workingCapital"] if r["accountCode"] == "1100")
    assert ar_row["change"] == "300.00"
    assert out["difference"] == "0.00"


def test_working_capital_rows_carry_the_statutory_code():
    out = _cn().cash_flow_indirect(PERIOD)
    ar = next(r for r in out["workingCapital"] if r["accountCode"] == "1100")
    assert ar["cnCode"] == "1122"                    # 应收账款
    assert ar["account"]                            # 准则科目名，不是空串


# ---- 3) 调汇不重复计入营运资本 ----

def test_fx_revaluation_entries_are_excluded_from_working_capital():
    """调汇把应收从 7000 调到 7200：差额必须只从「汇兑损益」加回，不再算进应收变动。"""
    from fd_erp_vertical_templates.statements import FX_REVAL_PREFIX

    rows = _cash_month() + [
        _e("1100", "debit", "200", txn="t-fx", source_ref=f"{FX_REVAL_PREFIX}2026-11"),
        _e("6603", "credit", "200", txn="t-fx", source_ref=f"{FX_REVAL_PREFIX}2026-11"),
    ]
    out = _cn(rows).cash_flow_indirect(PERIOD)
    ar = next(r for r in out["workingCapital"] if r["accountCode"] == "1100")
    assert ar["change"] == "0.00"                    # 调汇那 200 不算应收变动
    fx_row = next(r for r in out["adjustments"] if r["accountCode"] == "6603")
    assert fx_row["amount"] == "-200.00"             # 汇兑收益，贷方 → 减项
    assert out["difference"] == "0.00"
    assert out["consistent"] is True


# ---- 4) 对不上要如实报 ----

def test_unmapped_account_makes_it_inconsistent():
    """落不进现金流归位表的科目：两法净额可能相同，但报表仍判不自证。

    这是「绿得比绿还真」的反面——两法都算不出它，于是它们一起「看不见」这笔现金。
    """
    rows = _cash_month() + [
        _e("1000", "debit", "88", txn="t-weird"),
        _e("9999", "credit", "88", txn="t-weird"),
    ]
    out = _cn(rows).cash_flow_indirect(PERIOD)
    assert out["consistent"] is False
    assert out["unclassified"] == ["9999"]
    assert "未归位" in out["note"]

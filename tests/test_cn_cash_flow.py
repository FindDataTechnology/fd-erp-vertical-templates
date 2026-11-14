"""中国法定现金流量表（change: add-cn-cash-flow-statement）。

判据（对应 spec `erp-cn-localization` 新增要求「现金流量表与两套准则自重互证」）：

1. **三类净额之和 = 现金净增加额**：报表自己给出差额，不靠读者手算；
2. **期末现金 = 资产负债表「货币资金」**：两者由同一份「现金科目」定义推出，不是各算一遍；
3. **处置长期资产整笔归入投资活动**：卖设备是「现金/固定资产/处置收益」三条腿，
   按对手科目逐条归位会把收益那部分挪进经营——合计对得上，行次却是错的；
4. **两套准则在同一账本上给出同一净增加额**：准则差异只在行次与科目码，不在金额；
5. **同一账本与通用引擎净增加额一致**：两套科目码各算一遍，现金增减必须相同；
6. **归位表外的科目进 `unclassified` 并判不自证**：不猜类别、不调明细凑平；
7. 归位表/处置根写错时加载即拒（不静默少一行或永不触发）。

零 DB：替身内核返回内核真实形状。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from fd_erp_vertical_templates.cn_statements import CnStatements
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.localization import (BASE, CHART_LARGE, CHART_SMALL,
                                                    CnPack)
from fd_erp_vertical_templates.statements import StatementBook

TENANT = "tpl-cn-cash-flow"
PERIOD = "2026-11"


class _StubKernel:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}


def _e(code, direction, amount, txn, when="2026-11-10"):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "posted_at": f"{when}T09:00:00+08:00", "txn_id": txn, "line_no": 1,
            "dimensions": {}}


def _month() -> list[dict]:
    """一整月的现金收支（借贷必平）：

    t1 含税现销 1130（收入 1000 + 销项 130）、t2 含税现购 452（存货 400 + 进项 52）、
    t3 预收 300、t5 缴税 130、t6 股东出资 5000、t7 分配 500、t8 买设备 800、
    t9 处置设备收现 113（原值 80、已提折旧 20、销项 13、处置收益 40）、
    t10 付现费用 100。
    """
    return [
        _e("1000", "debit", "1130", "t1"), _e("4000", "credit", "1000", "t1"),
        _e("2221.01.01", "credit", "130", "t1"),
        _e("1200", "debit", "400", "t2"), _e("2221.01.02", "debit", "52", "t2"),
        _e("1000", "credit", "452", "t2"),
        _e("1000", "debit", "300", "t3"), _e("2100", "credit", "300", "t3"),
        _e("2221.01.01", "debit", "130", "t5"), _e("1000", "credit", "130", "t5"),
        _e("1000", "debit", "5000", "t6"), _e("3000", "credit", "5000", "t6"),
        _e("3100", "debit", "500", "t7"), _e("1000", "credit", "500", "t7"),
        _e("1601", "debit", "800", "t8"), _e("1000", "credit", "800", "t8"),
        _e("1000", "debit", "113", "t9"), _e("1602", "debit", "20", "t9"),
        _e("1601", "credit", "80", "t9"), _e("2221.01.01", "credit", "13", "t9"),
        _e("5301", "credit", "40", "t9"),
        _e("5100", "debit", "100", "t10"), _e("1000", "credit", "100", "t10"),
    ]


def _book(chart: str, rows: list[dict] | None = None) -> CnStatements:
    return CnStatements(TENANT, _StubKernel(_month() if rows is None else rows),
                        pack=CnPack(chart=chart))


def _bucket(flow, category):
    return flow["categories"][category]


# ---- 1) 三类净额之和 = 现金净增加额 ----

def test_the_three_categories_add_up_to_the_cash_change():
    flow = _book(CHART_SMALL).cash_flow(PERIOD)
    assert flow["consistent"] is True, flow.get("note") or flow.get("error")
    assert flow["sumCheck"]["ok"] is True, flow["sumCheck"]
    assert flow["sumCheck"]["difference"] == "0.00"
    # 现金：期初 0，收 1130+300+5000+113=6543，付 452+130+500+800+100=1982
    assert flow["netIncrease"] == "4561.00"
    assert flow["openingCash"] == "0.00"
    assert flow["closingCash"] == "4561.00"


def test_operating_lines_carry_the_business_flows_not_the_capital_ones():
    flow = _book(CHART_SMALL).cash_flow(PERIOD)
    operating = _bucket(flow, "operating")
    # 销售收现 1000 + 预收 300；货 400；税：销项 +130 +13、进项 -52、缴税 -130
    assert operating["net"] == "748.00", operating
    assert _bucket(flow, "investing")["net"] == "-687.00"   # 买设备 -800 + 处置 113
    assert _bucket(flow, "financing")["net"] == "4500.00"   # 出资 5000 − 分配 500


# ---- 2) 期末现金 = 资产负债表货币资金 ----

def test_closing_cash_equals_the_balance_sheet_cash_line():
    st = _book(CHART_SMALL)
    flow = st.cash_flow(PERIOD)
    bs = st.balance_sheet(PERIOD)
    cash = next(l for l in bs["assets"] if l["key"] == "cash")
    assert flow["balanceCheck"]["ok"] is True, flow["balanceCheck"]
    assert flow["balanceCheck"]["balanceSheetCash"] == cash["amount"] == "4561.00"


# ---- 3) 处置资产整笔归投资活动 ----

def test_a_disposal_is_one_investing_line_not_split_with_the_gain():
    flow = _book(CHART_SMALL).cash_flow(PERIOD)
    lines = {l["line"]: l for l in _bucket(flow, "investing")["lines"]}
    dispose = lines["处置固定资产、无形资产和其他长期资产收回的现金净额"]
    # 三条腿合计 113（现金）+ 20（折旧）+ 13（销项）+ 40（收益）= 净 113 全部进处置行
    assert dispose["net"] == "113.00"
    # 处置收益 40 不得作为经营流入出现：经营类里没有任何一行带 40
    assert all(l["net"] != "40.00" for l in _bucket(flow, "operating")["lines"])


def test_a_plain_purchase_of_assets_is_not_swept_into_the_disposal_line():
    # 买设备那笔（借 1601 / 贷 现金）也有长期资产腿，但它是借方——必须留在购建行
    flow = _book(CHART_SMALL).cash_flow(PERIOD)
    lines = {l["line"]: l for l in _bucket(flow, "investing")["lines"]}
    assert lines["购建固定资产、无形资产和其他长期资产支付的现金"]["outflow"] == "800.00"


# ---- 4) 两套准则同账本同数 ----

def test_both_charts_report_the_same_cash_change_on_one_ledger():
    small = _book(CHART_SMALL).cash_flow(PERIOD)
    large = _book(CHART_LARGE).cash_flow(PERIOD)
    for key in ("openingCash", "closingCash", "netIncrease", "categorizedNet"):
        assert small[key] == large[key], key
    for category in ("operating", "investing", "financing"):
        assert _bucket(small, category)["net"] == _bucket(large, category)["net"]
    assert large["chart"] == "cn-enterprise"
    assert large["consistent"] is True, large.get("note") or large.get("error")


# ---- 5) 与通用引擎同账本净增加额一致 ----

def test_the_generic_engine_agrees_on_the_net_cash_change():
    rows = _month()
    cn = _book(CHART_SMALL).cash_flow(PERIOD)
    generic = StatementBook(TENANT, _StubKernel(rows)).cash_flow(PERIOD)
    assert generic["netIncrease"] == cn["netIncrease"] == "4561.00"
    assert generic["closingCash"] == cn["closingCash"]
    # 通用引擎的归位表没有处置分支，那笔会被它判成未归类（consistent=False）——
    # 净增加额是现金余额的差，与归位表无关，两套科目码各算一遍必须相同。


# ---- 6) 表外科目判不自证，不猜 ----

def test_a_counterparty_outside_the_map_is_reported_instead_of_guessed():
    rows = _month() + [_e("1000", "debit", "60", "t11"),
                       _e("9999", "credit", "60", "t11")]
    flow = _book(CHART_SMALL, rows).cash_flow(PERIOD)
    assert flow["unclassified"] == ["9999"]
    assert flow["consistent"] is False
    assert flow["categorizedNet"] == "4561.00"     # 60 没被塞进任何一类
    assert flow["netIncrease"] == "4621.00"        # 现金确实多了 60
    assert flow["difference"] == "60.00"
    assert "9999" in flow["error"]


# ---- 7) 数据写错即拒 ----

def _copy_pack(tmp_path: Path) -> Path:
    dst = tmp_path / "cn"
    shutil.copytree(BASE, dst)
    return dst


def _resign(root: Path, name: str) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][name]["sha256"] = hashlib.sha256(
        (root / name).read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                        encoding="utf-8")


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d["cashFlow"]["map"].__setitem__(
        "9999", {"line": "op-sales", "why": "x"}), "9999"),
    (lambda d: d["cashFlow"]["map"].__setitem__(
        "1122", {"line": "no-such-line", "why": "x"}), "no-such-line"),
    (lambda d: d["cashFlow"]["disposal"].__setitem__("line", "no-such-line"),
     "no-such-line"),
    (lambda d: d["cashFlow"]["disposal"].__setitem__("roots", ["8888"]), "8888"),
    (lambda d: d["cashFlow"].__setitem__("cashLine", "no-such-line"), "no-such-line"),
    # 映射目标既不在归位表、也没声明 neverCash：那笔现金会静默落表外（11-14 大表 2203 就是这样）
    (lambda d: d["cashFlow"]["map"].pop("1122"), "1122"),
])
def test_broken_cash_flow_rules_are_refused_at_load(tmp_path, mutate, fragment):
    root = _copy_pack(tmp_path)
    path = root / "statements-small.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _resign(root, "statements-small.json")
    with pytest.raises(TemplateError) as ei:
        CnPack(root, chart=CHART_SMALL)
    assert fragment in ei.value.message

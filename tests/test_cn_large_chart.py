"""大企业（一般企业）准则报表与两准则互证（change: add-cn-large-chart-statements）。

判据（对应 spec `erp-cn-localization` 新增两条）：

1. **两套准则各自可出完整报表**：加载大表能按会企 01/02 表行次出资产负债表与利润表，
   `complete=true` 且 `balanced=true`；
2. **映射必须分表**：同一内核科目在两套准则里落不同码（4000 → 5001/6001、
   3000 → 3001/4001、1401 → 4001/5001、6602 → 5711/6721）；
3. **同一批分录两套报表金额一致**：净利润与资产总计逐值相等——准则差异只在呈现；
4. **抵减科目按抵减列示**：折旧 300 / 原值 1000 → 账面价值 700（不是 1300），表仍平；
5. 映射目标或行次科目不存在时加载即拒（不静默少一行）。

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

TENANT = "tpl-cn-large"
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
    """一个月的完整一套（借贷必平）：资本、固定资产与折旧、存货采购、含税赊销、
    成本结转、附加税、所得税、信用减值。"""
    return [
        _e("1000", "debit", "5000", "t1"), _e("3000", "credit", "5000", "t1"),
        _e("1601", "debit", "1200", "t2"), _e("1000", "credit", "1200", "t2"),
        _e("5200", "debit", "100", "t3"), _e("1602", "credit", "100", "t3"),
        _e("1200", "debit", "400", "t4"), _e("2221.01.02", "debit", "52", "t4"),
        _e("2000", "credit", "452", "t4"),
        _e("1100", "debit", "1130", "t5"), _e("4000", "credit", "1000", "t5"),
        _e("2221.01.01", "credit", "130", "t5"),
        _e("5000", "debit", "300", "t6"), _e("1200", "credit", "300", "t6"),
        _e("5403", "debit", "7.80", "t7"), _e("2221.12", "credit", "4.55", "t7"),
        _e("2221.13", "credit", "1.95", "t7"), _e("2221.14", "credit", "1.30", "t7"),
        _e("5801", "debit", "100", "t8"), _e("2221.11", "credit", "100", "t8"),
        _e("6602", "debit", "50", "t9"), _e("1231", "credit", "50", "t9"),
    ]


def _book(chart: str) -> CnStatements:
    return CnStatements(TENANT, _StubKernel(_month()), pack=CnPack(chart=chart))


def _line(rows, key):
    return next(r for r in rows if r["key"] == key)


# ---- 1) 两套准则都能出完整报表 ----

def test_the_large_chart_produces_complete_and_balanced_reports():
    st = _book(CHART_LARGE)
    bs = st.balance_sheet(PERIOD)
    inc = st.income_statement(PERIOD)
    assert bs["complete"] is True, bs.get("unmapped")
    assert inc["complete"] is True, inc.get("unmapped")
    assert bs["balanced"] is True, bs.get("error")
    assert bs["chart"] == "cn-enterprise"
    # 会企 02 表行次：营业收入取 6001、所得税费用取 6801
    assert _line(inc["lines"], "revenue")["amount"] == "1000.00"
    assert _line(inc["lines"], "income_tax")["amount"] == "100.00"
    assert inc["totals"]["netProfit"] == "442.20"


def test_the_small_chart_still_works_on_the_same_ledger():
    st = _book(CHART_SMALL)
    assert st.balance_sheet(PERIOD)["balanced"] is True
    assert st.income_statement(PERIOD)["totals"]["netProfit"] == "442.20"


# ---- 2) 映射必须分表 ----

def test_the_same_kernel_code_maps_differently_per_standard():
    small = CnPack(chart=CHART_SMALL).kernel_to_cn()
    large = CnPack(chart=CHART_LARGE).kernel_to_cn()
    assert set(small) == set(large)                     # 覆盖同一批内核科目
    assert (small["4000"], large["4000"]) == ("5001", "6001")
    assert (small["3000"], large["3000"]) == ("3001", "4001")
    assert (small["1401"], large["1401"]) == ("4001", "5001")
    assert (small["6602"], large["6602"]) == ("5711", "6721")
    assert (small["5801"], large["5801"]) == ("5801", "6801")


# ---- 3) 同一批分录两套报表金额一致 ----

def test_both_standards_agree_on_net_profit_and_total_assets():
    small = _book(CHART_SMALL)
    large = _book(CHART_LARGE)
    assert (small.income_statement(PERIOD)["totals"]["netProfit"]
            == large.income_statement(PERIOD)["totals"]["netProfit"] == "442.20")
    assert (small.balance_sheet(PERIOD)["totals"]["assets"]
            == large.balance_sheet(PERIOD)["totals"]["assets"] == "6080.00")
    assert (small.balance_sheet(PERIOD)["totals"]["liabilities"]
            == large.balance_sheet(PERIOD)["totals"]["liabilities"] == "637.80")


# ---- 4) 抵减科目按抵减列示（回归：sign 曾取两次负号） ----

def test_depreciation_reduces_non_current_assets_and_keeps_the_sheet_balanced():
    for chart in (CHART_SMALL, CHART_LARGE):
        st = _book(chart)
        bs = st.balance_sheet(PERIOD)
        assets = {r["key"]: r["amount"] for r in bs["assets"]}
        assert assets["fixed_assets_cost"] == "1200.00"
        assert assets["accumulated_depreciation"] == "-100.00"   # 抵减为负
        assert assets["fixed_assets_net"] == "1100.00"           # 不是 1300
        assert bs["balanced"] is True, f"{chart}: {bs.get('error')}"


# ---- 5) 数据写错即拒 ----

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


def test_a_mapping_target_missing_from_the_large_chart_is_refused(tmp_path):
    root = _copy_pack(tmp_path)
    path = root / "mapping-large.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["kernelToCn"]["4000"]["cn"] = "9999"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _resign(root, "mapping-large.json")
    with pytest.raises(TemplateError) as ei:
        CnPack(root, chart=CHART_LARGE)
    assert "9999" in ei.value.message


def test_a_statement_line_citing_a_missing_code_is_refused(tmp_path):
    root = _copy_pack(tmp_path)
    path = root / "statements-large.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for line in data["balanceSheet"]["assets"]:
        if line["key"] == "cash":
            line["codes"] = ["8888"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _resign(root, "statements-large.json")
    with pytest.raises(TemplateError) as ei:
        CnPack(root, chart=CHART_LARGE)
    assert "8888" in ei.value.message

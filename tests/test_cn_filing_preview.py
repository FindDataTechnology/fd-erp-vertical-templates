"""申报预演只算不写（change: add-cn-filing-endpoints）。

判据（对应 spec `erp-cn-localization` 新增要求「申报动作分读写两档，预演不写账」）：

1. **预演不写账**：`preview=True` 时内核待确认提案集合调用前后相同——读接口在账上留提案
   是最难查的副作用（谁点的、点两次是否重复，事后都说不清）；
2. **预演看得出是预演**：返回体 `proposed=false`，且不带 `stableId` / `proposal`；
3. **默认仍出提案**：`preview=False`（缺省）行为不变，后台作业照旧提提案；
4. **金额一致**：预演与真计提的数必须逐值相同——预演要是另一个数，它就只是安慰。
"""
from __future__ import annotations

from fd_erp_vertical_templates.cn_filing import CnFilingLane
from fd_erp_vertical_templates.cn_statements import CnStatements
from fd_erp_vertical_templates.cn_tax import CnVatLane
from fd_erp_vertical_templates.localization import CHART_SMALL, CnPack

TENANT = "tpl-cn-preview"
PERIOD = "2026-11"
RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


class _StubKernel:
    """记录提案，便于断言「预演一次都没提」。"""

    def __init__(self, rows: list[dict]):
        self.rows = list(rows)
        self.proposals: list[dict] = []

    def query_entries(self, offset=0, limit=200, **_kw):
        return {"entries": self.rows[offset:offset + limit],
                "rows_returned": len(self.rows)}

    def submit_proposal(self, stable_id, lines, **kw):
        self.proposals.append({"stable_id": stable_id, "lines": lines, **kw})
        return {"stable_id": stable_id, "status": "pending", **kw}


def _e(code, direction, amount, txn, when):
    return {"account_code": code, "direction": direction, "amount": amount,
            "amount_base": amount, "booked_on": when,
            "posted_at": f"{when}T09:00:00+08:00", "txn_id": txn, "line_no": 1,
            "dimensions": {}}


def _ledger() -> list[dict]:
    """含税现销 1130（收入 1000 + 销项 130）、含税现购 565（成本 500 + 进项 65）。"""
    return [
        _e("1000", "debit", "1130", "s1", "2026-11-10"),
        _e("4000", "credit", "1000", "s1", "2026-11-10"),
        _e("2221.01.01", "credit", "130", "s1", "2026-11-10"),
        _e("5000", "debit", "500", "p1", "2026-11-12"),
        _e("2221.01.02", "debit", "65", "p1", "2026-11-12"),
        _e("1000", "credit", "565", "p1", "2026-11-12"),
    ]


def _lane(tmp_path) -> tuple[CnFilingLane, _StubKernel]:
    kernel = _StubKernel(_ledger())
    statements = CnStatements(TENANT, kernel, pack=CnPack(chart=CHART_SMALL))
    lane = CnFilingLane(TENANT, tmp_path, kernel, vat=CnVatLane(TENANT, kernel),
                        statements=statements)
    lane.register_rates(RATES, effective_from="2026-01", source="测试：现行税法")
    return lane, kernel


# ---- 1) 预演不写账 ----

def test_previewing_surcharges_submits_no_proposal(tmp_path):
    lane, kernel = _lane(tmp_path)
    before = len(kernel.proposals)
    out = lane.surcharges(PERIOD, preview=True)
    assert len(kernel.proposals) == before
    assert out["total"] == "7.80"            # 应纳 65 × (7%+3%+2%)
    assert out["proposed"] is False
    assert "stableId" not in out and "proposal" not in out


def test_previewing_income_tax_submits_no_proposal(tmp_path):
    lane, kernel = _lane(tmp_path)
    before = len(kernel.proposals)
    out = lane.income_tax_prepay(PERIOD, preview=True)
    assert len(kernel.proposals) == before
    assert out["due"] == "125.00"            # 利润总额 500 × 25%
    assert out["proposed"] is False
    assert "stableId" not in out and "proposal" not in out


# ---- 2) 默认仍出提案 ----

def test_the_default_still_proposes(tmp_path):
    lane, kernel = _lane(tmp_path)
    out = lane.surcharges(PERIOD)
    assert out["proposed"] is True
    assert out["stableId"] == f"surcharge:{PERIOD}"
    assert len(kernel.proposals) == 1
    tax = lane.income_tax_prepay(PERIOD)
    assert tax["proposed"] is True
    assert len(kernel.proposals) == 2


# ---- 3) 预演与真计提逐值相同 ----

def test_preview_and_real_run_report_the_same_numbers(tmp_path):
    lane, _kernel = _lane(tmp_path)
    preview_s = lane.surcharges(PERIOD, preview=True)
    real_s = lane.surcharges(PERIOD)
    for key in ("base", "relief", "total"):
        assert preview_s[key] == real_s[key], key
    assert [i["amount"] for i in preview_s["items"]] == \
        [i["amount"] for i in real_s["items"]]

    preview_t = lane.income_tax_prepay(PERIOD, preview=True)
    real_t = lane.income_tax_prepay(PERIOD)
    for key in ("cumulativeProfit", "target", "alreadyPrepaid", "due", "rate"):
        assert preview_t[key] == real_t[key], key


# ---- 4) 留抵月份：预演与真跑都 noop，都不写 ----

def test_a_credit_month_previews_as_noop_without_writing(tmp_path):
    kernel = _StubKernel([
        _e("1200", "debit", "1000", "p1", "2026-11-12"),
        _e("2221.01.02", "debit", "130", "p1", "2026-11-12"),
        _e("1000", "credit", "1130", "p1", "2026-11-12"),
    ])
    statements = CnStatements(TENANT, kernel, pack=CnPack(chart=CHART_SMALL))
    lane = CnFilingLane(TENANT, tmp_path, kernel, vat=CnVatLane(TENANT, kernel),
                        statements=statements)
    lane.register_rates(RATES, effective_from="2026-01", source="测试")
    out = lane.surcharges(PERIOD, preview=True)
    assert out["noop"] is True
    assert out["proposed"] is False
    assert kernel.proposals == []

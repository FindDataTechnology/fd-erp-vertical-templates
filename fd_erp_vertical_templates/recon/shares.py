"""分成对账单与核对指令（erp-vertical-restaurant-chain 任务 3.6 / design D6）。

资金红线模板层落法：
- 模板只产出「双方可核对的分成对账单 + 核对指令数据」。
- 分账执行不由模板提供：输出数据是对接客户自选持牌分账通道的输入。
- 本模块刻意不提供任何资金执行 API —— assert_no_execution 面把
  「执行」类请求在模板层直接拒绝（SHARE_EXECUTION_DENIED）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..errors import TemplateError

#: 分成对账单 schema（双方核对口径）
SHARE_STATEMENT_SCHEMA = "erp-share-statement/v1"


def assert_no_execution(requested: dict[str, Any]) -> None:
    """资金执行面闸：任何执行类字段出现即拒绝（红线，不留旁门）。"""
    forbidden_keys = ("execute", "transfer", "payout_now", "executor",
                      "fund_channel", "auto_transfer")
    hits = [k for k in requested if str(k).lower() in forbidden_keys]
    if hits:
        raise TemplateError(
            "SHARE_EXECUTION_DENIED",
            f"模板不提供资金执行面（二清红线）：请求含执行类字段 {hits}。"
            "分成只出核对对账单与核对指令，资金划转由品牌方经持牌通道执行。",
            detail={"forbidden_keys": hits})


def build_share_statement(*, period: str, brand: str, franchisee: str,
                          daily_results: list[dict[str, Any]],
                          share_terms: dict[str, Any]) -> dict[str, Any]:
    """分成对账单：逐店逐日 revenue/fees/net/share，双方可核对。

    daily_results: 各店日结 report（restaurant.StoreDailyClose.daily_close 输出，
    仅限加盟店 operation_mode=franchise）。
    share_terms: {"brand_rate": "0.20", "base": "net_after_fees",
                  "rate_source": "..."} —— 费率/口径须带出处，无出处如实标注。
    """
    brand_rate = str(share_terms.get("brand_rate") or "")
    if not brand_rate:
        raise TemplateError("INVALID_ARGUMENT", "分成费率 brand_rate 必填（带出处）")
    rate_source = str(share_terms.get("rate_source") or "")
    if not rate_source:
        raise TemplateError(
            "INVALID_ARGUMENT",
            "分成费率须带出处（rate_source）——无出处的分成口径双方无法核对")

    rows: list[dict[str, Any]] = []
    totals = {"revenue": Decimal(0), "fees": Decimal(0), "net": Decimal(0),
              "brand_share": Decimal(0), "franchisee_share": Decimal(0)}
    for r in daily_results:
        store_id = r.get("store_id")
        report = r.get("report") or {}
        flows = report.get("flows") or {}
        receipts = report.get("receipts") or {}
        revenue = Decimal(flows.get("pos_total") or "0")
        fees = sum((Decimal((v or {}).get("fee_total") or "0")
                    for k, v in receipts.items() if k != "delivery"), Decimal(0))
        net = revenue - fees
        brand_share = (net * Decimal(brand_rate)).quantize(Decimal("0.01"))
        franchisee_share = net - brand_share
        rows.append({
            "store_id": store_id, "day": r.get("day"),
            "revenue": str(revenue), "fees": str(fees), "net": str(net),
            "brand_share": str(brand_share), "franchisee_share": str(franchisee_share),
            "diff_count": (report.get("diffs") or {}).get("diff_count", 0),
            "note": "有未拍板差异的期间分成行须以差异清零为前提（核对岗把关）",
        })
        totals["revenue"] += revenue
        totals["fees"] += fees
        totals["net"] += net
        totals["brand_share"] += brand_share
        totals["franchisee_share"] += franchisee_share

    return {
        "schema": SHARE_STATEMENT_SCHEMA,
        "period": period, "brand": brand, "franchisee": franchisee,
        "share_terms": {"brand_rate": brand_rate, "rate_source": rate_source,
                        "base": str(share_terms.get("base") or "net_after_fees")},
        "rows": rows,
        "totals": {k: str(v) for k, v in totals.items()},
        "both_party_verifiable": True,
        "execution_face": "none —— 模板不含资金执行面（二清红线）；"
                          "资金划转由品牌方经客户自选持牌通道执行",
    }


def build_verification_instructions(statement: dict[str, Any]) -> list[dict[str, Any]]:
    """核对指令数据：对接持牌分账产品的输入面（数据，不是执行）。

    每条指令 = {instruction_id, payee, period, amount, references}：
    持牌通道核对该数据后自行执行划转；平台不经手资金。
    """
    assert_no_execution(statement.get("_requested") or {})
    instructions = []
    for i, row in enumerate(statement.get("rows") or []):
        instructions.append({
            "instruction_id": f"VRF-{statement.get('period')}-{row.get('store_id')}-{i + 1}",
            "type": "verification-data",
            "payee": row.get("store_id"),
            "payer": statement.get("brand"),
            "period": statement.get("period"),
            "amount": row.get("brand_share"),
            "currency": "CNY",
            "references": {"statement_schema": statement.get("schema"),
                           "day": row.get("day"),
                           "diff_count": row.get("diff_count")},
            "note": "核对指令数据：由持牌通道消费；平台不执行划转",
        })
    return instructions

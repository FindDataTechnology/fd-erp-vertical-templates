"""承运商运费单车道（change: add-carrier-freight-dimension tasks 3.x）。

三件事，每条都对着 spec `erp-carrier-freight-lane` 的一条要求：

1. **按运单幂等计费**：`stable_id = <运单号>:freight`，同运单重推返回首次结果；
   计费时把 `{waybill, carrier, period}` 放进 `refs`——事件表没有 refs 列，而费用行的
   溯源就取这份 refs，承运商维度只能这样落下来。
2. **按承运商归集**：只认「费用行 refs 里带承运商」的行。没有承运商维度的费用**不进**
   运费单——宁可空，也不编归集（编出来的运费单比没有更糟）。
3. **确认走内核提案车道**：复用 `writeback_recon_conclusion`（自平核对记录，净额为零，
   不改变任何余额），状态不落本地；已拍板的单再拍如实失败，不静默覆盖。

本模块零直连库表：一切经 `KernelClient`。
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .errors import TemplateError
from .recon.writeback import writeback_recon_conclusion

Q6 = Decimal("0.000001")
#: 单号形如 `FS-<carrier>-<period>`；period 是 ISO 日（含连字符），所以从尾部锚定。
_STATEMENT_RE = re.compile(r"^FS-(?P<carrier>.+)-(?P<period>\d{4}-\d{2}-\d{2})$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q6, rounding=ROUND_HALF_UP))


class FreightStatementLane:
    """运费单：计费（按运单）→ 归集（按承运商+期间）→ 确认（内核提案车道）。"""

    def __init__(self, tenant_id: str, kernel: Any, *, base_currency: str = "CNY",
                 fleet: str = "cp-freight", post: str = "freight-statement"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._base = base_currency
        self._fleet = fleet
        self._post = post

    # ---- 1) 计费 ----

    def bill_delivery(self, *, waybill: str, carrier: str, occurred_at: str,
                      measures: dict[str, Any]) -> dict[str, Any]:
        """按运单计费（幂等）。`refs` 是归集的唯一依据，period 取交付日（业务时点）。"""
        waybill = str(waybill or "").strip()
        carrier = str(carrier or "").strip()
        if not waybill:
            raise TemplateError("INVALID_ARGUMENT", "缺运单号，无法计费")
        if not carrier:
            # 没有承运商就无法按承运商归集——这不是「未知可放行」，是缺关键维度
            raise TemplateError("INVALID_ARGUMENT",
                                f"运单 {waybill} 缺承运商，无法计运费")
        event = {
            "stable_id": f"{waybill}:freight",
            "kind": "delivery",
            "occurred_at": str(occurred_at),
            "measures": dict(measures or {}),
            "refs": {"waybill": waybill, "carrier": carrier,
                     "period": str(occurred_at)[:10]},
        }
        return self._kernel.bill_event(event)

    # ---- 2) 归集 ----

    def _fee_rows(self, *, max_pages: int = 40) -> tuple[list[dict[str, Any]], bool]:
        """读全部带承运商的费用行（分页读完）。返回 (行, 是否被页数上限截断)。

        **两种返回形态都要认**：MCP 工具面是 `paginate(...)` → `data/total/truncated`；
        模板层 in-process 测试替身是 `fees/rows_returned`。只认一种就会在生产侧静默
        读到空表——本仓已经踩过「跨入口形状漂移」：单侧测试全绿、真链读空。
        """
        page = 500
        rows: list[dict[str, Any]] = []
        truncated = False
        offset = 0
        for _ in range(max_pages):
            out = self._kernel.list_fees(offset=offset, limit=page) or {}
            batch = out.get("data")
            if not isinstance(batch, list):
                batch = out.get("fees") or []
            batch = [r for r in batch if isinstance(r, dict)]
            if not batch:
                break
            rows.extend(batch)
            offset += len(batch)
            total = out.get("total")
            if isinstance(total, int):
                if offset >= total:
                    break
                continue  # 还有：按完整性声明继续翻
            if out.get("truncated") is True:
                continue
            if out.get("truncated") is False:
                break
            # 旧形态（无 total 也无 truncated）：只能按「本页没满足请求页大小」收尾
            if len(batch) < page:
                break
        else:
            truncated = True  # 页数上限用尽：如实标记，不把读到的当成全部
        return ([r for r in rows
                 if isinstance(r.get("refs"), dict)
                 and str(r["refs"].get("carrier") or "").strip()], truncated)

    @staticmethod
    def statement_id(carrier: str, period: str) -> str:
        return f"FS-{carrier}-{period}"

    def statements(self, *, carrier: str | None = None, period: str | None = None
                   ) -> dict[str, Any]:
        """按（承运商, 期间）归集。红冲行是负额，自然净额抵消，不额外过滤。"""
        fees, truncated = self._fee_rows()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in fees:
            refs = row["refs"]
            car = str(refs.get("carrier") or "").strip()
            per = str(refs.get("period") or "").strip()
            if carrier and car != carrier:
                continue
            if period and per != period:
                continue
            groups.setdefault((car, per), []).append(row)
        rows: list[dict[str, Any]] = []
        for (car, per), items in sorted(groups.items()):
            total = sum((Decimal(str(i.get("amount") or "0")) for i in items), Decimal(0))
            rows.append({
                "statementId": self.statement_id(car, per),
                "carrier": car,
                "period": per,
                "waybillCount": len({str((i.get("refs") or {}).get("waybill") or "")
                                     for i in items}),
                "feeCount": len(items),
                "total": _fmt(total),
                "currency": str(items[0].get("currency") or self._base),
            })
        note = ("只统计带承运商的费用行；无承运商维度的费用不进运费单"
                if rows else "本期间没有带承运商的计费事实")
        if truncated:
            note += "；费用行读取触及页数上限，本结果可能不全（不是空表）"
        return {"rows": rows, "total": len(rows), "truncated": truncated, "note": note}

    def statement(self, statement_id: str) -> dict[str, Any]:
        """单张运费单明细：按运单聚合，每行带费用溯源（fee_id/规则版本/单据引用）。"""
        m = _STATEMENT_RE.match(str(statement_id or "").strip())
        if m is None:
            raise TemplateError("NOT_FOUND", f"运费单号格式不合法：{statement_id}",
                                detail={"expected": "FS-<carrier>-<YYYY-MM-DD>"})
        carrier, period = m.group("carrier"), m.group("period")
        fees, _ = self._fee_rows()
        items = [r for r in fees
                 if str((r.get("refs") or {}).get("carrier") or "") == carrier
                 and str((r.get("refs") or {}).get("period") or "") == period]
        if not items:
            raise TemplateError("NOT_FOUND", f"运费单不存在：{statement_id}")
        by_waybill: dict[str, list[dict[str, Any]]] = {}
        for row in items:
            refs = row.get("refs") or {}
            by_waybill.setdefault(str(refs.get("waybill") or ""), []).append(row)
        lines: list[dict[str, Any]] = []
        total = Decimal(0)
        for waybill, fees in sorted(by_waybill.items()):
            amount = sum((Decimal(str(f.get("amount") or "0")) for f in fees), Decimal(0))
            total += amount
            lines.append({
                "waybill": waybill,
                "total": _fmt(amount),
                "fees": [{"feeId": f.get("fee_id"), "feeItem": f.get("fee_item"),
                          "amount": f.get("amount"), "currency": f.get("currency"),
                          "ruleId": f.get("rule_id"),
                          "ruleVersion": f.get("rule_version"),
                          "status": f.get("status"),
                          "refs": f.get("refs") or {}} for f in fees],
            })
        return {"statementId": statement_id, "carrier": carrier, "period": period,
                "total": _fmt(total), "currency": str(items[0].get("currency") or self._base),
                "waybillCount": len(by_waybill), "lines": lines}

    # ---- 3) 确认（内核提案车道）----

    def _settled(self, statement_id: str) -> dict[str, Any] | None:
        """该运费单是否已有落定的核对结论（确认/驳回都算拍板）。

        为什么不靠 `submit_proposal` 的幂等返回拦「再拍」：`stable_id` 按结论类型派生
        （confirmed / disputed 是两条不同记录），所以「先确认、后改判驳回」撞不上幂等 ——
        那恰恰是最该拦的一种再拍，两条结论并存会让同一张单同时是「确认」和「有异议」。
        所以按单据引用扫落定状态。
        """
        needle = f":statement:{statement_id}->"
        for status in ("confirmed", "rejected"):
            out = self._kernel.list_proposals(status=status, limit=500) or {}
            rows = out.get("data") or out.get("proposals") or []
            for row in rows:
                if isinstance(row, dict) and needle in str(row.get("source_ref") or ""):
                    return row
        return None

    def confirm(self, statement_id: str, decision: str, *, note: str | None = None,
                confirmed_by: str = "human") -> dict[str, Any]:
        """确认/驳回一张运费单。

        - 驳回 MUST 附 note（与开票审核同一条纪律）；
        - 落库形态是**自平核对记录**（借贷同额、净额为零），不改任何余额；
        - 已拍板再拍：内核按 stable_id 幂等回既有提案，我们不静默吞掉——如实报冲突。
        """
        decision = str(decision or "").strip().lower()
        if decision not in ("accepted", "rejected"):
            raise TemplateError("INVALID_ARGUMENT",
                                "decision 须为 accepted 或 rejected",
                                detail={"decision": decision})
        note = str(note or "").strip()
        if decision == "rejected" and not note:
            raise TemplateError("INVALID_ARGUMENT",
                                "驳回必须写明原因（note）——承运商与运营据此处置")
        detail = self.statement(statement_id)  # 不存在即 NOT_FOUND
        # 已拍板再拍：如实报冲突（不论这次结论与上次是否同类），不静默改判
        settled = self._settled(statement_id)
        if settled is not None:
            raise TemplateError(
                "WORKFLOW_STATE",
                f"运费单已拍板（{settled.get('status')}）：{statement_id}",
                detail={"statementId": statement_id,
                        "status": settled.get("status"),
                        "proposalId": settled.get("id")})
        total = Decimal(str(detail["total"]))
        diff = {
            "kind": ("freight_statement_confirmed" if decision == "accepted"
                     else "freight_statement_disputed"),
            "left_ref": f"statement:{statement_id}",
            "right_ref": f"carrier:{detail['carrier']}",
            "explanation_hint": (f"运费单 {statement_id}：{detail['waybillCount']} 张运单，"
                                 f"合计 {detail['total']} {detail['currency']}"),
        }
        marker = [{"account_code": "1300", "direction": "debit",
                   "amount": _fmt(total), "currency": detail["currency"]},
                  {"account_code": "1300", "direction": "credit",
                   "amount": _fmt(total), "currency": detail["currency"]}]
        out = writeback_recon_conclusion(
            self._kernel, tenant_id=self._tenant, fleet=self._fleet, post=self._post,
            title=f"{detail['carrier']} 运费单 {detail['period']}",
            diff=diff, proposal_lines=marker,
            decision_note=(f"运费单{('确认' if decision == 'accepted' else '异议')}：{note}"
                           if note else "运费单核对确认（自平记录，净额为零）"),
            proposed_by=confirmed_by)
        if out.get("duplicate") and str(out.get("status") or "") != "pending":
            # 已拍板（confirmed/rejected）再拍：如实报冲突，不静默覆盖
            raise TemplateError(
                "WORKFLOW_STATE",
                f"运费单已拍板（{out.get('status')}）：{statement_id}",
                detail={"statementId": statement_id, "status": out.get("status")})
        return {"statementId": statement_id, "decision": decision,
                "status": str(out.get("status") or "pending"),
                "proposalId": out.get("proposal_id"),
                "total": detail["total"], "carrier": detail["carrier"],
                "period": detail["period"], "note": note or None,
                "confirmedBy": confirmed_by}

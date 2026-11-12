"""开票回执车道（change: add-invoice-receipt-lane 任务 1.x）。

申请侧已经闭环到「审核通过」，但票开出来之后没有地方登记——「谁还没开票」只能靠人对表格。
本车道补这一半：**回执录入 + 号码唯一 + 金额核对 + 未开票清单**。

## 回执为什么不进内核

票据本体是客户合规渠道（电子税局/金蝶/线下）的产物，不是账务事实。进内核会造出
「一张票 = 一笔分录」的伪耦合——开票与收入确认本来就不是一件事。所以回执落模板层
append-only 档，**申请状态仍读内核投影**：状态只有一份真源，回执是另一类事实，不复制状态。

## 三条不许松的核对

- **顺序**：只能给 `accepted` 的申请录回执。先开票后补审核等于绕开确认门（design D2）。
- **号码**：同租户全票号唯一，撞号必拒并点名归属哪个申请——同号两张票必有一张错，
  而错的那张会让客户抵扣出问题。作废的号不占位（税务上可作废后重开），作废留档（D3）。
- **金额**：推定金额由调用方给（运营页面/对账单），本车道不自己推——推错会把「系统算的对」
  变成假证据。没给推定值就是 `unverified`，**不是**「相符」；不符也照样入档，
  只标差额（票已经开了，事实不能丢）（D4）。

## 幂等与作废

同（申请, 号码, 金额）重复登记返回首次结果；作废是追加一条 `void` 记录、原回执永久留档，
未开票清单按折叠后的有效回执计算（D5）。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog

Q2 = Decimal("0.01")
RECEIPT_SCHEMA = "fd-erp-vertical-templates/invoice-receipt/v1"

#: 申请事件类型（与 orchestration.INVOICE_REQUEST_EVENT 同一串；此处不 import 以免循环依赖）。
INVOICE_REQUEST_EVENT = "invoice_requested"
#: 回执录入来源（封闭集）：线下/电子税局手动录，或金蝶直连回调。
CHANNELS = ("offline", "etax", "kingdee")
_INVOICE_NO_RE = re.compile(r"^[0-9A-Za-z\-]{4,64}$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: Any) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise TemplateError("INVALID_ARGUMENT", "开票日期不能为空")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise TemplateError("INVALID_ARGUMENT",
                            f"开票日期不是合法日期：{value!r}（须 YYYY-MM-DD）") from exc


class InvoiceReceiptLane:
    """开票回执：录入、号码唯一、金额核对、未开票清单、作废留痕。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any):
        self._tenant = tenant_id
        self._kernel = kernel
        self._log = EventLog(Path(data_dir) / f"invoice-receipts-{tenant_id}.jsonl")

    # ---- 读面 ----

    def _records(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self._log.events()
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def _receipt_state(self) -> dict[str, Any]:
        """按档序折叠：作废只抵消它之前的回执；作废后重录的照常生效。"""
        active: dict[str, dict[str, Any]] = {}
        voided: dict[str, dict[str, Any]] = {}
        for rec in self._log.events():
            kind = rec.get("kind")
            # 键用规范化后的票号：号码唯一性是大小写不敏感的，按原串折叠会漏判撞号
            key = rec.get("key") or str(rec.get("invoiceNo") or "").strip().upper()
            if kind == "receipt":
                active[key] = rec
            elif kind == "void":
                hit = active.pop(key, None)
                if hit is not None:
                    voided[key] = {**hit, **rec}
        return {"active": active, "voided": voided}

    def _requests(self) -> dict[str, dict[str, Any]]:
        """申请投影：stable_id → 事件视图（读内核，不缓存）。"""
        out: dict[str, dict[str, Any]] = {}
        after = 0
        while True:
            resp = self._kernel.query_events(event_type=INVOICE_REQUEST_EVENT,
                                             limit=200, after_event_id=after) or {}
            page = [e for e in (resp.get("items") or resp.get("events") or [])
                    if isinstance(e, dict)]
            if not page:
                break
            for ev in page:
                key = str(ev.get("stable_id") or "").strip()
                if key:
                    out[key] = ev
            nxt = max((int(e.get("id") or 0) for e in page), default=0)
            if nxt <= after:
                break
            after = nxt
            if len(page) < 200:
                break
        return out

    def _request(self, request_ref: str) -> dict[str, Any]:
        key = str(request_ref or "").strip()
        if not key:
            raise TemplateError("INVALID_ARGUMENT", "开票申请号不能为空")
        hit = self._requests().get(key)
        if hit is None:
            raise TemplateError("NOT_FOUND", f"没有该开票申请：{key}",
                                detail={"requestRef": key})
        return hit

    # ---- 录入 ----

    def record(self, request_ref: str, invoice_no: str, issued_on: Any, amount: Any,
               *, tax_amount: Any = 0, channel: str = "offline",
               expected_amount: Any = None, currency: str = "CNY",
               operator: str = "ops") -> dict[str, Any]:
        """登记一张已开出的票（只对已通过审核的申请）。"""
        event = self._request(request_ref)
        ref = str(request_ref).strip()
        status = str(event.get("status") or "").strip()
        if status != "accepted":
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"该申请当前状态为 {status or '未知'}，不能登记回执——"
                f"先过确认门再开票（顺序不许倒）",
                detail={"requestRef": ref, "status": status})

        no = str(invoice_no or "").strip()
        if not no:
            raise TemplateError("INVALID_ARGUMENT", "发票号码不能为空")
        if not _INVOICE_NO_RE.match(no):
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"发票号码形态不符（4-64 位数字/字母/短横线）：{no!r}")
        key = no.upper()
        amount_v = _dec(amount, "价税合计")
        if amount_v <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"价税合计必须大于 0：{amount_v}")
        tax_v = _dec(tax_amount, "税额") if tax_amount not in (None, "") else Decimal(0)
        if tax_v < 0:
            raise TemplateError("INVALID_ARGUMENT", f"税额不能为负：{tax_v}")
        if tax_v > amount_v:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"税额 {_fmt(tax_v)} 大于价税合计 {_fmt(amount_v)}——金额填反了？",
                detail={"amount": _fmt(amount_v), "taxAmount": _fmt(tax_v)})
        when = _parse_date(issued_on)
        src = str(channel or "").strip()
        if src not in CHANNELS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"回执录入来源不在册：{src!r}",
                                detail={"allowed": list(CHANNELS)})
        cur = str(currency or "").strip().upper() or "CNY"

        state = self._receipt_state()
        existing = state["active"].get(key)
        if existing is not None:
            if (existing["requestRef"] == ref
                    and _dec(existing["amount"], "价税合计") == amount_v):
                return {"invoiceNo": existing["invoiceNo"], "duplicate": True,
                        "requestRef": existing["requestRef"],
                        "amount": existing["amount"],
                        "note": "同一申请、同一号码、同一金额已登记过，返回首次结果"}
            if existing["requestRef"] == ref:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"号码 {no} 已在该申请下登记过（金额 {existing['amount']}），"
                    f"要改金额请先作废再重录",
                    detail={"invoiceNo": no, "requestRef": ref,
                            "registeredAmount": existing["amount"]})
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"号码 {no} 已归属申请 {existing['requestRef']}——同号两张票必有一张错",
                detail={"invoiceNo": no, "ownerRef": existing["requestRef"],
                        "requestRef": ref})

        verdict = "unverified"
        delta: str | None = None
        if expected_amount not in (None, ""):
            expected = _dec(expected_amount, "推定金额")
            delta_v = (amount_v - expected).quantize(Q2, rounding=ROUND_HALF_UP)
            verdict = "matched" if delta_v == 0 else "mismatched"
            delta = _fmt(delta_v)

        rec = {"kind": "receipt", "invoiceNo": no, "key": key,
               "requestRef": ref, "customerId": (event.get("payload") or {}).get("customer_id"),
               "issuedOn": when.isoformat(), "amount": _fmt(amount_v),
               "taxAmount": _fmt(tax_v), "currency": cur, "channel": src,
               "match": verdict, "difference": delta,
               "expectedAmount": (None if expected_amount in (None, "")
                                  else _fmt(_dec(expected_amount, "推定金额"))),
               "operator": str(operator or "").strip() or "ops",
               "recorded_at": _now()}
        self._log.append(rec)
        out = {"invoiceNo": no, "duplicate": False, "requestRef": ref,
               "issuedOn": rec["issuedOn"], "amount": rec["amount"],
               "taxAmount": rec["taxAmount"], "amountWithoutTax": _fmt(amount_v - tax_v),
               "currency": cur, "channel": src, "match": verdict}
        if delta is not None:
            out["difference"] = delta
            out["expectedAmount"] = rec["expectedAmount"]
            if verdict == "mismatched":
                out["warning"] = (f"与推定金额差 {delta}——票已开出，回执照录；"
                                  f"请核对单据或走票据作废重开")
        else:
            out["note"] = ("未给推定金额，核对结论为 unverified（不报「相符」）——"
                           "要比对请带上单据金额")
        return out

    def void(self, invoice_no: str, *, reason: str, voided_by: str = "owner"
             ) -> dict[str, Any]:
        """作废一条回执（录错/票作废）；原回执留档，不物理删除。"""
        no = str(invoice_no or "").strip()
        if not no:
            raise TemplateError("INVALID_ARGUMENT", "发票号码不能为空")
        note = str(reason or "").strip()
        if not note:
            raise TemplateError("INVALID_ARGUMENT", "作废回执须说明原因（税务动作要留痕）")
        state = self._receipt_state()
        key = no.upper()
        if key in state["voided"] and key not in state["active"]:
            return {"invoiceNo": no, "duplicate": True, "note": "该号码已作废过"}
        hit = state["active"].get(key)
        if hit is None:
            raise TemplateError("NOT_FOUND", f"没有该号码的回执：{no}",
                                detail={"invoiceNo": no})
        self._log.append({"kind": "void", "invoiceNo": hit["invoiceNo"], "key": key,
                          "reason": note, "voided_by": str(voided_by or "owner"),
                          "recorded_at": _now()})
        return {"invoiceNo": hit["invoiceNo"], "duplicate": False, "reason": note,
                "requestRef": hit["requestRef"], "amount": hit["amount"],
                "note": "已作废：未开票清单恢复，号码可重新登记"}

    # ---- 查询 ----

    def receipts_for(self, request_ref: str) -> list[dict[str, Any]]:
        ref = str(request_ref or "").strip()
        rows = [r for r in self._receipt_state()["active"].values()
                if r["requestRef"] == ref]
        rows.sort(key=lambda r: r["issuedOn"])
        return [{"invoiceNo": r["invoiceNo"], "requestRef": r["requestRef"],
                 "issuedOn": r["issuedOn"],
                 "amount": r["amount"], "taxAmount": r["taxAmount"],
                 "channel": r["channel"], "match": r.get("match"),
                 "difference": r.get("difference"), "operator": r.get("operator")}
                for r in rows]

    def unissued(self, customer_ids: Any) -> dict[str, Any]:
        """已通过但尚无有效回执的申请（`unverified` 与「没开票」是两件事，分开看）。"""
        ids = {str(c) for c in (customer_ids or []) if str(c).strip()}
        if not ids:
            raise TemplateError("INVALID_ARGUMENT",
                                "未开票清单须给客户集合（不列全租户）")
        active = self._receipt_state()["active"]
        covered = {r["requestRef"] for r in active.values()}
        rows: list[dict[str, Any]] = []
        for ref, ev in self._requests().items():
            payload = ev.get("payload") or {}
            if str(payload.get("customer_id") or "") not in ids:
                continue
            if str(ev.get("status") or "") != "accepted":
                continue
            if ref in covered:
                continue
            rows.append({"requestRef": ref,
                         "customerId": payload.get("customer_id"),
                         "title": payload.get("title"),
                         "taxId": payload.get("tax_id"),
                         "orderId": payload.get("order_id"),
                         "statementRef": payload.get("statement_ref"),
                         "submittedAt": ev.get("occurred_at")})
        rows.sort(key=lambda r: str(r.get("submittedAt") or ""))
        return {"rows": rows, "total": len(rows),
                "caliber_note": "只含已通过审核且无有效回执的申请；"
                                "待审与已驳回不计入缺口"}

    def reconciliation(self, customer_ids: Any) -> dict[str, Any]:
        """核对视图：每张已通过申请的状态、有效回执、核对结论与差额。"""
        ids = {str(c) for c in (customer_ids or []) if str(c).strip()}
        if not ids:
            raise TemplateError("INVALID_ARGUMENT", "核对视图须给客户集合")
        active = self._receipt_state()["active"]
        by_ref: dict[str, list[dict[str, Any]]] = {}
        for rec in active.values():
            by_ref.setdefault(rec["requestRef"], []).append(rec)
        rows: list[dict[str, Any]] = []
        counts = {"issued": 0, "unissued": 0, "mismatched": 0, "unverified": 0}
        for ref, ev in self._requests().items():
            payload = ev.get("payload") or {}
            if str(payload.get("customer_id") or "") not in ids:
                continue
            status = str(ev.get("status") or "")
            if status != "accepted":
                continue
            hits = sorted(by_ref.get(ref, []), key=lambda r: r["issuedOn"])
            verdicts = [r.get("match") or "unverified" for r in hits]
            if not hits:
                counts["unissued"] += 1
                row_verdict = "unissued"
            else:
                counts["issued"] += 1
                if "mismatched" in verdicts:
                    counts["mismatched"] += 1
                    row_verdict = "mismatched"
                elif "unverified" in verdicts:
                    counts["unverified"] += 1
                    row_verdict = "unverified"
                else:
                    row_verdict = "matched"
            rows.append({
                "requestRef": ref, "customerId": payload.get("customer_id"),
                "status": status, "verdict": row_verdict,
                "receipts": [{"invoiceNo": r["invoiceNo"],
                              "amount": r["amount"],
                              "match": r.get("match"),
                              "difference": r.get("difference")}
                             for r in hits],
                "difference": next((r.get("difference") for r in hits
                                    if r.get("match") == "mismatched"), None),
            })
        rows.sort(key=lambda r: (r["verdict"], str(r["requestRef"])))
        return {"rows": rows, "counts": counts,
                "caliber_note": "unverified 单列计数：没人给推定金额时不算相符；"
                                "unissued 才是「还没开票」"}


__all__ = ["InvoiceReceiptLane", "CHANNELS", "INVOICE_REQUEST_EVENT",
           "RECEIPT_SCHEMA"]

"""协商：询价 → 报价 → 接受（change establish-ai-native-trade-discovery 2.x）。

**为什么进编排档而不是事件层**：`event_types` 是会计事实（有 `requires_posting`、期间关账、
冲正语义）。询价与报价不产生分录，塞进去会让每条询价都要回答「是否过账」，而正确答案是
没有——把非会计事实塞进会计封闭集会污染那层的判据。

**报价为什么不做成第五个取价层**：取价层是按作用域自动匹配的（租户×渠道×商品×日），
报价是由 ref 指定的一次性承诺，没有「匹配」语义。塞进层级会造出「这条报价对哪些商品生效」
的模糊地带——而报价的全部价值就在于它明确指向。

**三条硬纪律**（spec `trade-negotiation`）：
1. 询价单**不带价格**（买方出价会让报价变成形式，也让成交价失去来源）；
2. 报价**必须有时效**，过期不可接受；
3. 协商**不碰钱**：不产生收款事实、不写资金类事件。
"""
from __future__ import annotations

import time
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from .errors import TemplateError
from .orchestration import _fmt, _ref

#: 协商记录的两种 `record`（与既有 14 种并列，读路径按 record 过滤，老数据不受影响）
RECORD_DEMAND = "trade_demand"
RECORD_QUOTE = "trade_quote"

#: 同一买方对同一店的未关闭询价单上限。不设上限 = 一个免费的通知轰炸入口。
MAX_OPEN_DEMANDS_PER_BUYER = 20


def _today() -> str:
    return date.today().isoformat()


def _parse_deadline(value: Any, what: str) -> float:
    """接受 ISO 日期（2026-12-01）或 ISO 时间戳（2026-12-01T12:00:00）。

    过期判定用**接受时刻**而非报价时刻（设计 D5 的 Risks）：报价在有效期内给出、
    在接受时已过期，就该拒。
    """
    s = str(value or "").strip()
    if not s:
        raise TemplateError("INVALID_ARGUMENT", f"{what} 必填", detail={"missing": what})
    try:
        if "T" in s:
            from datetime import datetime
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        from datetime import datetime as _dt
        return _dt.fromisoformat(s + "T23:59:59").timestamp()
    except ValueError as exc:
        raise TemplateError("INVALID_ARGUMENT",
                            f"{what} 须为 ISO 日期或日期时间：{s!r}",
                            detail={what: s}) from exc


class TradeDesk:
    """询价与报价（挂在既有编排档上，共用同一份 JSONL）。

    与 `OrderOrchestrator` 共用 `data_dir` 与 `_log`（同一个 `orchestration-<tenant>.jsonl`）：
    一次交易尝试从询价到成交是连续的事实，分开存会让「这次谈到哪一步」需要拼两个文件。
    """

    def __init__(self, tenant_id: str, data_dir, *, log=None):
        from pathlib import Path

        from .orchestration import EventLog, ORCH_SCHEMA
        self._tenant = tenant_id
        self._schema = ORCH_SCHEMA
        self._log = log or EventLog(Path(data_dir) / f"orchestration-{tenant_id}.jsonl")

    # ---- 档 ----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def _append(self, record: dict[str, Any]) -> None:
        self._log.append({"schema": self._schema, "tenant_id": self._tenant, **record})

    def _of(self, record: str) -> list[dict[str, Any]]:
        return [e for e in self._events() if e.get("record") == record]

    def _demand(self, ref: str) -> dict[str, Any]:
        hit = next((e for e in self._of(RECORD_DEMAND) if e.get("ref") == ref), None)
        if hit is None:
            raise TemplateError("NOT_FOUND", f"询价单不存在：{ref}")
        return hit

    def _quote_events(self, ref: str) -> list[dict[str, Any]]:
        return [e for e in self._of(RECORD_QUOTE) if e.get("ref") == ref]

    def _quote(self, ref: str) -> dict[str, Any]:
        """报价的**原始条款**（第一条事件）+ 累计已成交数量。"""
        evs = self._quote_events(ref)
        if not evs:
            raise TemplateError("NOT_FOUND", f"报价不存在：{ref}")
        return evs[0]

    # ---- 询价 ----

    def create_demand(self, *, lines: list[dict[str, Any]], needed_by: Any,
                      ship_to: dict[str, Any], buyer_ref: str = "",
                      requested_by: str = "agent") -> dict[str, Any]:
        """买方声明「要什么、多少、何时要、送到哪」。**不接受价格**（spec 硬要求）。"""
        if not isinstance(lines, list) or not lines:
            raise TemplateError("INVALID_ARGUMENT", "询价单 lines 须为非空数组",
                                detail={"missing": "lines"})
        extra = [k for k in ("expected_price", "price", "unit_price", "amount")
                 if k in (lines[0] if isinstance(lines[0], dict) else {})]
        if extra:
            # 买方出价会让「报价」变成形式，也让成交价失去来源——直接拒，不静默忽略
            # （静默忽略等于让调用方以为自己的出价被采纳）。
            raise TemplateError(
                "INVALID_ARGUMENT",
                "询价单不接受价格字段：价格必须由卖方在报价里给出",
                detail={"rejected_fields": extra})
        norm: list[dict[str, Any]] = []
        for ln in lines:
            if not isinstance(ln, dict):
                raise TemplateError("INVALID_ARGUMENT", "询价单行须为对象")
            code = str(ln.get("productId") or ln.get("product_code") or "").strip()
            if not code:
                raise TemplateError("INVALID_ARGUMENT", "询价单行缺 productId")
            qty = ln.get("qty")
            try:
                q = Decimal(str(qty))
            except Exception as exc:  # noqa: BLE001
                raise TemplateError("INVALID_ARGUMENT", f"数量非法：{qty!r}") from exc
            if q <= 0:
                raise TemplateError("INVALID_ARGUMENT", "询价数量须 > 0")
            norm.append({"product_code": code, "qty": str(q),
                         **({"unit": str(ln["unit"])} if ln.get("unit") else {})})
        deadline = _parse_deadline(needed_by, "needed_by")
        if not isinstance(ship_to, dict) or not str(ship_to.get("name") or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "询价单须提供收货信息 ship_to.name",
                                detail={"missing": "ship_to.name"})

        open_same_buyer = [e for e in self._of(RECORD_DEMAND)
                           if e.get("status") == "open"
                           and str(e.get("buyer_ref") or "") == str(buyer_ref or "")]
        if len(open_same_buyer) >= MAX_OPEN_DEMANDS_PER_BUYER:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"同一买方未关闭询价单已达上限 {MAX_OPEN_DEMANDS_PER_BUYER}；"
                "请先关闭或成交已有询价单",
                detail={"open_demands": len(open_same_buyer)})

        ref = _ref("TD", f"{self._tenant}|{uuid.uuid4().hex}")
        self._append({"record": RECORD_DEMAND, "ref": ref, "event": "created",
                      "status": "open", "buyer_ref": buyer_ref,
                      "requested_by": requested_by,
                      "payload": {"lines": norm, "needed_by": str(needed_by),
                                  "needed_by_ts": deadline,
                                  "ship_to": dict(ship_to)},
                      "note": "询价单：只声明需求，不含任何价格"})
        return {"demand_ref": ref, "status": "open", "lines": len(norm),
                "needed_by": str(needed_by), "amount": None}

    # ---- 报价 ----

    def create_quote(self, *, demand_ref: str, unit_price: Any, currency: str = "CNY",
                     valid_until: Any, lead_time_days: Any, moq: Any, qty_offered: Any,
                     product_code: str = "", quoted_by: str = "merchant"
                     ) -> dict[str, Any]:
        """卖方对某条询价单给出报价。**必须有时效**，MUST 有总量承诺。"""
        demand = self._demand(demand_ref)
        if demand.get("status") != "open":
            raise TemplateError("WORKFLOW_STATE",
                                f"询价单当前态不接受报价（{demand.get('status')}）：{demand_ref}")
        deadline = _parse_deadline(valid_until, "valid_until")
        if deadline <= time.time():
            raise TemplateError("INVALID_ARGUMENT",
                                f"报价有效期必须在未来：{valid_until!r}",
                                detail={"valid_until": str(valid_until)})
        try:
            price = Decimal(str(unit_price))
        except Exception as exc:  # noqa: BLE001
            raise TemplateError("INVALID_ARGUMENT", f"单价非法：{unit_price!r}") from exc
        if price <= 0:
            raise TemplateError("INVALID_ARGUMENT", "报价单价须 > 0")
        try:
            moq_d = Decimal(str(moq)) if moq not in (None, "") else Decimal(1)
            offered = Decimal(str(qty_offered))
            lead = int(lead_time_days)
        except Exception as exc:  # noqa: BLE001
            raise TemplateError("INVALID_ARGUMENT",
                                "moq / qty_offered / lead_time_days 须为数字") from exc
        if moq_d < 0:
            raise TemplateError("INVALID_ARGUMENT", "moq 不得为负")
        if offered <= 0:
            raise TemplateError("INVALID_ARGUMENT", "qty_offered（报价总量承诺）须 > 0",
                                detail={"missing": "qty_offered"})
        if offered < moq_d:
            raise TemplateError("INVALID_ARGUMENT",
                                f"报价总量 {offered} 小于最小起订量 {moq_d}：这条报价永远无法成交",
                                detail={"qty_offered": str(offered), "moq": str(moq_d)})
        if lead < 0:
            raise TemplateError("INVALID_ARGUMENT", "交期天数不得为负")

        ref = _ref("TQ", f"{self._tenant}|{demand_ref}|{uuid.uuid4().hex}")
        self._append({"record": RECORD_QUOTE, "ref": ref, "event": "quoted",
                      "status": "open", "demand_ref": demand_ref,
                      "requested_by": quoted_by,
                      "payload": {"product_code": str(product_code or ""),
                                  "unit_price": _fmt(price), "currency": str(currency),
                                  "valid_until": str(valid_until),
                                  "valid_until_ts": deadline,
                                  "lead_time_days": lead,
                                  "moq": _fmt(moq_d),
                                  "qty_offered": _fmt(offered),
                                  "qty_committed": "0"},
                      "note": "报价：有时效的承诺，含总量上限与最小起订量"})
        return {"quote_ref": ref, "demand_ref": demand_ref,
                "unit_price": _fmt(price), "currency": str(currency),
                "valid_until": str(valid_until), "moq": _fmt(moq_d),
                "qty_offered": _fmt(offered), "lead_time_days": lead}

    def _committed(self, quote_ref: str) -> Decimal:
        """累计已成交数量 = **最后一条** accepted 事件的 `qty_committed`。

        注意：该字段是**累计值**不是增量。对多条 accepted 事件求和会把 5+10 算成 15
        （2026-11-25 实测踩到），所以取最后一条而不是 sum。
        """
        acc = [e for e in self._quote_events(quote_ref) if e.get("event") == "accepted"]
        if not acc:
            return Decimal(0)
        return Decimal(str(acc[-1]["payload"].get("qty_committed") or 0))

    def quote_remaining(self, quote_ref: str) -> Decimal:
        """该报价还剩多少可成交 = 总量承诺 − 累计已成交。"""
        first = self._quote(quote_ref)
        offered = Decimal(str(first["payload"]["qty_offered"]))
        return offered - self._committed(quote_ref)

    # ---- 接受 ----

    def check_quote_acceptable(self, quote_ref: str, qty: Any) -> dict[str, Any]:
        """接受前的全部判定（纯读，不改档）——供 API 层与接受路径共用。"""
        first = self._quote(quote_ref)
        payload = first.get("payload") or {}
        if first.get("status") == "closed":
            raise TemplateError("WORKFLOW_STATE", f"报价已关闭：{quote_ref}")
        deadline = float(payload.get("valid_until_ts") or 0)
        now = time.time()
        if not deadline or deadline <= now:
            raise TemplateError(
                "WORKFLOW_STATE",
                f"报价已过期（有效期至 {payload.get('valid_until')}），不可接受",
                detail={"quote_ref": quote_ref,
                        "valid_until": payload.get("valid_until")})
        try:
            q = Decimal(str(qty))
        except Exception as exc:  # noqa: BLE001
            raise TemplateError("INVALID_ARGUMENT", f"成交数量非法：{qty!r}") from exc
        if q <= 0:
            raise TemplateError("INVALID_ARGUMENT", "成交数量须 > 0")
        moq = Decimal(str(payload.get("moq") or 0))
        if q < moq:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"成交数量 {q} 低于该报价的最小起订量 {moq}",
                detail={"quote_ref": quote_ref, "moq": _fmt(moq), "qty": str(q)})
        remaining = self.quote_remaining(quote_ref)
        if q > remaining:
            raise TemplateError(
                "WORKFLOW_STATE",
                f"该报价剩余可成交数量 {_fmt(remaining)} 不足 {q}（总量承诺已部分用尽）",
                detail={"quote_ref": quote_ref, "remaining": _fmt(remaining),
                        "qty": str(q)})
        unit = Decimal(str(payload["unit_price"]))
        return {"quote_ref": quote_ref, "demand_ref": first.get("demand_ref"),
                "product_code": str(payload.get("product_code") or ""),
                "unit_price": _fmt(unit), "qty": str(q),
                "amount": _fmt((unit * q).quantize(Decimal("0.000001"))),
                "currency": str(payload.get("currency") or "CNY"),
                "lead_time_days": payload.get("lead_time_days"),
                "remaining_before": _fmt(remaining)}

    def commit_acceptance(self, *, quote_ref: str, qty: Any, order_ref: str,
                          accepted_by: str = "agent") -> dict[str, Any]:
        """接受已成立后**记账**：追加一条 `accepted` 事件（累计 qty_committed）。

        为什么追加而不是改写报价记录：编排档是只追加的（`_append` 语义），改字段会让
        同一 ref 的两条记录互相矛盾。累加式让「一条报价被分两次用掉」天然成立，
        也让事后审计能看见每一次接受。
        """
        first = self._quote(quote_ref)
        payload = first.get("payload") or {}
        total = self._committed(quote_ref) + Decimal(str(qty))
        offered = Decimal(str(payload.get("qty_offered") or 0))
        self._append({"record": RECORD_QUOTE, "ref": quote_ref, "event": "accepted",
                      "status": "closed" if total >= offered else "open",
                      "demand_ref": first.get("demand_ref"),
                      "requested_by": accepted_by,
                      "payload": {"qty_committed": _fmt(total),
                                  "qty_offered": _fmt(offered),
                                  "order_ref": order_ref},
                      "note": f"接受报价 {quote_ref} → 订单 {order_ref}"})
        return {"quote_ref": quote_ref, "qty_committed": _fmt(total),
                "qty_offered": _fmt(offered), "order_ref": order_ref}

    def accept_quote(self, *, quote_ref: str, qty: Any,
                     orchestrator, day: str, channel: str,
                     ship_to: dict[str, Any], buyer_ref: str = "",
                     requested_by: str = "agent",
                     grant_jti: str | None = None,
                     grant_budget: Any | None = None,
                     budget_ledger: Any | None = None) -> dict[str, Any]:
        """接受报价 → 生成订单（成交价取自报价，受同一道预算闸约束）。

        **顺序很关键**：先判定（含过期/起订量/剩余量），再下单，最后记账。反过来做的
        最坏形态是「订单已落库但报价没扣减」，同一条报价可以被无限次接受。
        """
        chk = self.check_quote_acceptable(quote_ref, qty)
        code = chk["product_code"]
        if not code:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"报价 {quote_ref} 未指向具体商品，无法成交（请卖方在报价里声明 product_code）",
                detail={"quote_ref": quote_ref})
        out = orchestrator.accept_prepaid_order(
            [{"product_code": code, "qty": str(qty)}], day, channel=channel,
            ship_to=ship_to, buyer_ref=buyer_ref, requested_by=requested_by,
            grant_jti=grant_jti, grant_budget=grant_budget,
            budget_ledger=budget_ledger,
            # 报价优先于零售层：由 ref 指定，不做作用域匹配（设计 D5）。
            quoted={"quote_ref": quote_ref, "unit_price": chk["unit_price"],
                    "currency": chk["currency"]})
        self.commit_acceptance(quote_ref=quote_ref, qty=qty,
                               order_ref=out["order_ref"], accepted_by=requested_by)
        return {**out, "quote_ref": quote_ref, "quoted_unit_price": chk["unit_price"]}

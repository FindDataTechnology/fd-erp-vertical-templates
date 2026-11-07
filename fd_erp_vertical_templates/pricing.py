"""取价引擎（add-supply-chain-vertical 任务 4.3）。

三层规则：客户专属价（customer_price）/ 活动价（promo）/ 优惠券（coupon）。
纪律：
- 规则带生效期（valid_from/valid_to）与出处（rate_source）——无出处规则
  拒绝参与取价（配置面拒绝无出处规则；取价面再防御性跳过并留痕）；
- 规则冲突（同层同维度作用域 + 生效期重叠）检测拒绝配置；
- 规则配置走确认车道（pending → confirm），append-only 留痕；
- 取价可溯源：返回命中规则链（provenance，逐规则带 rate_source）。

取价语义（可解释、确定性）：
1. 基础价 = 商品主档 base_price；
2. customer_price 层命中 → 覆盖为基础价之上的客户专属价（绝对价）；
3. promo 层命中 → 覆盖为活动价（绝对价）；
4. coupon 层命中 → 在现价上打折（discount_pct 百分比，取最优一张）。
每层内取「更具体作用域优先」（客户/商品精确 > 通配），同精确度取
valid_from 更晚者，再同则取 rule_id 序（确定性）。
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog

PRICING_SCHEMA = "erp-pricing-rules/v1"
RULE_LAYERS = ("customer_price", "promo", "coupon")
_SCOPE_FIELDS = ("product_code", "customer_id")


def _day(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


class PricingRuleBook:
    """取价规则簿：配置（确认车道）+ 冲突检测 + resolve_price。"""

    def __init__(self, tenant_id: str, data_dir: str | Path,
                 master: Any | None = None):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"pricing-rules-{tenant_id}.jsonl")
        self._master = master  # MasterData：基础价出处（商品主档 base_price）

    # ---- 规则配置（确认车道）----

    def _events(self) -> list[dict[str, Any]]:
        return self._log.events()

    def add_rule(self, rule: dict[str, Any],
                 *, requested_by: str = "agent") -> dict[str, Any]:
        """新增规则提案（pending）。无出处 / 生效期非法 / 维度重叠冲突即拒绝。"""
        rule = dict(rule)
        self._validate_rule(rule)
        conflict = self._find_conflict(rule)
        if conflict is not None:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"规则冲突：同层同维度作用域生效期重叠（{conflict['rule_id']} vs "
                f"{rule.get('rule_id') or 'new'}）——拒绝配置",
                detail={"existing": conflict["rule_id"],
                        "layer": rule.get("layer"),
                        "scope": rule.get("scope")})
        record = {
            "schema": PRICING_SCHEMA, "event": "add_rule",
            "request_id": uuid.uuid4().hex,
            "rule_id": str(rule.get("rule_id") or uuid.uuid4().hex),
            "tenant_id": self._tenant, "rule": rule,
            "status": "pending", "requested_by": requested_by,
        }
        self._log.append(record)
        return {"request_id": record["request_id"], "rule_id": record["rule_id"],
                "status": "pending",
                "note": "取价规则经确认车道：确认后参与取价，拒绝留痕"}

    def confirm_rule(self, request_id: str, *, decided_by: str = "owner") -> dict[str, Any]:
        target = self._log.set_status(request_id, "confirmed", decided_by=decided_by)
        if target is None:
            raise TemplateError("NOT_FOUND", f"待确认规则不存在或已裁决：{request_id}")
        return {"request_id": request_id, "rule_id": target["rule_id"],
                "decision": "confirmed", "decided_by": decided_by}

    def reject_rule(self, request_id: str, reason: str,
                    *, decided_by: str = "owner") -> dict[str, Any]:
        target = self._log.set_status(request_id, "rejected",
                                      decided_by=decided_by, reason=reason)
        if target is None:
            raise TemplateError("NOT_FOUND", f"待确认规则不存在或已裁决：{request_id}")
        return {"request_id": request_id, "rule_id": target["rule_id"],
                "decision": "rejected", "reason": reason,
                "note": "拒绝留痕：该规则不参与取价"}

    def _validate_rule(self, rule: dict[str, Any]) -> None:
        layer = str(rule.get("layer") or "")
        if layer not in RULE_LAYERS:
            raise TemplateError("INVALID_ARGUMENT",
                                f"规则层须为 {RULE_LAYERS}", detail={"layer": layer})
        # 出处纪律：无出处规则拒绝配置（rate_source 必填）
        if not str(rule.get("rate_source") or "").strip():
            raise TemplateError("INVALID_ARGUMENT",
                                "取价规则缺出处 rate_source（无出处规则拒绝配置）")
        scope = rule.get("scope")
        if not isinstance(scope, dict):
            raise TemplateError("INVALID_ARGUMENT", "规则缺作用域 scope")
        for f in _SCOPE_FIELDS:
            if not str(scope.get(f) or "*").strip():
                raise TemplateError("INVALID_ARGUMENT", f"scope.{f} 非法")
        vf, vt = str(rule.get("valid_from") or ""), str(rule.get("valid_to") or "")
        try:
            if _day(vf) > _day(vt):
                raise TemplateError("INVALID_ARGUMENT",
                                    f"生效期非法：valid_from {vf} > valid_to {vt}")
        except ValueError as exc:
            raise TemplateError("INVALID_ARGUMENT",
                                f"生效期日期非法：{vf!r}/{vt!r}（YYYY-MM-DD）") from exc
        if layer in ("customer_price", "promo"):
            try:
                price = Decimal(str(rule.get("price")))
            except (InvalidOperation, TypeError) as exc:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"{layer} 规则须带正数 price") from exc
            if price <= 0:
                raise TemplateError("INVALID_ARGUMENT", f"{layer} 规则 price 须 > 0")
        else:  # coupon
            try:
                pct = Decimal(str(rule.get("discount_pct")))
            except (InvalidOperation, TypeError) as exc:
                raise TemplateError("INVALID_ARGUMENT",
                                    "coupon 规则须带 discount_pct（0~100）") from exc
            if not (Decimal(0) < pct < Decimal(100)):
                raise TemplateError("INVALID_ARGUMENT",
                                    "coupon discount_pct 须在 (0, 100) 开区间")

    def _find_conflict(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        """同层 + 同维度作用域（product/customer 全等）+ 生效期重叠 = 冲突。"""
        for ev in self._events():
            if ev.get("event") != "add_rule" or ev.get("status") == "rejected":
                continue
            existing = ev.get("rule") or {}
            if existing.get("layer") != candidate.get("layer"):
                continue
            if dict(existing.get("scope") or {}) != dict(candidate.get("scope") or {}):
                continue
            if _day(existing["valid_to"]) < _day(candidate["valid_from"]) \
                    or _day(existing["valid_from"]) > _day(candidate["valid_to"]):
                continue
            return {"rule_id": ev.get("rule_id") or existing.get("rule_id") or "",
                    **existing}
        return None

    # ---- 取价 ----

    def confirmed_rules(self, layer: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ev in self._events():
            if ev.get("event") == "add_rule" and ev.get("status") == "confirmed":
                r = ev["rule"]
                if layer is None or r.get("layer") == layer:
                    out.append({**r, "rule_id": ev["rule_id"]})
        return out

    def resolve_price(self, tenant: str, customer: str, product: str, day: str,
                      qty: str | int | float = 1) -> dict[str, Any]:
        """下单自动取价：返回价格 + 命中规则链（provenance）。

        无出处规则不参与取价（跳过并留痕 unsourced_skipped）；
        无任何规则命中时回落商品主档基础价。
        """
        if tenant != self._tenant:
            raise TemplateError("INVALID_ARGUMENT", "租户不匹配（逐租户取价）")
        base_price = self._base_price(product)
        price = Decimal(str(base_price))
        chain: list[dict[str, Any]] = []
        unsourced: list[dict[str, Any]] = []
        d = _day(day)
        for layer in ("customer_price", "promo", "coupon"):
            cands = [r for r in self.confirmed_rules(layer)
                     if str(r.get("scope", {}).get("product_code") or "*") in (product, "*")
                     and str(r.get("scope", {}).get("customer_id") or "*") in (customer, "*")
                     and _day(r["valid_from"]) <= d <= _day(r["valid_to"])]
            picked, skipped = self._pick(layer, cands)
            unsourced.extend(skipped)
            if picked is None:
                continue
            if layer == "coupon":
                pct = Decimal(str(picked["discount_pct"]))
                new_price = (price * (Decimal(100) - pct) / Decimal(100))
                effect = f"-{pct}%（{price} → {new_price}）"
            else:
                new_price = Decimal(str(picked["price"]))
                effect = f"覆盖为 {new_price}（原 {price}）"
            price = new_price
            chain.append({"layer": layer, "rule_id": picked["rule_id"],
                          "rate_source": picked["rate_source"],
                          "valid_from": picked["valid_from"],
                          "valid_to": picked["valid_to"], "effect": effect})
        try:
            q = Decimal(str(qty))
        except InvalidOperation as exc:
            raise TemplateError("INVALID_ARGUMENT", f"数量非法：{qty!r}") from exc
        line_total = (price * q).quantize(Decimal("0.000001"))
        return {
            "tenant": tenant, "customer": customer, "product": product, "day": day,
            "qty": str(qty), "base_price": str(base_price),
            "unit_price": format(price.quantize(Decimal("0.000001")).normalize(), "f"),
            "line_total": format(line_total.normalize(), "f"),
            "rule_chain": chain, "unsourced_skipped": unsourced,
            "caliber_note": "取价可溯源：逐规则带 rate_source；"
                            "无出处规则不参与取价",
        }

    def _pick(self, layer: str, cands: list[dict[str, Any]]
              ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """层内择一：更具体作用域优先 > valid_from 更晚 > rule_id 序。"""
        picked: dict[str, Any] | None = None
        skipped: list[dict[str, Any]] = []
        for r in sorted(cands, key=lambda x: x["rule_id"]):
            if not str(r.get("rate_source") or "").strip():
                skipped.append({"rule_id": r["rule_id"], "layer": layer,
                                "note": "无出处规则拒绝参与取价"})
                continue
            if picked is None or self._specificity(r) > self._specificity(picked) \
                    or (self._specificity(r) == self._specificity(picked)
                        and _day(r["valid_from"]) > _day(picked["valid_from"])):
                picked = r
        return picked, skipped

    @staticmethod
    def _specificity(rule: dict[str, Any]) -> int:
        scope = rule.get("scope") or {}
        return sum(1 for f in _SCOPE_FIELDS
                   if str(scope.get(f) or "*") != "*")

    def _base_price(self, product: str) -> str:
        if self._master is not None:
            snap = self._master.find_by_code("product", product)
            if snap is not None and snap["payload"].get("base_price") is not None:
                return str(snap["payload"]["base_price"])
        raise TemplateError("NOT_FOUND",
                            f"商品基础价不可得（主档缺 base_price 或商品未建档）：{product}")

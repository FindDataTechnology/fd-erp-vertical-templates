"""跨境电商对账驱动（erp-vertical-crossborder 任务 2.3 + 2.4）。

三方流：订单（Shopify orders）↔ 收款（Shopify payouts）↔ 库存（inventory）。
逐店出账：store 维度隔离，互不混账。平台侧零写入（连接器只读）。

多币种（任务 2.4）：
- 原币对账为准；折算仅作呈现，汇率出处可溯（ecb-exr 观测流）；
  无出处的汇率不参与折算（如实标注 fx_unsourced，不静默换算）。
- 折算核对落账时由内核二次把关（fx_facts 登记 + FX_UNSOURCED 拒绝）。

平台费用维度：payouts / 佣金 / 支付手续费独立列示，费用差异按平台费率
配置解释（费率带出处）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from ..errors import TemplateError
from ..mapping import UNIVERSAL_ACCOUNT_CODES
from .three_way import align_pair, align_status

#: 回写白名单科目（核对回写禁写资金类分录：1000 现金 / 3000-3100 权益不在列）
WRITEBACK_ALLOWED_ACCOUNTS = frozenset(
    {"1100", "1200", "1300", "2000", "4000", "5000", "5100", "5200"})

FxProvider = Callable[[str, str], tuple[str, str]]  # currency, as_of -> (rate, source)


def _diff_amount(diff: dict[str, Any]) -> str | None:
    """差异金额摘要。amount_mismatch 且双侧都有金额 → 取差额（核对更正是差额）；
    其余取左优先、右兜底、raw 再兜底。"""
    if diff.get("kind") == "amount_mismatch" and diff.get("left_amount") \
            and diff.get("right_amount"):
        delta = abs(Decimal(str(diff["left_amount"]))
                    - Decimal(str(diff["right_amount"])))
        return str(delta)
    for key in ("left_amount", "right_amount"):
        if diff.get(key):
            return str(diff[key])
    raw = diff.get("left_raw") or diff.get("right_raw") or {}
    return raw.get("amount")


def _diff_currency(diff: dict[str, Any], *, default: str) -> str:
    for key in ("left_currency", "right_currency"):
        if diff.get(key):
            return str(diff[key])
    raw = diff.get("left_raw") or diff.get("right_raw") or {}
    return str(raw.get("currency") or default)


class EcbExrFxProvider:
    """ecb-exr 形状观测流出处的汇率供给（测试注入 / 生产接观测流）。

    observations: [{"base":"CNY","quote":"USD","rate":"7.2","as_of":"2026-11-06",
                    "source":"ecb-exr"}, ...]
    """

    def __init__(self, observations: list[dict[str, Any]]):
        self._by_pair: dict[tuple[str, str], tuple[str, str]] = {}
        for o in observations:
            pair = (str(o.get("quote")), str(o.get("as_of")))
            self._by_pair[pair] = (str(o.get("rate")), str(o.get("source") or "ecb-exr"))

    def __call__(self, currency: str, as_of: str) -> tuple[str, str]:
        hit = self._by_pair.get((currency, as_of))
        if hit is None:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"无出处汇率不参与折算：{currency} {as_of}（ecb-exr 无该日观测）",
                detail={"currency": currency, "as_of": as_of, "reason": "fx_unsourced"})
        return hit


def _convert(amount: str, currency: str, as_of: str, base_currency: str,
             fx: FxProvider | None) -> dict[str, Any]:
    """原币 → 本位币呈现折算。无出处汇率：如实标注，不折算。"""
    if currency == base_currency:
        return {"amount_base": amount, "fx_rate": None, "fx_as_of": None,
                "fx_source": None, "fx_unsourced": False}
    if fx is None:
        return {"amount_base": None, "fx_rate": None, "fx_as_of": as_of,
                "fx_source": None, "fx_unsourced": True,
                "note": "未提供汇率出处（fx provider），只出原币口径"}
    try:
        rate, source = fx(currency, as_of)
    except TemplateError as exc:
        return {"amount_base": None, "fx_rate": None, "fx_as_of": as_of,
                "fx_source": None, "fx_unsourced": True,
                "note": exc.message}
    converted = (Decimal(amount) * Decimal(rate)).quantize(Decimal("0.000001"))
    return {"amount_base": str(converted), "fx_rate": rate, "fx_as_of": as_of,
            "fx_source": source, "fx_unsourced": False}


class CrossborderRecon:
    """逐店三方对账。store 级实例（互不混账靠单店实例 + store 字段落款）。

    `matrix` + `platform` 可选：给了就按矩阵的可对齐腿数降级（三方 → 两方 → 单方），
    并把不可得腿写进差异清单的 `capability_missing`；不给则维持旧行为（三方全跑），
    用于既有测试与未注入矩阵的调用面。
    """

    def __init__(self, store: str, *, base_currency: str = "CNY",
                 fx: FxProvider | None = None,
                 platform_fee_schedule: dict[str, str] | None = None,
                 matrix: Any | None = None, platform: str | None = None):
        self._store = store
        self._base = base_currency
        self._fx = fx
        self._fees = platform_fee_schedule or {}
        self._matrix = matrix
        self._platform = platform

    @property
    def store(self) -> str:
        return self._store

    # ---- 矩阵面 ----

    def _missing(self, flow: str) -> dict[str, Any] | None:
        """该腿的不可得归因（无矩阵时恒 None = 不做降级判定）。"""
        if self._matrix is None or not self._platform:
            return None
        return self._matrix.capability_missing(self._platform, flow)

    def capability_missing(self) -> list[dict[str, Any]]:
        if self._matrix is None or not self._platform:
            return []
        return self._matrix.missing_legs(self._platform)

    def recon_shape(self) -> int | None:
        if self._matrix is None or not self._platform:
            return None
        return self._matrix.recon_shape(self._platform)

    def _unavailable(self, flow: str, pair: str) -> dict[str, Any] | None:
        missing = self._missing(flow)
        if missing is None:
            return None
        return {
            "store": self._store,
            "pair": pair,
            "diffs": [],
            "matched": 0,
            "capability_missing": missing,
            "note": (f"该腿不可得（{missing['status']}）：{missing['reason']}；"
                     "不产出零差异结论"),
        }

    # ---- 订单 ↔ 收款 ----

    def reconcile_orders_payments(self, orders: list[dict[str, Any]],
                                  payments: list[dict[str, Any]],
                                  *, amount_tolerance: str = "0") -> dict[str, Any]:
        """订单流 ↔ 收款流逐笔对齐（关联键 = raw.order_id / raw.link）。

        结算腿不可得时**不产出任何对齐结果**：拿到空的 payments 就报「零差异」
        是把「没核对」说成「核对无误」。
        """
        blocked = self._unavailable("settlements", "orders-payments")
        if blocked is not None:
            return blocked
        result = align_pair(orders, payments, left_side="orders",
                            right_side="payments",
                            amount_tolerance=amount_tolerance)
        result["store"] = self._store
        result["pair"] = "orders-payments"
        # 差异折算呈现（原币对账为准，折算带出处；无出处如实 fx_unsourced）
        for d in result["diffs"]:
            for prefix in ("left", "right"):
                amount = d.get(f"{prefix}_amount")
                currency = d.get(f"{prefix}_currency")
                day = d.get(f"{prefix}_day")
                if amount and currency and day:
                    d[f"{prefix}_conversion"] = _convert(
                        str(amount), str(currency), str(day), self._base, self._fx)
        return result

    # ---- 订单 ↔ 库存（状态腿）----

    def reconcile_orders_inventory(self, orders: list[dict[str, Any]],
                                   inventory_moves: list[dict[str, Any]]
                                   ) -> dict[str, Any]:
        """订单 ↔ 库存状态对齐：在途/未扣减 → needs_explanation=logistics。

        库存腿不可得时不产出对齐结果，只报 `capability_missing`——「库存零差异」
        在没读过库存的情况下是假绿。
        """
        blocked = self._unavailable("inventory", "orders-inventory")
        if blocked is not None:
            return blocked
        result = align_status(orders, inventory_moves, left_side="orders",
                              right_side="inventory")
        result["store"] = self._store
        result["pair"] = "orders-inventory"
        return result

    # ---- 平台费用维度 ----

    def fee_report(self, payments: list[dict[str, Any]]) -> dict[str, Any]:
        """payouts / 佣金 / 支付手续费独立维度；差异按费率配置解释。

        平台费率配置（platform_fee_schedule）= {dimension: "rate"}，
        带出处（rate_source）方可参与解释；无费率出处 → 如实 unexplained。

        结算腿不可得时：不产出行、不产出 totals、不给 explained——费用对账
        无从谈起，只能报 `capability_missing`。
        """
        blocked = self._missing("settlements")
        if blocked is not None:
            return {"store": self._store, "rows": [], "totals": None,
                    "fee_explanations": [],
                    "capability_missing": blocked,
                    "note": (f"结算面不可得（{blocked['status']}）：{blocked['reason']}；"
                             "不产出结算额与费用差异")}
        rows: list[dict[str, Any]] = []
        totals: dict[str, Decimal] = {"payouts": Decimal(0), "commission": Decimal(0),
                                      "payment_fee": Decimal(0)}
        for p in payments:
            fees = (p.get("raw") or {}).get("fees") or {}
            as_of = str(p.get("occurred_at") or "")[:10]
            conv = _convert(str(p.get("amount")), str(p.get("currency")),
                            as_of, self._base, self._fx)
            rows.append({
                "payout_ref": p.get("source_ref"), "day": as_of,
                "payout_amount": str(p.get("amount")),
                "commission": str(fees.get("commission") or "0"),
                "payment_fee": str(fees.get("payment_fee") or "0"),
                "conversion": conv,
            })
            totals["payouts"] += Decimal(str(p.get("amount") or "0"))
            totals["commission"] += Decimal(str(fees.get("commission") or "0"))
            totals["payment_fee"] += Decimal(str(fees.get("payment_fee") or "0"))
        explanations: list[dict[str, Any]] = []
        for dim, rate in self._fees.items():
            if "/" not in rate:
                explanations.append({"dimension": dim, "rate": rate,
                                     "explained": False,
                                     "note": "费率配置缺出处（rate=分子/分母/出处），"
                                             "费用差异 unexplained"})
                continue
            numerator, denominator, source = rate.split("/", 2)
            explanations.append({
                "dimension": dim, "rate": f"{numerator}/{denominator}",
                "rate_source": source, "explained": True,
                "note": "费率差异解释带出处（平台费率配置）",
            })
        return {"store": self._store, "rows": rows,
                "totals": {k: str(v) for k, v in totals.items()},
                "fee_explanations": explanations}

    # ---- 一次对账（按矩阵腿数编排）----

    def reconcile(self, *, orders: list[dict[str, Any]],
                  payments: list[dict[str, Any]] | None = None,
                  inventory: list[dict[str, Any]] | None = None,
                  amount_tolerance: str = "0") -> dict[str, Any]:
        """按可得腿数跑对账并汇总。

        返回 `shape`（3/2/1）与 `capability_missing`（逐腿归因）；不可得腿对应的
        pair 不产出差异清单，只产出归因——差异数为 0 与「没核对」必须可区分。
        """
        shape = self.recon_shape()
        results: dict[str, dict[str, Any]] = {}

        settlements_missing = self._missing("settlements")
        if settlements_missing is None and payments is not None:
            results["orders-payments"] = self.reconcile_orders_payments(
                orders, payments, amount_tolerance=amount_tolerance)
        else:
            blocked = self._unavailable("settlements", "orders-payments")
            results["orders-payments"] = blocked if blocked is not None else {
                "store": self._store, "pair": "orders-payments",
                "skipped": True, "note": "调用方未提供收款流数据"}

        inventory_missing = self._missing("inventory")
        if inventory_missing is None and inventory is not None:
            results["orders-inventory"] = self.reconcile_orders_inventory(
                orders, inventory)
        else:
            blocked = self._unavailable("inventory", "orders-inventory")
            results["orders-inventory"] = blocked if blocked is not None else {
                "store": self._store, "pair": "orders-inventory",
                "skipped": True, "note": "调用方未提供库存流数据"}

        diffs = [d for r in results.values() for d in (r.get("diffs") or [])]
        return {
            "store": self._store,
            "platform": self._platform,
            "shape": shape,
            "legs": {k: ("available" if not r.get("capability_missing") else
                         r["capability_missing"]["status"])
                     for k, r in results.items()},
            "capability_missing": self.capability_missing(),
            "pairs": results,
            "diff_count": len(diffs),
            "diffs": diffs,
        }

    # ---- 回写差异核对结论（白名单车道在 writeback.py；此处只出建议分录面）--

    def propose_correction_lines(self, diff: dict[str, Any]) -> list[dict[str, Any]]:
        """差异 → 核对回写建议分录（只引用回写白名单科目；只读不提交）。

        - missing_in_right（右缺：收款侧漏单）→ 借 1300/1100 贷 4000（应收/在途挂起）
        - extra_in_right（右多：收款侧多出）→ 反向（借 4000 贷 1300/1100 冲挂）
        - amount_mismatch/status_mismatch → 按差额更正建议，由拍板席确认
        资金类科目（1000/2100/3000/3100）不出现 —— 机器车道禁写资金类分录。
        """
        amount = _diff_amount(diff)
        currency = _diff_currency(diff, default=self._base)
        if not amount or Decimal(amount) <= 0:
            raise TemplateError("INVALID_ARGUMENT",
                                "差异缺金额或金额非正，无法出核对建议分录",
                                detail={"diff_kind": diff.get("kind")})
        kind = diff.get("kind")
        if kind == "missing_in_right":
            lines = [{"account_code": "1300", "direction": "debit", "amount": amount,
                      "currency": currency},
                     {"account_code": "4000", "direction": "credit", "amount": amount,
                      "currency": currency}]
        elif kind == "extra_in_right":
            lines = [{"account_code": "4000", "direction": "debit", "amount": amount,
                      "currency": currency},
                     {"account_code": "1300", "direction": "credit", "amount": amount,
                      "currency": currency}]
        else:  # amount_mismatch / status_mismatch → 按差额（更正建议）
            lines = [{"account_code": "1300", "direction": "debit", "amount": amount,
                      "currency": currency},
                     {"account_code": "4000", "direction": "credit", "amount": amount,
                      "currency": currency}]
        for line in lines:
            if line["account_code"] not in WRITEBACK_ALLOWED_ACCOUNTS:
                raise TemplateError(
                    "BOUNDARY_VIOLATION",
                    f"回写科目 {line['account_code']} 不在白名单（资金类分录禁写）",
                    detail={"account_code": line["account_code"]})
        if "1000" in UNIVERSAL_ACCOUNT_CODES and any(
                line["account_code"] == "1000" for line in lines):
            raise TemplateError("BOUNDARY_VIOLATION", "资金类科目 1000 禁入机器回写")
        return lines

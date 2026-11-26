"""店面在线收款（change establish-ai-native-ecommerce-stack 任务 0.13）。

商家自有 PayPal（`paypal.create_order` / `paypal.capture_order`）作为零售预付单
的在线收款路径：钱进**商家**在连接器里授权的 PayPal 账户（BYOK），平台不经手
资金、不代收——与 `payments_received` 事实「只记事实、不落分录」同一口径。

纪律（沿用 payments 车道既有 gate 口径）：
- 出网唯一口 = `RuntimeClient`（`scripts/payment_direct_call_gate.py` 守模板层
  零直连；PayPal API 主机已入该门的支付渠道主机名清单）；
- 运行时未配置 / 连接未授权 = `NOT_WIRED` 如实失败，**不回落任何自建直连**；
- live 面以实测为准（当前无真实商户号，如实为零——未实测不宣称）。
"""
from __future__ import annotations

from typing import Any

from ..errors import TemplateError
from ..runtime_client import ActionRef, RuntimeClient


class PayPalCheckout:
    """商家自有 PayPal 在线收款：create（出批准链接）→ 买家批准 → capture。

    凭据（client id/secret、沙箱或 live 环境）由连接器运行时侧持有
    （BYOK，模板层不读）；`connection` 是商家授权连接的别名。
    """

    provider = "paypal"

    def __init__(self, client: RuntimeClient | None = None,
                 connection: str | None = None):
        self._client = client or RuntimeClient()
        self._connection = connection or "paypal-default"

    # ---- 车道动作 ----

    def create_order(self, *, order_ref: str, amount: str,
                     currency: str) -> dict[str, Any]:
        """为一张预付单创建 PayPal 订单（intent=CAPTURE：批准即捕获）。

        金额/币种由调用方取自订单事实传入——买家侧不能自报价。
        返回 `paypal_order_id` 与 `approve_url`（买家批准页）。
        """
        out = self._call("create_order", {
            "intent": "CAPTURE",
            "purchaseUnits": [{"referenceId": order_ref,
                               "amount": {"currencyCode": currency,
                                          "value": str(amount)}}],
        })
        order = _first_record(out, "paypal.create_order")
        return {"paypal_order_id": str(order.get("id") or ""),
                "status": str(order.get("status") or ""),
                "approve_url": _approve_url(order),
                "raw": order}

    def capture_order(self, *, paypal_order_id: str) -> dict[str, Any]:
        """捕获已批准的 PayPal 订单，返回捕获结果（completed + capture id）。"""
        out = self._call("capture_order", {"orderId": str(paypal_order_id)})
        order = _first_record(out, "paypal.capture_order")
        capture = _capture_detail(order)
        status = str(order.get("status") or "")
        return {"paypal_order_id": str(order.get("id") or paypal_order_id),
                "status": status,
                "completed": status == "COMPLETED",
                "capture_id": capture.get("id"),
                "amount": capture.get("amount"),
                "currency": capture.get("currency"),
                "raw": order}

    def describe(self) -> dict[str, Any]:
        return {"adapter": "paypal-checkout", "provider": self.provider,
                "transport": "connector-runtime",
                "actions": ["create_order", "capture_order"],
                "writes_external": True,
                "credential": "BYOK（运行时侧持有，模板层不读）",
                "live_declared": False,
                "note": "live 面以实测为准（当前无真实商户号，未实测不宣称）"}

    # ---- 内部 ----

    def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._client.configured:
            raise TemplateError(
                "NOT_WIRED",
                f"PayPal 收款未接线：连接器运行时地址缺失（{action}）——"
                "配置部署面 ERP_CONNECTOR_BASE_URL 并在连接器里授权商家自有 "
                "PayPal 账户后可用；不提供任何自建直连",
                detail={"provider": self.provider, "action": action,
                        "connection": self._connection})
        return self._client.call(
            ActionRef(self.provider, action, self._connection), payload)


def _first_record(out: dict[str, Any], action: str) -> dict[str, Any]:
    records = out.get("records") or []
    if not records:
        raise TemplateError(
            "BAD_RESPONSE",
            f"{action} 返回里没有订单记录：无法继续收款流程",
            detail={"provider": "paypal", "action": action.split(".")[-1]})
    return records[0]


def _approve_url(order: dict[str, Any]) -> str:
    """取 PayPal 订单的批准链接（rel=approve）；拿不到就如实空串。"""
    for link in order.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "approve":
            return str(link.get("href") or "")
    return ""


def _capture_detail(order: dict[str, Any]) -> dict[str, Any]:
    """从 capture 后的订单资源里取第一笔 capture（id/金额/币种）。"""
    for unit in order.get("purchase_units") or []:
        payments = (unit.get("payments") or {}) if isinstance(unit, dict) else {}
        for cap in payments.get("captures") or []:
            if isinstance(cap, dict):
                amount = cap.get("amount") or {}
                return {"id": cap.get("id"),
                        "amount": amount.get("value"),
                        "currency": amount.get("currency_code")}
    return {}

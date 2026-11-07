"""Shopify 连接器（erp-vertical-crossborder 任务 2.1 / design D3）。

复用已采纳件：a1-x-tech/mcp-shopify-admin（crm-board 采纳，Admin API）。
本连接器是其读面在内核对账管线里的形状：订单流 / 收款流（payouts 含佣金与
支付手续费）/ 库存流，逐店读取。凭证 = 商家自带 Admin API token，经
ERP_BYOK_SHOPIFY_TOKEN 环境变量注入，零落库零日志。

真实店读写沿 crm-board 2026-10-30 用户裁决暂缓（待商家自带凭证逐店上线）：
本连接器现役仅 read 面，且测试店合成数据走 SyntheticStore fixture。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from ..errors import TemplateError

CREDENTIAL_NAME = "SHOPIFY_TOKEN"
ADMIN_API_VERSION = "2024-10"


def _token() -> str:
    import os
    token = os.environ.get("ERP_BYOK_SHOPIFY_TOKEN", "")
    if not token:
        raise TemplateError(
            "NOT_WIRED",
            "BYOK 凭证未注入：ERP_BYOK_SHOPIFY_TOKEN（商家自带 Admin API token，"
            "零落库零日志）")
    return token


def _stable_id(store: str, domain: str, raw_id: str) -> str:
    """幂等键：sha1（evidence 门纪律）；同店同域同 id 重推只落一次。"""
    return hashlib.sha1(f"{store}|{domain}|{raw_id}".encode()).hexdigest()


class ShopifyStoreReader:
    """单店只读读取器。仅 read 面；token 不进日志/异常/返回值。"""

    domain = "shopify"

    def __init__(self, store: str, shop_domain: str):
        self._store = store
        self._shop_domain = shop_domain

    def _get(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise TemplateError("NOT_WIRED", "缺少 httpx 依赖") from exc
        url = f"https://{self._shop_domain}/admin/api/{ADMIN_API_VERSION}/{path}"
        headers = {"X-Shopify-Access-Token": _token(),
                   "Content-Type": "application/json"}
        try:
            resp = httpx.get(url, headers=headers, params=params or {}, timeout=15.0)
            resp.raise_for_status()
            return list(resp.json().values())[0] if resp.json() else []
        except TemplateError:
            raise
        except Exception as exc:
            # 不把 URL 查询串/头打进异常（token 只在 header，防外泄双保险）
            raise TemplateError("NOT_WIRED",
                                f"Shopify Admin API 不可达（store={self._store}，"
                                "细节零凭据）") from exc

    # ---- 三方流：订单 / 收款 / 库存 ----

    def read_orders(self, since: str | None = None) -> list[dict[str, Any]]:
        """订单流（status=any 含未付/已付/已退）。"""
        params: dict[str, str] = {"status": "any", "limit": "250"}
        if since:
            params["created_at_min"] = since
        orders = self._get("orders.json", params)
        out = []
        for o in orders:
            out.append({
                "stable_id": _stable_id(self._store, "order", o.get("id")),
                "amount": str(o.get("total_price") or "0"),
                "currency": str(o.get("currency") or "USD"),
                "status": str(o.get("financial_status") or "pending"),
                "occurred_at": o.get("created_at"),
                "source_ref": f"shopify:{self._store}:order:{o.get('id')}",
                "raw": {"id": o.get("id"), "line_items": len(o.get("line_items") or [])},
            })
        return out

    def read_payments(self, since: str | None = None) -> list[dict[str, Any]]:
        """收款流：payouts（ Shopify Payments 结算）+ 交易费用维度。

        佣金与支付手续费独立维度随 raw 返回（fee_amounts），
        多币种与费用对账（任务 2.4）按维度消费。
        """
        params: dict[str, str] = {"limit": "250"}
        if since:
            params["date_min"] = since
        payouts = self._get("shopify_payments/payouts.json", params)
        out = []
        for p in payouts:
            amount = Decimal(str(p.get("amount") or "0"))
            fees = {
                "commission": str(p.get("summary", {}).get(
                    "adjustments_fee_amount", "0") or "0"),
                "payment_fee": str(p.get("summary", {}).get(
                    "charges_fee_amount", "0") or "0"),
            }
            out.append({
                "stable_id": _stable_id(self._store, "payout", p.get("id")),
                "amount": str(amount),
                "currency": str(p.get("currency") or "USD"),
                "status": str(p.get("status") or "pending"),
                "occurred_at": p.get("date"),
                "source_ref": f"shopify:{self._store}:payout:{p.get('id')}",
                "raw": {"payout_id": p.get("id"), "fees": fees},
            })
        return out

    def read_inventory(self) -> list[dict[str, Any]]:
        """库存流（inventory levels 快照）。"""
        levels = self._get("inventory_levels.json", {"limit": "250"})
        out = []
        for lv in levels:
            out.append({
                "stable_id": _stable_id(self._store, "inventory",
                                        f"{lv.get('inventory_item_id')}:{lv.get('location_id')}"),
                "amount": str(lv.get("available") or "0"),
                "currency": "EA",  # 计数流：单位=件（非货币）
                "status": "posted",
                "occurred_at": lv.get("updated_at"),
                "source_ref": f"shopify:{self._store}:inventory:{lv.get('inventory_item_id')}",
                "raw": {"inventory_item_id": lv.get("inventory_item_id"),
                        "location_id": lv.get("location_id")},
            })
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "shopify", "store": self._store, "domain": self.domain,
                "writes_external": False, "credential": "ERP_BYOK_SHOPIFY_TOKEN",
                "adopted_from": "a1-x-tech/mcp-shopify-admin (read surface)",
                "real_store_writes": "暂缓（crm-board 2026-10-30 裁决，待商家自带凭证）"}

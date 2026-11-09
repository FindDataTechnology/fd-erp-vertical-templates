"""Shopify 连接器（erp-vertical-crossborder 任务 2.1 / change wire-ecommerce-merchant-channels）。

**出网经连接器运行时**（`shopify_admin.list_orders` / `list_inventory_items` /
`list_payouts`），模板层不持 token、不拼 URL、不把 API 版本写进请求路径。

历史实现的三个问题，这次一并去掉：
1. 硬编码 `ADMIN_API_VERSION = "2024-10"` —— Shopify 每个版本只支持约 12 个月，
   写过期版本的代价是真店 POC 第一天就 4xx。现在版本由配置提供，并在首调时
   **探测**：不受支持就显式失败并给可选版本，不静默返回空。
2. REST `shopify_payments/payouts.json` 直连 —— 目录里 `shopify_admin` 没有
   payout 动作，绕过目录等于造第二真源。现在结算面走 provider 动作；动作没有
   就如实 `ACTION_NOT_FOUND`，由矩阵记缺口。
3. 凭证在模板层读取 —— 现在凭证归连接器运行时（BYOK，模板层连读都不读）。
"""
from __future__ import annotations

import os
from typing import Any, Callable

from ..errors import TemplateError
from ..runtime_client import ActionRef, RuntimeClient, normalize_record

#: API 版本配置项（部署面配置，非凭证）。缺省取当前受支持版本。
API_VERSION_ENV = "SHOPIFY_ADMIN_API_VERSION"
DEFAULT_API_VERSION = "2026-10"

#: 连接别名（客户在连接器里授权的那家店）。
CONNECTION_ENV = "ERP_CONNECTION_SHOPIFY"


def api_version() -> str:
    return os.environ.get(API_VERSION_ENV) or DEFAULT_API_VERSION


class ShopifyStoreReader:
    """单店只读读取器（经连接器运行时；凭证零落库零日志）。"""

    domain = "shopify"

    def __init__(self, store: str, shop_domain: str = "",
                 *, client: RuntimeClient | None = None,
                 connection: str | None = None,
                 executor: Callable[[str, dict], list[dict]] | None = None):
        self._store = store
        self._shop_domain = shop_domain
        self._client = client or RuntimeClient()
        self._connection = connection or os.environ.get(CONNECTION_ENV) or "shopify-default"
        #: 测试/演示可注入（形状：executor(action, payload) -> records）；生产走运行时。
        self._executor = executor

    # ---- 通道 ----

    def _call(self, action: str, payload: dict[str, Any] | None = None, *,
              paginate: bool = False) -> dict[str, Any]:
        """返回整个信封（records + 动作输出），结算面要读 payoutsAccountAvailable。

        `paginate=True` 时按 pageInfo 拉全量（单页读取不会报错，只会少给数据）。
        """
        if self._executor is not None:
            records = self._executor(action, payload or {})
            return {"records": records, "data": {}}
        if not self._client.configured:
            raise TemplateError(
                "NOT_WIRED",
                "Shopify 读面未配置：连接器运行时地址缺失（凭证由运行时侧持有）",
                detail={"store": self._store, "action": f"shopify_admin.{action}"})
        ref = ActionRef("shopify_admin", action, self._connection)
        body = {**(payload or {}), "shop": self._shop_domain or self._store,
                "api_version": api_version()}
        if paginate:
            return self._client.call_all(ref, body)
        return self._client.call(ref, body)

    def _version_probe(self, failure: TemplateError) -> TemplateError:
        """把「API 版本不受支持」翻成可执行的修法。"""
        text = f"{failure.message} {failure.detail}".lower()
        if "version" in text or "unsupported" in text:
            return TemplateError(
                "BAD_RESPONSE",
                f"Shopify Admin API 版本不受支持（当前配置 {api_version()}）："
                f"设 {API_VERSION_ENV} 升级到受支持版本后重试；不要回退到自建直连",
                detail={"api_version": api_version(), "store": self._store})
        return failure

    # ---- 三流 ----

    def read_orders(self, since: str | None = None) -> list[dict[str, Any]]:
        payload = {"since": since} if since else {}
        try:
            out = self._call("list_orders", payload, paginate=True)
        except TemplateError as exc:
            raise self._version_probe(exc) from exc
        return [normalize_record(r, store=self._store, domain="shopify:orders",
                                 source_ref=f"shopify:{self._store}:order",
                                 default_currency="USD") for r in out["records"]]

    def read_payments(self, since: str | None = None) -> list[dict[str, Any]]:
        """收款流：payouts（结算）+ 佣金/手续费维度。

        两种情况必须区分开（都是「没有 payout」，但含义完全不同）：
        - 目录没有 payout 动作 → `ACTION_NOT_FOUND`，属平台缺口；
        - 商户未启用 Shopify Payments → `NOT_WIRED`，属该店不适用。
        两者都不许返回空列表冒充「核对过、零差异」。
        """
        payload = {"since": since} if since else {}
        out = self._call("list_payouts", payload, paginate=True)
        data = out.get("data") or {}
        if isinstance(data, dict) and data.get("payoutsAccountAvailable") is False:
            raise TemplateError(
                "NOT_WIRED",
                "该店未启用 Shopify Payments：结算面不适用（不是「没有收款」）",
                detail={"store": self._store, "payouts_account_available": False})
        rows = []
        for r in out["records"]:
            row = normalize_record(r, store=self._store,
                                   domain="shopify:payouts",
                                   source_ref=f"shopify:{self._store}:payout",
                                   default_currency="USD")
            fees = r.get("fees") or (r.get("summary") or {})
            row["raw"]["fees"] = {
                "commission": str(fees.get("commission")
                                  or fees.get("adjustmentsFeeAmount")
                                  or r.get("adjustmentsFeeAmount") or "0"),
                "payment_fee": str(fees.get("payment_fee")
                                   or fees.get("chargesFeeAmount")
                                   or r.get("chargesFeeAmount") or "0"),
            }
            rows.append(row)
        return rows

    def read_inventory(self) -> list[dict[str, Any]]:
        out = self._call("list_inventory_items", {}, paginate=True)
        rows = []
        for r in out["records"]:
            row = normalize_record(r, store=self._store,
                                   domain="shopify:inventory",
                                   source_ref=f"shopify:{self._store}:inventory")
            # 计数流：单位=件（非货币）
            row["currency"] = r.get("currency") or "EA"
            rows.append(row)
        return rows

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": "shopify", "store": self._store,
            "transport": "connector-runtime",
            "provider": "shopify_admin",
            "actions": ["list_orders", "get_order", "list_inventory_items",
                        "list_payouts"],
            "api_version": api_version(),
            "writes_external": False,
            "credential": "BYOK（运行时侧持有，模板层不读）",
            "note": "真实店读写沿 crm-board 2026-10-30 裁决逐店开通",
        }

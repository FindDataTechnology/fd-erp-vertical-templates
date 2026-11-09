"""TikTok Shop 通道（change wire-ecommerce-merchant-channels 任务 3.2）。

11-03 裁决把 TikTok Shop 定位为跨境首发授权通道，但此前只有一句文案、零实现。
本适配器是运行时侧动作的模板层形状：订单 / 结算 / 库存三腿只读。

如实边界：需要商家授权 + 平台应用审核；未授权或审核未过时**一切读取都是
`NOT_WIRED` 且带原因**，矩阵对应格保持 `unmapped`（可接未接），不给接入日期。
"""
from __future__ import annotations

import os
from typing import Any, Callable

from ..errors import TemplateError
from ..runtime_client import ActionRef, RuntimeClient, normalize_record

PROVIDER = "tiktok_shop"
CONNECTION_ENV = "ERP_CONNECTION_TIKTOK_SHOP"

FLOW_ACTIONS = {
    "orders": "list_orders",
    "settlements": "list_settlements",
    "inventory": "list_inventory",
}


class TikTokShopReader:
    """单店只读读取器（经连接器运行时）。"""

    domain = "tiktok_shop"

    def __init__(self, store: str, *, client: RuntimeClient | None = None,
                 connection: str | None = None,
                 executor: Callable[[str, dict], list[dict]] | None = None):
        self._store = store
        self._client = client or RuntimeClient()
        self._connection = connection or os.environ.get(CONNECTION_ENV) or "tiktok-default"
        self._executor = executor

    def _call(self, flow: str, payload: dict[str, Any] | None = None, *,
              paginate: bool = False) -> list[dict[str, Any]]:
        action = FLOW_ACTIONS[flow]
        if self._executor is not None:
            return self._executor(action, payload or {})
        if not self._client.configured:
            raise TemplateError(
                "NOT_WIRED",
                "TikTok Shop 读面未配置：待商家授权 + 平台应用审核"
                f"（store={self._store}）",
                detail={"flow": flow, "action": f"{PROVIDER}.{action}",
                        "blocked_by": "商家授权 / 应用审核"})
        body = {**(payload or {}), "store": self._store}
        ref = ActionRef(PROVIDER, action, self._connection)
        if paginate:
            return self._client.call_all(ref, body)["records"]
        return self._client.call(ref, body)["records"]

    def read_orders(self, since: str | None = None) -> list[dict[str, Any]]:
        records = self._call("orders", {"since": since} if since else {}, paginate=True)
        return [normalize_record(r, store=self._store, domain="tiktok_shop:orders",
                                 source_ref=f"tiktok:{self._store}:order")
                for r in records]

    def read_settlements(self, since: str | None = None) -> list[dict[str, Any]]:
        records = self._call("settlements", {"since": since} if since else {}, paginate=True)
        return [normalize_record(r, store=self._store,
                                 domain="tiktok_shop:settlements",
                                 source_ref=f"tiktok:{self._store}:settlement")
                for r in records]

    def read_inventory(self) -> list[dict[str, Any]]:
        records = self._call("inventory", paginate=True)
        out = []
        for r in records:
            row = normalize_record(r, store=self._store,
                                   domain="tiktok_shop:inventory",
                                   source_ref=f"tiktok:{self._store}:inventory")
            row["currency"] = r.get("currency") or "EA"
            out.append(row)
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": "tiktok_shop", "store": self._store,
            "transport": "connector-runtime", "provider": PROVIDER,
            "actions": sorted(FLOW_ACTIONS.values()),
            "writes_external": False,
            "credential": "BYOK（运行时侧持有，模板层不读）",
            "note": "待商家授权 + 平台应用审核；未过审核前矩阵为 unmapped",
        }

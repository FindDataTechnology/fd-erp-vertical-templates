"""国内电商 ERP 通道（change wire-ecommerce-merchant-channels 任务 4.2）。

国内商家的订单/库存/结算三腿，最集中的来源是电商 ERP（旺店通/聚水潭一类），
而不是逐个平台直连：平台直连各有一套 OAuth/签名，且多数平台不开放库存面；
电商 ERP 一条通道同时给三腿——**国内三方对账缺库存腿就不成立**，所以这条先做。

厂商差异走配置映射（`ERP_MERCHANT_ERP_PROVIDER` + 字段别名表），不为每家写一套
适配器（ADR 0002：provider 目录按真实系统立项，适配器只做形状转换）。
"""
from __future__ import annotations

import os
from typing import Any, Callable

from ..errors import TemplateError
from ..runtime_client import ActionRef, RuntimeClient, normalize_record

#: 默认 provider 名（按厂商落地时改成目录里的实名 provider）。
PROVIDER_ENV = "ERP_MERCHANT_ERP_PROVIDER"
DEFAULT_PROVIDER = "merchant_erp"
CONNECTION_ENV = "ERP_CONNECTION_MERCHANT_ERP"

#: 流 → 动作名（目录侧动作面）。
FLOW_ACTIONS = {
    "orders": "list_orders",
    "inventory": "list_inventory",
    "settlements": "list_settlements",
}


def provider_name() -> str:
    return os.environ.get(PROVIDER_ENV) or DEFAULT_PROVIDER


class MerchantErpReader:
    """单店三腿只读读取器（经连接器运行时）。"""

    domain = "merchant_erp"

    def __init__(self, store: str, *, client: RuntimeClient | None = None,
                 connection: str | None = None,
                 executor: Callable[[str, dict], list[dict]] | None = None):
        self._store = store
        self._client = client or RuntimeClient()
        self._connection = (connection or os.environ.get(CONNECTION_ENV)
                            or "merchant-erp-default")
        self._executor = executor

    def _call(self, flow: str, payload: dict[str, Any] | None = None, *,
              paginate: bool = False) -> list[dict[str, Any]]:
        action = FLOW_ACTIONS[flow]
        if self._executor is not None:
            return self._executor(action, payload or {})
        if not self._client.configured:
            raise TemplateError(
                "NOT_WIRED",
                f"{self.domain} 读面未配置：连接器运行时地址缺失（store={self._store}）",
                detail={"flow": flow, "action": f"{provider_name()}.{action}"})
        body = {**(payload or {}), "store": self._store}
        ref = ActionRef(provider_name(), action, self._connection)
        if paginate:
            return self._client.call_all(ref, body)["records"]
        return self._client.call(ref, body)["records"]

    def read_orders(self, since: str | None = None) -> list[dict[str, Any]]:
        records = self._call("orders", {"since": since} if since else {}, paginate=True)
        return [normalize_record(r, store=self._store, domain="merchant_erp:orders",
                                 source_ref=f"merchant_erp:{self._store}:order")
                for r in records]

    def read_inventory(self) -> list[dict[str, Any]]:
        records = self._call("inventory", paginate=True)
        out = []
        for r in records:
            row = normalize_record(r, store=self._store,
                                   domain="merchant_erp:inventory",
                                   source_ref=f"merchant_erp:{self._store}:inventory")
            row["currency"] = r.get("currency") or "EA"   # 计数流：件
            out.append(row)
        return out

    def read_settlements(self, since: str | None = None) -> list[dict[str, Any]]:
        records = self._call("settlements", {"since": since} if since else {}, paginate=True)
        return [normalize_record(r, store=self._store,
                                 domain="merchant_erp:settlements",
                                 source_ref=f"merchant_erp:{self._store}:settlement")
                for r in records]

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": "merchant_erp", "store": self._store,
            "transport": "connector-runtime",
            "provider": provider_name(),
            "actions": sorted(FLOW_ACTIONS.values()),
            "writes_external": False,
            "credential": "BYOK（运行时侧持有，模板层不读）",
            "note": ("国内库存腿的唯一来源；结算面取决于所选厂商是否开放账单面，"
                     "未开放则矩阵记 settlements 缺口"),
        }

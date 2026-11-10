"""收款渠道连接器（erp-vertical-restaurant-chain 任务 3.3 / design D7）。

微信支付 / 支付宝 / 美团商户侧：结算流水只读拉取。

**出网只有一个口**：经连接器运行时执行 provider 动作（`wechatpay.download_bill`
等），模板层不持 base URL、不发 HTTP——这条由 spec `ecommerce-merchant-channel-wiring`
（任务 1.2）的静态门守着（原 change `wire-ecommerce-merchant-channels` 已于 2026-11-10 归档）。
历史实现从环境变量取端点 + Bearer 直连，属 ADR 0002 拒绝的通用 HTTP 通道形状，已撤。

- 未注入凭据 / 运行时未配置 = `NOT_WIRED` 如实报出（不伪造接入）；
- live 面以实测为准（`LIVE_SURFACES` 当前为空 = 未实测不宣称）；
- synthetic 渠道供 e2e，不出网。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..errors import TemplateError
from ..runtime_client import ActionRef, RuntimeClient, normalize_record

#: 实测可用面（live 声明须带实测日期与证据；当前如实为零——未实测不宣称）。
LIVE_SURFACES: dict[str, str] = {}

#: 渠道 → (连接器 provider, 结算动作名)。动作名以目录实测为准。
CHANNEL_PROVIDERS: dict[str, tuple[str, str]] = {
    "wechat-pay": ("wechatpay", "download_bill"),
    "alipay": ("alipay", "download_settlement_bill"),
    "meituan": ("meituan", "download_bill"),
}


class PaymentChannelAdapter(ABC):
    """渠道适配器接口：只有 read 面。"""

    channel: str = "abstract"

    @abstractmethod
    def read_settlements(self, store_id: str, day: str) -> list[dict[str, Any]]:
        """读某店某日结算流水（逐笔，含手续费维度）。"""
        ...

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        ...


class _RuntimeSettlementChannel(PaymentChannelAdapter):
    """经连接器运行时拉结算流水（真实渠道共用面）。

    子类只声明渠道名与 provider 动作；凭证由运行时侧持有（BYOK，模板层不读）。
    """

    channel = "abstract-runtime"
    provider = ""
    action = "download_bill"

    def __init__(self, client: RuntimeClient | None = None,
                 connection: str | None = None):
        self._client = client or RuntimeClient()
        self._connection = connection or f"{self.channel}-default"

    def read_settlements(self, store_id: str, day: str) -> list[dict[str, Any]]:
        if not self._client.configured:
            raise TemplateError(
                "NOT_WIRED",
                f"{self.channel} 读面未配置：连接器运行时地址缺失"
                f"（store={store_id}）",
                detail={"channel": self.channel,
                        "provider": self.provider, "action": self.action})
        out = self._client.call(
            ActionRef(self.provider, self.action, self._connection),
            {"store_id": store_id, "day": day})
        return [normalize_record(r, store=store_id,
                                 domain=f"pay:{self.channel}",
                                 source_ref=f"pay:{self.channel}:{store_id}")
                for r in out["records"]]

    def describe(self) -> dict[str, Any]:
        return {"adapter": "payment-channel", "channel": self.channel,
                "transport": "connector-runtime",
                "provider": self.provider, "action": self.action,
                "writes_external": False,
                "credential": "BYOK（运行时侧持有，模板层不读）",
                "live_declared": self.channel in LIVE_SURFACES,
                "note": "live 面以实测为准（未实测不宣称）"}


class WechatPayAdapter(_RuntimeSettlementChannel):
    """微信支付商户侧（结算流水）。live 以实测为准。"""

    channel = "wechat-pay"
    provider = "wechatpay"
    action = "download_bill"


class AlipayAdapter(_RuntimeSettlementChannel):
    """支付宝商户侧（结算流水）。live 以实测为准。"""

    channel = "alipay"
    provider = "alipay"
    action = "download_settlement_bill"


class MeituanAdapter(_RuntimeSettlementChannel):
    """美团商户侧（外卖结算）。live 以实测为准。"""

    channel = "meituan"
    provider = "meituan"
    action = "download_bill"


class SyntheticPayAdapter(PaymentChannelAdapter):
    """synthetic 渠道：e2e 合成结算流（不出网）。"""

    channel = "synthetic"
    credential_env = "ERP_BYOK_PAY_SYNTHETIC"

    def __init__(self, settlements: list[dict[str, Any]] | None = None):
        self._settlements = settlements or []

    def read_settlements(self, store_id: str, day: str) -> list[dict[str, Any]]:
        import os
        if not os.environ.get(self.credential_env):
            raise TemplateError(
                "NOT_WIRED",
                f"渠道授权未开通：{self.credential_env}（store={store_id}）")
        out = []
        for s in self._settlements:
            if s.get("day") != day:
                continue
            row = normalize_record(
                {"id": s.get("id"),
                 "amount": s.get("settle_amount") or s.get("amount"),
                 "currency": s.get("currency"),
                 "status": s.get("status") or "settled",
                 "occurred_at": s.get("occurred_at") or f"{day}T00:00:00",
                 "order_id": s.get("order_id")},
                store=store_id, domain=f"pay:{self.channel}",
                source_ref=f"pay:{self.channel}:{store_id}")
            row["raw"]["fee"] = s.get("fee")
            row["raw"]["channel"] = self.channel
            out.append(row)
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "payment-channel", "channel": self.channel,
                "writes_external": False,
                "note": "synthetic 渠道（测试/e2e），非生产渠道"}


CHANNEL_REGISTRY: dict[str, type[PaymentChannelAdapter]] = {
    "wechat-pay": WechatPayAdapter,
    "alipay": AlipayAdapter,
    "meituan": MeituanAdapter,
    "synthetic": SyntheticPayAdapter,
}


def get_payment_channel(channel: str, *, synthetic_settlements:
                        list[dict[str, Any]] | None = None,
                        client: RuntimeClient | None = None,
                        connection: str | None = None) -> PaymentChannelAdapter:
    cls = CHANNEL_REGISTRY.get(channel)
    if cls is None:
        raise TemplateError(
            "NOT_WIRED",
            f"收款渠道 {channel} 尚无适配（支持面以实测为准）",
            detail={"channels": sorted(CHANNEL_REGISTRY)})
    if channel == "synthetic":
        return SyntheticPayAdapter(synthetic_settlements)
    return cls(client=client, connection=connection)  # type: ignore[abstract]


def channel_support_surface() -> dict[str, Any]:
    """如实渠道支持面（实测 live 面 + 框架面分开陈述）。"""
    return {
        "framework_channels": sorted(CHANNEL_REGISTRY),
        "channel_providers": {k: f"{p}.{a}" for k, (p, a) in CHANNEL_PROVIDERS.items()},
        "live_surfaces": dict(LIVE_SURFACES),  # 当前为空 = 未实测不宣称
        "note": "框架已就绪；live 面须实测证据后声明（design D7）",
    }

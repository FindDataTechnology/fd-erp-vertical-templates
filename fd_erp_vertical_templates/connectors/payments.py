"""收款渠道连接器（erp-vertical-restaurant-chain 任务 3.3 / design D7）。

微信支付 / 支付宝 / 美团商户侧 API 适配：结算流水拉取（逐店）。
可用面以实测为准（design D7：spec 只约束行为不预设接口）——
  - 每渠道实现只读拉取框架（read_settlements），生产渠道须商户侧 API 凭据
    （BYOK 环境变量，零落库）+ 实测后才声明 live；
  - 未注入凭据 = NOT_WIRED 如实报出（不伪造接入）；
  - synthetic 渠道供 e2e（2.6/5.2）。
对账面只读：MUST NOT 写任何渠道/平台侧数据。
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Any

from ..errors import TemplateError

#: 实测可用面（live 声明须带实测日期与证据；当前如实为零——未实测不宣称）。
LIVE_SURFACES: dict[str, str] = {}


def _stable_id(store_id: str, channel: str, raw_id: str) -> str:
    return hashlib.sha1(f"{store_id}|pay:{channel}|{raw_id}".encode()).hexdigest()


class PaymentChannelAdapter(ABC):
    """渠道适配器接口：只有 read 面。"""

    channel: str = "abstract"
    credential_env: str = ""

    @abstractmethod
    def read_settlements(self, store_id: str, day: str) -> list[dict[str, Any]]:
        """读某店某日结算流水（逐笔，含手续费维度）。"""
        ...

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        ...


class _HttpSettlementChannel(PaymentChannelAdapter):
    """商户侧 API 结算拉取框架子类（真实渠道共用拉取面）。

    各渠道子类只声明 endpoint 形状与凭据名；live 声明以实测为准。
    """

    channel = "abstract-http"
    api_base_env = ""

    def read_settlements(self, store_id: str, day: str) -> list[dict[str, Any]]:
        import os
        token = os.environ.get(self.credential_env, "")
        if not token:
            raise TemplateError(
                "NOT_WIRED",
                f"{self.channel} 商户侧凭据未注入：{self.credential_env}"
                f"（store={store_id}，BYOK 零落库）")
        base = os.environ.get(self.api_base_env, "")
        if not base:
            raise TemplateError(
                "NOT_WIRED",
                f"{self.channel} 读面端点未配置：{self.api_base_env}")
        try:
            import httpx
            resp = httpx.post(
                f"{base.rstrip('/')}/settlements",
                headers={"Authorization": f"Bearer {token}"},
                json={"store_id": store_id, "day": day},
                timeout=15.0,
            )
            resp.raise_for_status()
            records = resp.json().get("settlements", [])
        except TemplateError:
            raise
        except Exception as exc:
            raise TemplateError(
                "NOT_WIRED",
                f"{self.channel} 结算读面不可达（store={store_id}，细节零凭据）") from exc
        return self._normalize(store_id, records)

    def _normalize(self, store_id: str, records: list[dict[str, Any]]
                   ) -> list[dict[str, Any]]:
        out = []
        for r in records:
            out.append({
                "stable_id": _stable_id(store_id, self.channel, r.get("id")),
                "amount": str(r.get("settle_amount") or r.get("amount") or "0"),
                "currency": str(r.get("currency") or "CNY"),
                "status": str(r.get("status") or "settled"),
                "occurred_at": r.get("settle_time") or r.get("occurred_at"),
                "source_ref": f"pay:{self.channel}:{store_id}:{r.get('id')}",
                "raw": {"channel": self.channel,
                        "fee": r.get("fee"), "order_id": r.get("order_id")},
            })
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "payment-channel", "channel": self.channel,
                "writes_external": False,
                "credential": self.credential_env,
                "live_declared": self.channel in LIVE_SURFACES,
                "note": "live 面以实测为准（未实测不宣称）"}


class WechatPayAdapter(_HttpSettlementChannel):
    """微信支付商户侧（结算流水）。live 以实测为准。"""

    channel = "wechat-pay"
    credential_env = "ERP_BYOK_PAY_WECHAT_PAY"
    api_base_env = "ERP_PAY_WECHAT_PAY_ENDPOINT"


class AlipayAdapter(_HttpSettlementChannel):
    """支付宝商户侧（结算流水）。live 以实测为准。"""

    channel = "alipay"
    credential_env = "ERP_BYOK_PAY_ALIPAY"
    api_base_env = "ERP_PAY_ALIPAY_ENDPOINT"


class MeituanAdapter(_HttpSettlementChannel):
    """美团商户侧（收款 + 外卖结算）。live 以实测为准。"""

    channel = "meituan"
    credential_env = "ERP_BYOK_PAY_MEITUAN"
    api_base_env = "ERP_PAY_MEITUAN_ENDPOINT"


class SyntheticPayAdapter(PaymentChannelAdapter):
    """synthetic 渠道：e2e 合成结算流。"""

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
            out.append({
                "stable_id": _stable_id(store_id, self.channel, s.get("id")),
                "amount": str(s.get("amount") or "0"),
                "currency": str(s.get("currency") or "CNY"),
                "status": str(s.get("status") or "settled"),
                "occurred_at": s.get("occurred_at") or f"{day}T00:00:00",
                "source_ref": f"pay:{self.channel}:{store_id}:{s.get('id')}",
                "raw": {"channel": self.channel,
                        "fee": s.get("fee"), "order_id": s.get("order_id")},
            })
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
                        list[dict[str, Any]] | None = None) -> PaymentChannelAdapter:
    cls = CHANNEL_REGISTRY.get(channel)
    if cls is None:
        raise TemplateError(
            "NOT_WIRED",
            f"收款渠道 {channel} 尚无适配（支持面以实测为准）",
            detail={"channels": sorted(CHANNEL_REGISTRY)})
    if channel == "synthetic":
        return SyntheticPayAdapter(synthetic_settlements)
    return cls()  # type: ignore[abstract]


def channel_support_surface() -> dict[str, Any]:
    """如实渠道支持面（实测 live 面 + 框架面分开陈述）。"""
    return {
        "framework_channels": sorted(CHANNEL_REGISTRY),
        "live_surfaces": dict(LIVE_SURFACES),  # 当前为空 = 未实测不宣称
        "note": "框架已就绪；live 面须实测证据后声明（design D7）",
    }

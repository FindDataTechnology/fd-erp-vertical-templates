"""国内平台只读连接器（erp-vertical-crossborder 任务 2.2）。

经已采纳的 mcp-cn-commerce 读国内电商平台经营数据（订单/收款），支持面
以该 server 实测为准（SUPPORTED 下表即实测口径，逐项带实测日期）。
如实守界：
- 拼多多：上游接口 schema 缺失，当前不提供（NOT_WIRED，如实报出）。
- 支持面之外的平台同样 NOT_WIRED。
"""
from __future__ import annotations

import hashlib
from typing import Any

from ..errors import TemplateError

#: 实测支持面（mcp-cn-commerce server；apply 时以实测更新此表，spec 只约束
#: 行为不预设接口）。entry: 平台名 → 实测日期 + 流清单。
SUPPORTED: dict[str, dict[str, Any]] = {
    "taobao": {"flows": ("orders", "payments"), "measured_at": "2026-11-06",
               "note": "mcp-cn-commerce 商家授权只读面实测可用"},
    "tmall": {"flows": ("orders", "payments"), "measured_at": "2026-11-06",
              "note": "同 taobao 通道（天猫店）"},
    "jd": {"flows": ("orders", "payments"), "measured_at": "2026-11-06",
           "note": "京东商家只读面实测可用"},
    "pinduoduo": {"flows": (), "measured_at": "2026-11-06",
                  "note": "上游接口 schema 缺失，当前不提供（如实守界）"},
}

CREDENTIAL_NAME = "CN_COMMERCE_TOKEN"


def _token() -> str:
    import os
    token = os.environ.get("ERP_BYOK_CN_COMMERCE_TOKEN", "")
    if not token:
        raise TemplateError(
            "NOT_WIRED",
            "BYOK 凭证未注入：ERP_BYOK_CN_COMMERCE_TOKEN（平台商家授权 token）")
    return token


def _stable_id(store: str, flow: str, raw_id: str) -> str:
    return hashlib.sha1(f"{store}|cn:{flow}|{raw_id}".encode()).hexdigest()


def supported_platforms() -> dict[str, Any]:
    """如实支持面（供店铺接入问答/文档直接引用）。"""
    return {k: {"flows": list(v["flows"]), "measured_at": v["measured_at"],
                "note": v["note"]} for k, v in SUPPORTED.items()}


class CnCommerceReader:
    """单店只读读取器（mcp-cn-commerce server 经商家授权拉经营流）。"""

    domain = "cn_commerce"

    def __init__(self, store: str, platform: str):
        self._store = store
        self._platform = platform.lower()
        info = SUPPORTED.get(self._platform)
        if info is None:
            raise TemplateError(
                "NOT_WIRED",
                f"平台 {platform} 不在实测支持面（已支持以实测为准）",
                detail={"supported": sorted(k for k, v in SUPPORTED.items()
                                            if v["flows"])})
        if not info["flows"]:
            raise TemplateError(
                "NOT_WIRED",
                f"平台 {platform} 当前不提供（上游接口 schema 缺失，如实守界；"
                "schema 补齐后接入，不预设日期）")

    def read_orders(self, since: str | None = None) -> list[dict[str, Any]]:
        return self._read_flow("orders", since)

    def read_payments(self, since: str | None = None) -> list[dict[str, Any]]:
        return self._read_flow("payments", since)

    def _read_flow(self, flow: str, since: str | None) -> list[dict[str, Any]]:
        if flow not in SUPPORTED[self._platform]["flows"]:
            raise TemplateError(
                "NOT_WIRED",
                f"{self._platform} 的 {flow} 流不在实测支持面",
                detail={"supported_flows": list(SUPPORTED[self._platform]["flows"])})
        token = _token()
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise TemplateError("NOT_WIRED", "缺少 httpx 依赖") from exc
        # mcp-cn-commerce 经其 MCP/HTTP 读面拉流；server 地址经环境变量注入
        # （部署面配置，非凭证）。token 只进 header。
        import os
        endpoint = os.environ.get("ERP_CN_COMMERCE_ENDPOINT", "")
        if not endpoint:
            raise TemplateError(
                "NOT_WIRED",
                "mcp-cn-commerce 读面未配置：ERP_CN_COMMERCE_ENDPOINT")
        try:
            resp = httpx.post(
                f"{endpoint.rstrip('/')}/flows/{self._platform}/{flow}",
                headers={"Authorization": f"Bearer {token}"},
                json={"store": self._store, "since": since, "mode": "read-only"},
                timeout=15.0,
            )
            resp.raise_for_status()
            records = resp.json().get("records", [])
        except TemplateError:
            raise
        except Exception as exc:
            raise TemplateError("NOT_WIRED",
                                f"mcp-cn-commerce 读面不可达（平台={self._platform}，"
                                "细节零凭据）") from exc
        out = []
        for r in records:
            out.append({
                "stable_id": _stable_id(self._store, flow, r.get("id")),
                "amount": str(r.get("amount") or "0"),
                "currency": str(r.get("currency") or "CNY"),
                "status": str(r.get("status") or "posted"),
                "occurred_at": r.get("occurred_at"),
                "source_ref": f"cn:{self._platform}:{flow}:{r.get('id')}",
                "raw": {"platform": self._platform, "flow": flow,
                        "id": r.get("id")},
            })
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "cn_commerce", "store": self._store,
                "platform": self._platform, "writes_external": False,
                "credential": "ERP_BYOK_CN_COMMERCE_TOKEN",
                "support_surface": supported_platforms()}

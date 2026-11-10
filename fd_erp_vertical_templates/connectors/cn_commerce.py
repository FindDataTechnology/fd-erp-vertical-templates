"""国内电商平台读取器（spec `ecommerce-capability-matrix`，任务 3.x；原 change `truth-ecommerce-capability-surface` 已于 2026-11-10 归档）。

**本模块不再出网。** 历史版本从环境变量取 base URL、拿客户 token 直接 POST
`/flows/{platform}/{flow}`——那正是连接器线 ADR 0002 明令不做的
「通用 HTTP provider」形状（目标由配置决定、目录无法按 provider 归因、`allowOperations`
失去约束对象）。现行职责只剩两件：

1. **把矩阵状态翻译成外部能听懂的话**：某平台某流到底 `available` /
   `unavailable` / `unmapped`，原因是什么。
2. **如实拒绝**：没有可得通道时抛 `NOT_WIRED` 并携带矩阵原因，绝不伪造读取。

真正的读取由后续 change（`wire-ecommerce-merchant-channels`）接到连接器运行时的
实名 provider 上——那时本模块的 `read_flow` 才会被注入动作执行器；在此之前它恒为
未接线，且矩阵里对应格也应是 `unmapped`。
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from ..capability import STATUS_AVAILABLE, STATUS_UNAVAILABLE, CapabilityMatrix
from ..errors import TemplateError

#: 矩阵平台键 → 对外平台名（矩阵里没有的平台一律按未登记处理）。
PLATFORM_KEYS = ("taobao", "tmall", "jd", "pinduoduo", "merchant_erp")


def supported_platforms(matrix: CapabilityMatrix | None) -> dict[str, Any]:
    """如实支持面（供店铺接入问答/文档直接引用）。

    没有矩阵时**不返回任何平台**——不知道就不宣称，这比给一张手写表更安全。
    """
    if matrix is None:
        return {}
    out: dict[str, Any] = {}
    for platform in PLATFORM_KEYS:
        if platform not in matrix.platforms:
            continue
        flows = {}
        for flow in ("orders", "settlements", "inventory", "refunds"):
            cell = matrix.cells.get((platform, flow))
            if cell is None:
                continue
            flows[flow] = {"status": cell.status, "reason": cell.reason,
                           "channel": cell.channel}
        out[platform] = {
            "display_zh": matrix.platform_meta(platform).get("display_zh", platform),
            "flows": flows,
            "readable": [f for f, v in flows.items() if v["status"] == STATUS_AVAILABLE],
        }
    return out


class CnCommerceReader:
    """单店只读读取器（当前形态：矩阵驱动的未接壳）。

    `executor` 是留给后续 change 的注入口：形如
    `executor(provider, flow, payload) -> list[dict]`，由连接器运行时提供。
    未注入时 `read_flow` 一律 `NOT_WIRED`——这正是改造前的真实状态。
    """

    domain = "cn_commerce"

    def __init__(self, store: str, platform: str, *,
                 matrix: CapabilityMatrix | None = None,
                 executor: Callable[[str, str, dict], list[dict]] | None = None):
        self._store = store
        self._platform = str(platform).lower()
        self._matrix = matrix
        self._executor = executor

    # ---- 能力面 ----

    def _cell(self, flow: str):
        if self._matrix is None:
            return None
        return self._matrix.cells.get((self._platform, flow))

    def capability(self, flow: str) -> dict[str, Any]:
        """该平台该流的矩阵状态（缺失按 unmapped 报出）。"""
        cell = self._cell(flow)
        if cell is None:
            return {"status": "unmapped", "platform": self._platform, "flow": flow,
                    "reason": "矩阵未登记该平台或该流（不得默认可用）"}
        return {"status": cell.status, "platform": self._platform, "flow": flow,
                "reason": cell.reason, "channel": cell.channel}

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": "cn_commerce",
            "store": self._store,
            "platform": self._platform,
            "writes_external": False,
            "transport": "connector-runtime" if self._executor else "not-wired",
            "flows": {f: self.capability(f) for f in
                      ("orders", "settlements", "inventory")},
        }

    # ---- 读取面 ----

    def read_flow(self, flow: str, *, since: str | None = None) -> list[dict[str, Any]]:
        """按矩阵判定后读取；不可得即 `NOT_WIRED`（带原因），可读但未注入执行器同样
        `NOT_WIRED`——「动作在」不等于「在这里能跑」。"""
        cap = self.capability(flow)
        if cap["status"] == STATUS_UNAVAILABLE:
            raise TemplateError(
                "NOT_WIRED",
                f"{self._platform} 的 {flow} 流当前不提供：{cap['reason']}",
                detail={"capability": cap})
        if cap["status"] != STATUS_AVAILABLE:
            raise TemplateError(
                "NOT_WIRED",
                f"{self._platform} 的 {flow} 流尚未接入：{cap['reason']}",
                detail={"capability": cap})
        if self._executor is None:
            raise TemplateError(
                "NOT_WIRED",
                "矩阵标为可得，但未注入连接器运行时执行器：模板层不得自建出网通道",
                detail={"capability": cap})
        raw = self._executor(self._platform, flow,
                             {"store": self._store, "since": since})
        return [self._normalize(flow, r) for r in raw]

    def _normalize(self, flow: str, r: dict[str, Any]) -> dict[str, Any]:
        """动作返回 → recon 记录形状（与 shopify 适配器同形）。"""
        raw_id = str(r.get("id") or "")
        stable = hashlib.sha1(
            f"{self._store}|cn:{flow}|{raw_id}".encode()).hexdigest()
        return {
            "stable_id": stable,
            "amount": str(r.get("amount") or "0"),
            "currency": str(r.get("currency") or "CNY"),
            "status": str(r.get("status") or "posted"),
            "occurred_at": r.get("occurred_at"),
            "source_ref": f"cn:{self._platform}:{flow}:{raw_id}",
            "raw": {"platform": self._platform, "flow": flow, "id": raw_id,
                    "link": r.get("link")},
        }

    def read_orders(self, since: str | None = None) -> list[dict[str, Any]]:
        return self.read_flow("orders", since=since)

    def read_settlements(self, since: str | None = None) -> list[dict[str, Any]]:
        return self.read_flow("settlements", since=since)

    def read_inventory(self) -> list[dict[str, Any]]:
        return self.read_flow("inventory")

    def supported_snapshot(self) -> dict[str, Any]:
        return supported_platforms(self._matrix)

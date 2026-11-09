"""演示双轨（change wire-ecommerce-merchant-channels 任务 6.2）。

两条轨迹：
- `shopify`：跨境店（订单/结算/库存，原币 USD）；
- `domestic`：国内店（经商家电商 ERP，CNY，库存腿由 ERP 提供）。

每轨都跑**两遍**，因为这是演示最容易被误读的地方：

1. **按真实矩阵**（现在全 `unmapped`）→ 展示「还没接通时，对账会说缺什么」；
2. **按假设已接通**（`assumed=True`，矩阵里该轨三腿临时标记 available）→ 展示
   「接通后长什么样」。

第 2 遍的矩阵**是演示用的假设**，不是证据：`note` 里明写 `assumed`，且绝不
写回 `capability-matrix.json`。演示脚本必须把这两遍都打出来——只打第 2 遍就是
把「将来」说成「现在」。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .capability import STATUS_AVAILABLE, CapabilityMatrix, load_matrix, matrix_path_for
from .connectors import merchant_erp, shopify
from .recon.crossborder import CrossborderRecon

TRACKS = ("shopify", "domestic")

#: 轨 → (矩阵平台键, 连接器描述)。
TRACK_PLATFORM = {
    "shopify": "shopify",
    "domestic": "merchant_erp",
}

FLOWS = ("orders", "settlements", "inventory")


def load_domestic_fixture(template_dir: str | Path,
                          variant: str | None = None) -> dict[str, Any]:
    """读国内店 fixture；`variant="settlements_missing"` 时按变体丢腿。"""
    path = Path(template_dir) / "fixtures" / "domestic-store.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    drop: list[str] = []
    if variant:
        spec = (body.get("variants") or {}).get(variant)
        if spec is None:
            raise KeyError(f"fixture 没有变体 {variant}（有：{sorted(body.get('variants') or {})}）")
        drop = list(spec.get("drop") or [])
    out = {"store": body["store"], "platform": body["platform"],
           "variant": variant, "dropped": drop}
    for flow in FLOWS:
        out[flow] = [] if flow in drop else list(body.get(flow) or [])
    return out


def assumed_matrix(matrix: CapabilityMatrix, platform: str, *,
                   skip: tuple[str, ...] = ()) -> CapabilityMatrix:
    """演示用：把该平台三腿临时视为可得（**不落盘**，只在本进程内）。

    `skip` 里的腿保持原状——变体声明「该通道没有这个面」时，演示也不许把它补上。
    """
    for flow in FLOWS:
        if flow in skip:
            continue
        if (platform, flow) in matrix.cells:
            matrix.cells[(platform, flow)] = replace(
                matrix.cells[(platform, flow)],
                status=STATUS_AVAILABLE,
                reason="演示假设：已接通（非矩阵事实）",
                downgraded_from=None,
            )
    return matrix


def _readers(track: str, store: str, records: dict[str, list[dict]]):
    """按轨造读取器（executor 注入 = 不出网，演示用）。"""
    if track == "shopify":
        reader = shopify.ShopifyStoreReader(
            store, executor=lambda action, payload=None, **kw: records[action])
        return (reader.read_orders, reader.read_payments, reader.read_inventory)
    reader = merchant_erp.MerchantErpReader(
        store, executor=lambda action, payload=None, **kw: records[action])
    return (reader.read_orders, reader.read_settlements, reader.read_inventory)


SHOPIFY_ACTIONS = {"orders": "list_orders", "settlements": "list_payouts",
                   "inventory": "list_inventory_items"}
DOMESTIC_ACTIONS = {"orders": "list_orders", "settlements": "list_settlements",
                    "inventory": "list_inventory"}


def run_track(track: str, *, template_dir: str | Path,
              records: dict[str, list[dict]] | None = None,
              variant: str | None = None,
              assume_connected: bool = True) -> dict[str, Any]:
    """跑一轨对账：先按真实矩阵，再（可选）按假设已接通。

    `records` 给定时优先用（演示脚本传自己的合成数据）；否则读国内 fixture。
    """
    if track not in TRACKS:
        raise ValueError(f"未知轨迹 {track}（合法：{', '.join(TRACKS)}）")
    template_dir = Path(template_dir)
    platform = TRACK_PLATFORM[track]

    if records is None:
        if track == "domestic":
            fixture = load_domestic_fixture(template_dir, variant)
            records = {f: fixture[f] for f in FLOWS}
            store = fixture["store"]
            skip = tuple(fixture["dropped"])
        else:
            raise ValueError("shopify 轨迹需要调用方提供 records（演示合成数据在本仓外）")
    else:
        store = records.pop("store", None) or ("demo-shop" if track == "shopify"
                                               else "demo-cn-shop")
        skip = tuple(f for f in FLOWS if f not in records)

    actions = SHOPIFY_ACTIONS if track == "shopify" else DOMESTIC_ACTIONS
    table = {actions[f]: records.get(f, []) for f in FLOWS}
    read_orders, read_settlements, read_inventory = _readers(track, store, table)

    real = load_matrix(matrix_path_for(template_dir))
    honest = CrossborderRecon(store, matrix=real, platform=platform)
    honest_out = honest.reconcile(orders=read_orders(),
                                  payments=read_settlements(),
                                  inventory=read_inventory())

    result: dict[str, Any] = {
        "track": track,
        "platform": platform,
        "store": store,
        "variant": variant,
        "matrix_summary": real.summary(),
        "matrix_fingerprint": real.fingerprint(),
        "honest": {"shape": honest_out["shape"],
                   "diff_count": honest_out["diff_count"],
                   "capability_missing": honest_out["capability_missing"]},
    }
    if assume_connected:
        # 重新加载一份再改，避免污染上面的指纹与 honest 结果。
        assumed = assumed_matrix(load_matrix(matrix_path_for(template_dir)),
                                 platform, skip=skip)
        recon = CrossborderRecon(store, matrix=assumed, platform=platform)
        out = recon.reconcile(orders=read_orders(),
                              payments=read_settlements(),
                              inventory=read_inventory())
        result["assumed"] = {
            "shape": out["shape"],
            "diff_count": out["diff_count"],
            "capability_missing": out["capability_missing"],
            "note": "演示假设：三腿已接通（非矩阵事实，不写回矩阵）",
            "diff_kinds": sorted({d.get("kind") for d in out["diffs"]}),
            "skipped_legs": list(skip),
        }
    return result

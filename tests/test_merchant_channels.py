"""新通道适配器：TikTok Shop 与国内电商 ERP（A 任务 3.2 / 4.2）。

判据不是「能读」，而是：
- 未接时措辞对（授权/审核/运行时缺失各说各的）；
- 接了之后三腿归一化同形，recon 能直接吃；
- 国内轨迹能跑三方（含库存腿），这才是国内对账成立的条件。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability import load_matrix
from fd_erp_vertical_templates.connectors import merchant_erp, tiktok_shop
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.recon.crossborder import CrossborderRecon

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "crossborder"

ORDERS = [{"id": "o1", "amount": "100", "currency": "CNY",
           "status": "paid", "occurred_at": "2026-11-06T10:00:00",
           "link": "o1"}]
SETTLEMENTS = [{"id": "s1", "amount": "98", "currency": "CNY",
                "status": "settled", "occurred_at": "2026-11-06T12:00:00",
                "link": "o1", "fees": {"commission": "2"}}]
INVENTORY = [{"id": "i1", "amount": "0", "occurred_at": "2026-11-06T10:05:00",
              "link": "o1", "status": "deducted"}]


def _executor(table):
    def run(action, payload):
        return table[action]
    return run


# ---- 未接时措辞 ----

def test_tiktok_without_runtime_names_the_real_blocker(monkeypatch):
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    with pytest.raises(TemplateError) as ei:
        tiktok_shop.TikTokShopReader("s1").read_orders()
    assert ei.value.code == "NOT_WIRED"
    assert "授权" in ei.value.message and "审核" in ei.value.message


def test_merchant_erp_without_runtime_honest(monkeypatch):
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    with pytest.raises(TemplateError) as ei:
        merchant_erp.MerchantErpReader("s1").read_inventory()
    assert ei.value.code == "NOT_WIRED"
    assert "运行时" in ei.value.message


def test_merchant_erp_provider_name_is_configurable(monkeypatch):
    monkeypatch.setenv("ERP_MERCHANT_ERP_PROVIDER", "wangdiantong")
    reader = merchant_erp.MerchantErpReader("s1",
                                            executor=_executor({"list_inventory": []}))
    assert reader.describe()["provider"] == "wangdiantong"


# ---- 接通后：三腿同形 + 国内三方对账成立 ----

def test_merchant_erp_three_legs_normalized():
    reader = merchant_erp.MerchantErpReader("s1", executor=_executor({
        "list_orders": ORDERS, "list_inventory": INVENTORY,
        "list_settlements": SETTLEMENTS}))
    orders = reader.read_orders()
    inv = reader.read_inventory()
    stl = reader.read_settlements()
    for row in orders + inv + stl:
        assert set(row) == {"stable_id", "amount", "currency", "status",
                            "occurred_at", "source_ref", "raw"}
    assert inv[0]["currency"] == "EA"          # 计数流不是货币
    assert stl[0]["raw"]["fees"]["commission"] == "2"


def test_domestic_three_way_recon_works_with_merchant_erp():
    """国内三方对账能跑——前提正是库存腿由电商 ERP 提供。"""
    from fd_erp_vertical_templates.capability import load_matrix
    body = {"schema": "ecommerce-capability-matrix/v1", "vertical": "crossborder",
            "platforms": [{"key": "merchant_erp", "flows": {
                f: {"status": "available", "channel": "merchant_erp." + a,
                    "reason": "", "measured_at": "2026-11-09",
                    "evidence": [{"kind": "provider_action", "ref": "p"},
                                 {"kind": "runtime_read", "ref": "r"}]}
                for f, a in (("orders", "list_orders"),
                             ("settlements", "list_settlements"),
                             ("inventory", "list_inventory"))}}]}
    m = load_matrix(body)
    reader = merchant_erp.MerchantErpReader("s1", executor=_executor({
        "list_orders": ORDERS, "list_inventory": INVENTORY,
        "list_settlements": SETTLEMENTS}))
    recon = CrossborderRecon("s1", matrix=m, platform="merchant_erp")
    out = recon.reconcile(orders=reader.read_orders(),
                          payments=reader.read_settlements(),
                          inventory=reader.read_inventory())
    assert out["shape"] == 3
    assert out["capability_missing"] == []
    assert out["diff_count"] >= 0


def test_tiktok_three_legs_normalized():
    reader = tiktok_shop.TikTokShopReader("s1", executor=_executor({
        "list_orders": ORDERS, "list_settlements": SETTLEMENTS,
        "list_inventory": INVENTORY}))
    assert reader.read_orders()[0]["source_ref"].startswith("tiktok:")
    assert reader.read_settlements()[0]["currency"] == "CNY"
    assert reader.read_inventory()[0]["currency"] == "EA"


# ---- 矩阵不因「有适配器」而自动升格 ----

def test_adapters_existing_does_not_upgrade_matrix():
    """只有适配器、没有真实读取记录 → 矩阵仍是 unmapped（证据两半缺一不算）。"""
    m = load_matrix(TEMPLATE / "capability-matrix.json")
    for platform in ("tiktok_shop", "merchant_erp"):
        for flow in ("orders", "settlements", "inventory"):
            assert m.status(platform, flow) == "unmapped"

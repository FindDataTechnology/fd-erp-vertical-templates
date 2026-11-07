"""连接器层（任务 2.1/2.2/3.2/3.3 + 5.1）：BYOK 注入验证、如实守界、只读面。"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.connectors import shopify, cn_commerce, pos, payments
from fd_erp_vertical_templates.errors import TemplateError


# ---- 2.1 Shopify：BYOK 注入 + 零落库 ----

def test_shopify_without_token_honest(monkeypatch):
    monkeypatch.delenv("ERP_BYOK_SHOPIFY_TOKEN", raising=False)
    reader = shopify.ShopifyStoreReader("test-store", "test.example.myshopify.com")
    with pytest.raises(TemplateError) as ei:
        reader.read_orders()
    assert ei.value.code == "NOT_WIRED"
    assert "ERP_BYOK_SHOPIFY_TOKEN" in ei.value.message


def test_shopify_stable_id_deterministic():
    a = shopify._stable_id("s1", "order", "1001")
    b = shopify._stable_id("s1", "order", "1001")
    c = shopify._stable_id("s1", "payout", "1001")
    assert a == b and a != c  # 同键重推同 id（幂等），跨域不同


def test_shopify_describe_readonly():
    d = shopify.ShopifyStoreReader("s", "x.example").describe()
    assert d["writes_external"] is False
    assert d["adopted_from"].startswith("a1-x-tech/mcp-shopify-admin")


def test_shopify_token_never_in_error(monkeypatch):
    """token 零落日志/异常：网络异常细节不带凭据。"""
    monkeypatch.setenv("ERP_BYOK_SHOPIFY_TOKEN", "shpat_supersecret")
    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom shpat_supersecret")))
    reader = shopify.ShopifyStoreReader("s", "x.example")
    with pytest.raises(TemplateError) as ei:
        reader.read_orders()
    assert "shpat_supersecret" not in ei.value.message
    assert "shpat_supersecret" not in str(ei.value)


# ---- 2.2 国内平台：实测支持面 + 拼多多如实不提供 ----

def test_cn_platform_support_surface_honest():
    surface = cn_commerce.supported_platforms()
    assert surface["pinduoduo"]["flows"] == []
    assert "schema 缺失" in surface["pinduoduo"]["note"]
    assert surface["taobao"]["flows"] == ["orders", "payments"]


def test_pinduoduo_honestly_unsupported():
    with pytest.raises(TemplateError) as ei:
        cn_commerce.CnCommerceReader("store-1", "pinduoduo")
    assert ei.value.code == "NOT_WIRED"
    assert "不提供" in ei.value.message


def test_unknown_platform_unsupported():
    with pytest.raises(TemplateError) as ei:
        cn_commerce.CnCommerceReader("store-1", "etsy")
    assert ei.value.code == "NOT_WIRED"


def test_cn_reader_without_credentials_honest(monkeypatch):
    monkeypatch.delenv("ERP_BYOK_CN_COMMERCE_TOKEN", raising=False)
    reader = cn_commerce.CnCommerceReader("store-1", "taobao")
    with pytest.raises(TemplateError) as ei:
        reader.read_orders()
    assert ei.value.code == "NOT_WIRED"


# ---- 3.2 POS：框架插拔 + 未适配如实 + 导出文件通道 ----

def test_unregistered_vendor_honest_with_fallback():
    with pytest.raises(TemplateError) as ei:
        pos.get_pos_adapter("meituan-pos", store_id="st-1")
    assert ei.value.code == "NOT_WIRED"
    assert "导出文件" in ei.value.message  # 替代路径如实说明
    assert "不" in ei.value.message


def test_synthetic_pos_requires_byok(monkeypatch):
    monkeypatch.delenv("ERP_BYOK_POS_SYNTHETIC", raising=False)
    with pytest.raises(TemplateError) as ei:
        pos.get_pos_adapter("synthetic", store_id="st-1")
    assert "授权未开通" in ei.value.message


def test_synthetic_pos_reads_day_flows(monkeypatch):
    monkeypatch.setenv("ERP_BYOK_POS_SYNTHETIC", "1")
    flows = [{"id": "p1", "day": "2026-11-06", "amount": "120.5",
              "channel": "wechat-pay"},
             {"id": "p2", "day": "2026-11-05", "amount": "80"}]
    adapter = pos.get_pos_adapter("synthetic", store_id="st-1",
                                  synthetic_flows=flows)
    rows = adapter.read_day_flows("st-1", "2026-11-06")
    assert len(rows) == 1  # 只含当日流水
    assert rows[0]["amount"] == "120.5"
    assert rows[0]["source_ref"].startswith("pos:synthetic:st-1:p1")
    # 幂等键确定性：同店同厂商同 id 重读同 stable_id
    assert adapter.read_day_flows("st-1", "2026-11-06")[0]["stable_id"] == \
        rows[0]["stable_id"]


def test_export_file_channel(monkeypatch):
    csv_text = "id,day,amount,currency,channel\nx1,2026-11-06,100.00,CNY,wechat-pay\n"
    adapter = pos.get_pos_adapter("export-file", store_id="st-1", export_csv=csv_text)
    rows = adapter.read_day_flows("st-1", "2026-11-06")
    assert len(rows) == 1 and rows[0]["amount"] == "100.00"
    # 缺 id（幂等键）→ 拒绝（导出通道要求可对账）
    bad = "day,amount\n2026-11-06,50\n"
    adapter2 = pos.get_pos_adapter("export-file", store_id="st-1", export_csv=bad)
    with pytest.raises(TemplateError) as ei:
        adapter2.read_day_flows("st-1", "2026-11-06")
    assert "缺流水 id" in ei.value.message


def test_vendor_surface_honest():
    surface = pos.vendor_support_surface()
    assert surface["production_vendors"] == []  # 首单客户驱动，如实为零
    assert "导出" in surface["fallback"]


# ---- 3.3 收款渠道：实测可用面为准，未实测不宣称 live ----

def test_wechat_without_credentials_honest(monkeypatch):
    monkeypatch.delenv("ERP_BYOK_PAY_WECHAT_PAY", raising=False)
    adapter = payments.get_payment_channel("wechat-pay")
    with pytest.raises(TemplateError) as ei:
        adapter.read_settlements("st-1", "2026-11-06")
    assert ei.value.code == "NOT_WIRED"


def test_channel_surface_live_empty():
    surface = payments.channel_support_surface()
    assert surface["live_surfaces"] == {}  # 未实测不宣称
    assert "实测" in surface["note"]


def test_synthetic_channel_reads(monkeypatch):
    monkeypatch.setenv("ERP_BYOK_PAY_SYNTHETIC", "1")
    settlements = [{"id": "s1", "day": "2026-11-06", "amount": "118",
                    "fee": "2.5", "channel": "synthetic"}]
    adapter = payments.get_payment_channel("synthetic",
                                           synthetic_settlements=settlements)
    rows = adapter.read_settlements("st-1", "2026-11-06")
    assert len(rows) == 1 and rows[0]["raw"]["fee"] == "2.5"


def test_unknown_channel_honest():
    with pytest.raises(TemplateError):
        payments.get_payment_channel("stripe")

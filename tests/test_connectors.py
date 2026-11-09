"""连接器层（任务 2.1/2.2/3.2/3.3 + 5.1）：BYOK 注入验证、如实守界、只读面。"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.connectors import shopify, cn_commerce, pos, payments
from fd_erp_vertical_templates.errors import TemplateError


# ---- 2.1 Shopify：运行时单口 + 版本探测 + 零凭据 ----

def test_shopify_without_runtime_honest(monkeypatch):
    """没有运行时地址 = 未接；提示里说明凭证归运行时，模板层不读。"""
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    reader = shopify.ShopifyStoreReader("test-store", "test.example.myshopify.com")
    with pytest.raises(TemplateError) as ei:
        reader.read_orders()
    assert ei.value.code == "NOT_WIRED"
    assert "运行时" in ei.value.message


def test_shopify_stable_id_deterministic():
    from fd_erp_vertical_templates.runtime_client import stable_id
    a = stable_id("s1", "order", "1001")
    b = stable_id("s1", "order", "1001")
    c = stable_id("s1", "payout", "1001")
    assert a == b and a != c  # 同键重推同 id（幂等），跨域不同


def test_shopify_describe_points_at_runtime_provider():
    d = shopify.ShopifyStoreReader("s", "x.example").describe()
    assert d["writes_external"] is False
    assert d["transport"] == "connector-runtime"
    assert d["provider"] == "shopify_admin"
    assert "list_payouts" in d["actions"]


def test_shopify_version_probe_gives_actionable_error():
    """版本过期 → 显式失败并给配置项
 
    判据是「错误里有没有可执行的下一步」，不是「有没有抛异常」。
    """
    def executor(action, payload):
        raise TemplateError("BAD_RESPONSE",
                            "unsupported api version: 2024-10",
                            detail={"status": 400})
    reader = shopify.ShopifyStoreReader("s", "x.example", executor=executor)
    with pytest.raises(TemplateError) as ei:
        reader.read_orders()
    assert "SHOPIFY_ADMIN_API_VERSION" in ei.value.message
    assert "不要回退到自建直连" in ei.value.message


def test_shopify_executor_path_never_leaks_token(monkeypatch):
    monkeypatch.setenv("ERP_BYOK_SHOPIFY_TOKEN", "shpat_supersecret")

    def executor(action, payload):
        raise TemplateError("RUNTIME_UNREACHABLE", "boom（细节零凭据）")

    reader = shopify.ShopifyStoreReader("s", "x.example", executor=executor)
    with pytest.raises(TemplateError) as ei:
        reader.read_orders()
    assert "shpat_supersecret" not in ei.value.message
    assert "shpat_supersecret" not in str(ei.value)


# ---- 2.2 国内平台：矩阵驱动，未接即如实未接 ----

def _matrix():
    from pathlib import Path

    from fd_erp_vertical_templates.capability import load_matrix
    path = (Path(__file__).resolve().parent.parent
            / "templates" / "crossborder" / "capability-matrix.json")
    return load_matrix(path)


def test_cn_platform_support_surface_comes_from_matrix():
    surface = cn_commerce.supported_platforms(_matrix())
    # 拼多多：确认拿不到 —— unavailable，且带原因
    pdd = surface["pinduoduo"]["flows"]["orders"]
    assert pdd["status"] == "unavailable"
    assert "schema" in pdd["reason"]
    # 淘宝：可接但未接 —— unmapped（与「不提供」是两种话）
    tb = surface["taobao"]["flows"]["orders"]
    assert tb["status"] == "unmapped"
    assert surface["taobao"]["readable"] == []


def test_cn_support_surface_without_matrix_claims_nothing():
    """没有矩阵就不宣称任何平台——手写支持表正是漂移的来源。"""
    assert cn_commerce.supported_platforms(None) == {}


def test_pinduoduo_honestly_unsupported():
    with pytest.raises(TemplateError) as ei:
        cn_commerce.CnCommerceReader("store-1", "pinduoduo",
                                     matrix=_matrix()).read_orders()
    assert ei.value.code == "NOT_WIRED"
    assert "不提供" in ei.value.message
    assert ei.value.detail["capability"]["status"] == "unavailable"


def test_unwired_platform_says_need_onboarding_not_unsupported():
    """淘宝是「需一次接入」，措辞必须与拼多多的「不提供」区分开。"""
    with pytest.raises(TemplateError) as ei:
        cn_commerce.CnCommerceReader("store-1", "taobao",
                                     matrix=_matrix()).read_orders()
    assert "尚未接入" in ei.value.message
    assert "不提供" not in ei.value.message
    assert ei.value.detail["capability"]["status"] == "unmapped"


def test_unknown_platform_reports_unregistered():
    with pytest.raises(TemplateError) as ei:
        cn_commerce.CnCommerceReader("store-1", "etsy",
                                     matrix=_matrix()).read_orders()
    assert ei.value.code == "NOT_WIRED"
    assert ei.value.detail["capability"]["status"] == "unmapped"


def test_available_without_executor_still_refuses_to_go_direct():
    """矩阵说可得但没注入执行器 → 仍拒；绝不自建出网通道。"""
    from fd_erp_vertical_templates.capability import load_matrix
    body = {
        "schema": "ecommerce-capability-matrix/v1",
        "vertical": "crossborder",
        "platforms": [{"key": "taobao", "flows": {"orders": {
            "status": "available", "channel": "taobao.list_orders",
            "reason": "",
            "measured_at": "2026-11-09",
            "evidence": [{"kind": "provider_action", "ref": "a"},
                         {"kind": "runtime_read", "ref": "b"}],
        }}}],
    }
    m = load_matrix(body)
    reader = cn_commerce.CnCommerceReader("s", "taobao", matrix=m)
    with pytest.raises(TemplateError) as ei:
        reader.read_orders()
    assert "不得自建出网通道" in ei.value.message


def test_cn_commerce_module_has_no_outbound_path():
    """静态证明：模块里没有 http 客户端、没有 base URL、没有从环境变量取端点。"""
    import ast
    from pathlib import Path

    src = Path(cn_commerce.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    urls = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.startswith(("http://", "https://"))]
    assert urls == [], f"模块内不得出现硬地址：{urls}"
    # 不得 import / 调用任何 http 客户端（只看调用点，不看 dict.get 这类同名方法）
    http_clients = {"httpx", "requests", "urllib", "aiohttp", "urllib3"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not ({a.name.split(".")[0] for a in node.names} & http_clients)
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in http_clients
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name):
                assert base.id not in http_clients, \
                    f"不得调用 {base.id}.{node.func.attr}"
    # 不得从环境变量取端点（历史上就是这么接的）
    env_reads = [n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and "ENDPOINT" in str(n.value).upper()]
    assert env_reads == [], f"不得从环境变量取端点：{env_reads}"


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

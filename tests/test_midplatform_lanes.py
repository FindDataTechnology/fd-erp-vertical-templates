"""中台文件服务与端内消息通道测试（change establish-ai-native-ecommerce-stack 3.2）。

判据：
1. 未接时 `NOT_WIRED` 如实失败（自建直连不存在）；
2. 接线形态 = 经运行时执行 `file-service.*` / `inapp-notify.send_broadcast`
   （动作名与载荷形状对齐上游服务的真实契约）；
3. 推送留痕不复制第二份：send_broadcast 原样带回网关广播回执。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.connectors import midplatform
from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.runtime_client import RuntimeClient


def _client_with(payload_by_action: dict[str, object]) -> RuntimeClient:
    seen: list[tuple[str, dict]] = []

    def transport(method, url, headers, body):
        path = url.rsplit("/v1/actions/", 1)[-1]
        seen.append((path, body))
        return 200, payload_by_action.get(path.split(".")[-1])

    client = RuntimeClient(base_url="http://runtime.test", transport=transport)
    client.seen = seen  # type: ignore[attr-defined]
    return client


# ---- 未接时如实 ----

def test_file_service_without_runtime_is_not_wired(monkeypatch):
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    lane = midplatform.FileServiceUpload()
    with pytest.raises(TemplateError) as ei:
        lane.create_upload_url(owner="erp:t:sp-invoice", filename="f.pdf",
                               mime="application/pdf", size=10)
    assert ei.value.code == "NOT_WIRED"
    assert "自建直连" in ei.value.message


def test_inapp_notify_without_runtime_is_not_wired(monkeypatch):
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    with pytest.raises(TemplateError) as ei:
        midplatform.InappNotify().send_broadcast(
            audience={"kind": "all"}, title="t", body="b")
    assert ei.value.code == "NOT_WIRED"


# ---- 接线形态 ----

def test_file_service_chain_matches_upstream_contract():
    client = _client_with({
        "create_upload_url": {"data": {"assets": [
            {"id": "F1", "upload_url": "http://s3/x", "method": "PUT",
             "dedup": False}]}},
        "confirm_upload": {"data": {"assets": [
            {"id": "F1", "status": "ready", "size": 10}]}},
        "list_files": {"data": {"files": [
            {"id": "F1", "filename": "a.pdf", "mime": "application/pdf",
             "size": 10, "created_at": "2026-11-26T00:00:00"}]}},
    })
    lane = midplatform.FileServiceUpload(client=client)
    grant = lane.create_upload_url(owner="erp:t:sp-invoice", filename="a.pdf",
                                   mime="application/pdf", size=10, sha256="ab")
    assert grant["id"] == "F1" and grant["upload_url"] == "http://s3/x"
    asset = lane.confirm_upload(owner="erp:t:sp-invoice", file_id="F1")
    assert asset["status"] == "ready"
    rows = lane.list_files(owner="erp:t:sp-invoice", limit=50)
    assert rows[0]["id"] == "F1"
    (p1, b1), (p2, b2), (p3, b3) = client.seen  # type: ignore[attr-defined]
    assert p1 == "file-service.create_upload_url"
    assert b1["owner"] == "erp:t:sp-invoice" and b1["sha256"] == "ab"
    assert b1["size"] == 10 and b1["filename"] == "a.pdf"
    assert p2 == "file-service.confirm_upload" and b2["file_id"] == "F1"
    assert p3 == "file-service.list_files" and b3["owner"] == "erp:t:sp-invoice"


def test_send_broadcast_passes_audience_and_returns_gateway_record():
    """推送留痕=网关广播回执原样带回（不复制第二份账本）。"""
    client = _client_with({"send_broadcast": {"data": {"broadcasts": [{
        "id": "BC-1", "seq": 42, "audience": {"kind": "org", "id": "org-9"},
        "audienceLabel": "组织 org-9", "hitCount": 3, "okCount": 0,
        "failCount": 0, "wake": "none"}]}}})
    out = midplatform.InappNotify(client=client).send_broadcast(
        audience={"kind": "org", "id": "org-9"},
        title="对账单 ST-1 待确认", body="期间 2026-10 金额 250 CNY")
    assert out["broadcast_id"] == "BC-1" and out["hit_count"] == 3
    (path, body), = client.seen  # type: ignore[attr-defined]
    assert path == "inapp-notify.send_broadcast"
    assert body["audience"] == {"kind": "org", "id": "org-9"}
    assert body["title"].startswith("对账单") and body["wake"] == "none"


def test_describe_honestly_declares_no_live():
    for lane in (midplatform.FileServiceUpload(), midplatform.InappNotify()):
        assert lane.describe()["live_declared"] is False

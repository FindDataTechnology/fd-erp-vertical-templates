"""中台文件服务与端内消息通道（change establish-ai-native-ecommerce-stack 3.2）。

两条 501 的解除车道：

- **file-service**（provider `file-service`）：供应商发票影像件落中台文件服务
  （预签名直传：create_upload_url → 客户端/调用方 PUT → confirm_upload），
  读面走 list_files。钱路无关，但同守出网单口（RuntimeClient）。
- **inapp-notify**（provider `inapp-notify`）：对账单推送走底座端内消息通道
  （POST /api/broadcast，Bearer 为管理组账号的 app/MP 令牌；网关侧 ledger 即
  「推送留痕 + 送达回执」的唯一真源，本层不复制第二份）。

纪律（同 payments/checkout 车道）：
- 出网唯一口 = `RuntimeClient`；未配置运行时 / 未授权连接 = `NOT_WIRED` 如实失败，
  **不回落任何自建直连**；
- live 面以实测为准（未实测不宣称）。
"""
from __future__ import annotations

from typing import Any

from ..errors import TemplateError
from ..runtime_client import ActionRef, RuntimeClient


class _RuntimeLane:
    """两条车道共用的运行时骨架：动作名 → `provider.action` 精确引用。"""

    provider = ""

    def __init__(self, client: RuntimeClient | None = None,
                 connection: str | None = None):
        self._client = client or RuntimeClient()
        self._connection = connection or f"{self.provider}-default"

    def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._client.configured:
            raise TemplateError(
                "NOT_WIRED",
                f"{self.provider} 未接线：连接器运行时地址缺失（{action}）——不提供"
                "任何自建直连",
                detail={"provider": self.provider, "action": action,
                        "connection": self._connection})
        return self._client.call(
            ActionRef(self.provider, action, self._connection), payload)


def _first(out: dict[str, Any], label: str) -> dict[str, Any]:
    """单对象动作的输出 = 运行时回执里的首条记录（provider 侧以集合封套回传）。"""
    records = out.get("records") or []
    if not records:
        raise TemplateError(
            "BAD_RESPONSE", f"{label} 返回里没有记录：无法继续",
            detail={"action": label})
    return records[0]


class FileServiceUpload(_RuntimeLane):
    """供应商发票影像件：预签名直传 + 确认 + 下载 URL + 按 owner 列表。"""

    provider = "file-service"

    def create_upload_url(self, *, owner: str, filename: str, mime: str,
                          size: int, sha256: str | None = None) -> dict[str, Any]:
        payload = {"owner": owner, "filename": filename, "mime": mime,
                   "size": int(size)}
        if sha256:
            payload["sha256"] = sha256
        return _first(self._call("create_upload_url", payload),
                      "file-service.create_upload_url")

    def confirm_upload(self, *, owner: str, file_id: str) -> dict[str, Any]:
        return _first(self._call("confirm_upload",
                                 {"owner": owner, "file_id": file_id}),
                      "file-service.confirm_upload")

    def get_download_url(self, *, owner: str, file_id: str) -> dict[str, Any]:
        return _first(self._call("get_download_url",
                                 {"owner": owner, "file_id": file_id}),
                      "file-service.get_download_url")

    def list_files(self, *, owner: str, mime_prefix: str | None = None,
                   limit: int = 50) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"owner": owner, "limit": int(limit)}
        if mime_prefix:
            payload["mime_prefix"] = mime_prefix
        return list(self._call("list_files", payload).get("records") or [])

    def describe(self) -> dict[str, Any]:
        return {"adapter": "file-service", "provider": self.provider,
                "transport": "connector-runtime",
                "actions": ["create_upload_url", "confirm_upload",
                            "get_download_url", "list_files"],
                "live_declared": False,
                "note": "live 面以实测为准（未实测不宣称）"}


class InappNotify(_RuntimeLane):
    """底座端内消息通道：广播一条通知给指定受众（推送留痕在网关 ledger）。"""

    provider = "inapp-notify"

    def send_broadcast(self, *, audience: dict[str, Any], title: str, body: str,
                       wake: str = "none",
                       action: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一条端内通知。audience 形如 {kind: org|role|userIds|all, ...}。

        留痕不在这里复制：网关 POST /api/broadcast 的记录（id/seq/受众/命中数）
        就是唯一真源；本方法把它的回执原样带回调用方。
        """
        payload: dict[str, Any] = {"audience": audience, "title": title,
                                   "body": body, "wake": wake}
        if action is not None:
            payload["action"] = action
        record = _first(self._call("send_broadcast", payload),
                        "inapp-notify.send_broadcast")
        return {"broadcast": record,
                "broadcast_id": record.get("id"),
                "seq": record.get("seq"),
                "hit_count": record.get("hitCount"),
                "im": record.get("im")}

    def describe(self) -> dict[str, Any]:
        return {"adapter": "inapp-notify", "provider": self.provider,
                "transport": "connector-runtime", "actions": ["send_broadcast"],
                "live_declared": False,
                "note": "live 面以实测为准（未实测不宣称）"}

"""连接器运行时客户端（change `wire-ecommerce-merchant-channels` 任务 1.1-1.3）。

**模板层唯一的出网口。** 所有商家数据读取都经 `POST /v1/actions/<provider>.<action>`
（带 connection alias）执行，模板层不拼 vendor URL、不持签名逻辑、不读凭证。

为什么这样划（ADR 0002 的三条理由，都是这条设计要躲的坑）：

1. 目标由代码字面量决定 → 一旦适配器能拼 URL，上一轮撤下的直连会换个形式回来。
2. 目录是策展目录而非协议适配器 → 只有经 provider 归因，「接通了」才可审计、可写进 SLA。
3. 凭据 BYOK 零落库 → 凭证只在运行时侧，模板层连读都不读。

错误码按「下一步该做什么」划分，不按 HTTP 状态堆砌（见 `TemplateError.CODES`）：
`RUNTIME_UNREACHABLE` 可退避、`ACTION_NOT_FOUND` 必须记缺口、`AUTH_FAILED` 去重授权。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Callable

from .errors import TemplateError

#: 默认运行时地址的环境变量名（部署面配置，非凭证）。
DEFAULT_BASE_URL_ENV = "ERP_CONNECTOR_BASE_URL"
#: 默认超时（秒）。商家后台普遍慢，但超过这个数说明链路有问题。
DEFAULT_TIMEOUT = 15.0

#: transport 签名：(method, url, headers, json_body) -> (status, body)
Transport = Callable[[str, str, dict[str, str], dict[str, Any]], "tuple[int, Any]"]


@dataclass(frozen=True)
class ActionRef:
    """一个动作引用：provider + action + 连接别名（客户在连接器里授权的那个）。"""

    provider: str
    action: str
    connection: str

    def __post_init__(self) -> None:
        for field_name in ("provider", "action", "connection"):
            if not str(getattr(self, field_name) or "").strip():
                raise TemplateError("INVALID_ARGUMENT",
                                    f"ActionRef.{field_name} 不可为空")
        if any(ch in self.provider + self.action for ch in "/: "):
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"provider/action 只能是标识符，不得含路径或协议字符："
                f"{self.provider}.{self.action}")

    @property
    def path(self) -> str:
        return f"/v1/actions/{self.provider}.{self.action}"

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "action": self.action,
                "connection": self.connection}


class RuntimeClient:
    """经连接器运行时执行动作。

    `transport` 可注入（测试用）。默认 transport 用 httpx；httpx 不在时**不**
    退回 urllib 直连——那等于绕过运行时。缺失即 `RUNTIME_UNREACHABLE` 并说明。
    """

    def __init__(self, base_url: str | None = None, *, timeout: float = DEFAULT_TIMEOUT,
                 transport: Transport | None = None,
                 base_url_env: str = DEFAULT_BASE_URL_ENV):
        self._base_url = (base_url or os.environ.get(base_url_env, "")).rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._base_url_env = base_url_env

    # ---- 目标 ----

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def _url(self, ref: ActionRef) -> str:
        if not self._base_url:
            raise TemplateError(
                "NOT_WIRED",
                f"连接器运行时未配置：{self._base_url_env}（部署面配置，非凭证）",
                detail={"env": self._base_url_env})
        return f"{self._base_url}{ref.path}"

    # ---- 执行 ----

    def call(self, ref: ActionRef, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行一个动作，返回归一化后的 `{"records": [...], "raw": {...}}`。

        错误只带 provider/action/状态码/归因——不回显 header、token、签名、
        完整查询串（错误是最容易漏凭据的地方）。
        """
        url = self._url(ref)
        body = dict(payload or {})
        body.setdefault("connection", ref.connection)
        headers = {"Content-Type": "application/json"}
        try:
            status, payload_out = self._send("POST", url, headers, body)
        except TemplateError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层任何异常都归为不可达
            raise TemplateError(
                "RUNTIME_UNREACHABLE",
                f"连接器运行时不可达（{ref.provider}.{ref.action}，细节零凭据）",
                detail={"provider": ref.provider, "action": ref.action}) from exc

        self._raise_for_status(status, ref, payload_out)
        records = _extract_records(payload_out, ref)
        data = payload_out.get("data") if isinstance(payload_out, dict) else None
        return {"records": records, "data": data, "raw": payload_out,
                "action": ref.as_dict()}

    def call_all(self, ref: ActionRef, payload: dict[str, Any] | None = None, *,
                 page_size: int = 100, max_pages: int = 50) -> dict[str, Any]:
        """按 `pageInfo` 翻页拉全量，并按 `stable_id` 去重。

        为什么要翻页而不是只取第一页：单页读取**不会报错**，它只是少给数据——
        对账看到的「缺失」会变成平台的责任，实际是我们没读全。这是最贵的一类错。

        去重键用 `stable_id`（`store|domain|id` 的 sha1）：同窗口重推是常态，
        重复拉取必须幂等；没有 id 的记录由 `normalize_record` 直接判红，不静默丢。
        """
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        cursor: str | None = None
        data_seen: list[dict[str, Any]] = []
        while pages < max_pages:
            body = {**(payload or {}), "first": page_size}
            if cursor:
                body["after"] = cursor
            out = self.call(ref, body)
            pages += 1
            if isinstance(out.get("data"), dict):
                data_seen.append(out["data"])
            for rec in out["records"]:
                key = rec.get("stable_id")
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                merged.append(rec)
            page_info = (out.get("data") or {}).get("pageInfo") \
                if isinstance(out.get("data"), dict) else None
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                return {"records": merged, "pages": pages,
                        "data": _merge_page_data(data_seen),
                        "action": ref.as_dict()}
            next_cursor = page_info.get("endCursor")
            if not next_cursor:
                raise TemplateError(
                    "BAD_RESPONSE",
                    f"{ref.provider}.{ref.action} 声称还有下一页但没有 endCursor："
                    "无法继续翻页，不能当作读完",
                    detail={"provider": ref.provider, "action": ref.action,
                            "pages": pages})
            cursor = str(next_cursor)
        raise TemplateError(
            "BAD_RESPONSE",
            f"{ref.provider}.{ref.action} 翻页超过 {max_pages} 页仍未结束："
            "按未读完处理，不返回部分结果",
            detail={"provider": ref.provider, "action": ref.action,
                    "pages": pages})

    def probe(self, provider: str) -> set[str]:
        """拉某 provider 的动作面（供矩阵「动作存在」半边证据用）。

        取不到就返回空集并**不**静默——调用方据此判定，不据此宣称。
        """
        url = self._url(ActionRef(provider, "list_actions", "-"))
        try:
            status, payload_out = self._send("GET", url.replace(
                "/v1/actions/%s.list_actions" % provider,
                "/v1/providers/%s/actions" % provider), {}, None)
        except Exception:  # noqa: BLE001
            return set()
        if status != 200:
            return set()
        items = payload_out.get("actions") if isinstance(payload_out, dict) else payload_out
        out: set[str] = set()
        for item in items or []:
            if isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict) and item.get("name"):
                out.add(str(item["name"]))
        return out

    # ---- 内部 ----

    def _send(self, method: str, url: str, headers: dict[str, str],
              body: dict[str, Any] | None) -> tuple[int, Any]:
        if self._transport is not None:
            return self._transport(method, url, headers, body or {})
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 依赖缺失走同一归因
            raise TemplateError(
                "RUNTIME_UNREACHABLE",
                "缺少 httpx 依赖，且不得退回自建直连通道") from exc
        kwargs: dict[str, Any] = {"headers": headers, "timeout": self._timeout}
        if body is not None:
            kwargs["json"] = body
        resp = httpx.request(method, url, **kwargs)
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应按原文摘要处理
            parsed = {"_raw_text_len": len(resp.text or "")}
        return resp.status_code, parsed

    def _raise_for_status(self, status: int, ref: ActionRef, body: Any) -> None:
        if 200 <= status < 300:
            return
        detail = {"provider": ref.provider, "action": ref.action, "status": status}
        if status in (401, 403):
            raise TemplateError(
                "AUTH_FAILED",
                f"{ref.provider}.{ref.action} 鉴权失败或权限不足（{status}）："
                "去连接器重新授权，或确认该连接已授予此动作",
                detail=detail)
        if status == 404:
            raise TemplateError(
                "ACTION_NOT_FOUND",
                f"动作不在目录：{ref.provider}.{ref.action}（404）。"
                "这是目录缺口，不得以自建通道补齐",
                detail=detail)
        if status == 429:
            raise TemplateError(
                "RATE_LIMITED",
                f"{ref.provider}.{ref.action} 被上游限流（429）：退避后重试；"
                "报数时归因「配额」而不是「无数据」",
                detail=detail)
        hint = ""
        if isinstance(body, dict):
            msg = str(body.get("message") or body.get("error") or "")[:200]
            if msg:
                hint = f"：{msg}"
        raise TemplateError(
            "BAD_RESPONSE",
            f"{ref.provider}.{ref.action} 返回 {status}{hint}",
            detail=detail)


def _extract_records(payload_out: Any, ref: ActionRef) -> list[dict[str, Any]]:
    """动作返回 → 记录列表。

    运行时 HTTP 封套实测形状（`open-connector-mt/src/server/api/runtime-api.ts:217`）：

        {"success": true, "message": "OK", "data": <动作输出>, "meta": {...}}

    而动作输出里的集合键**每个动作自己起名**（`orders` / `payouts` /
    `inventoryItems` / `records` …），所以这里按「第一个对象数组」取值，
    而不是硬编码键名——硬编码会在加动作时静默返回空列表。
    """
    if isinstance(payload_out, list):
        return [r for r in payload_out if isinstance(r, dict)]
    if not isinstance(payload_out, dict):
        raise TemplateError(
            "BAD_RESPONSE",
            f"{ref.provider}.{ref.action} 返回结构不是对象或数组",
            detail={"provider": ref.provider, "action": ref.action})

    # 先剥一层运行时封套，再在动作输出里找集合。
    candidates: list[Any] = []
    if isinstance(payload_out.get("data"), (dict, list)):
        candidates.append(payload_out["data"])
    candidates.append(payload_out)

    for candidate in candidates:
        if isinstance(candidate, list):
            return [r for r in candidate if isinstance(r, dict)]
        if not isinstance(candidate, dict):
            continue
        # 显式键优先（兼容自定义网关），其次「第一个对象数组」。
        for key in ("records", "items", "results"):
            value = candidate.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        for value in candidate.values():
            if isinstance(value, list) and value and all(
                    isinstance(item, dict) for item in value):
                return list(value)
        # 单记录输出（如 get_order → {"order": {...}}）：取第一个带 id 的对象。
        for value in candidate.values():
            if isinstance(value, dict) and value.get("id") is not None:
                return [value]
        if candidate.get("id") is not None:
            return [candidate]

    raise TemplateError(
        "BAD_RESPONSE",
        f"{ref.provider}.{ref.action} 返回结构里没有记录集合",
        detail={"provider": ref.provider, "action": ref.action,
                "keys": sorted(k for k in payload_out if isinstance(k, str))[:10]})


# ---------------------------------------------------------------- 归一化


def stable_id(store: str, domain: str, raw_id: Any) -> str:
    """幂等键（evidence 门纪律）：同店同域同 id 重推只落一次。"""
    return hashlib.sha1(f"{store}|{domain}|{raw_id}".encode()).hexdigest()


def _merge_page_data(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """多页的 `data` 合并：除 `pageInfo` 外，标量取最后一页（如 payoutsAccountAvailable）。"""
    merged: dict[str, Any] = {}
    for page in pages:
        for key, value in page.items():
            if key == "pageInfo":
                continue
            merged[key] = value
    if pages:
        merged["pages"] = len(pages)
    return merged


def normalize_record(record: dict[str, Any], *, store: str, domain: str,
                     source_ref: str, default_currency: str = "CNY",
                     extra_raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """动作记录 → recon 记录形状（三平台共用一套，避免各适配器自行变形）。

    幂等：已经是 recon 形状（有 `stable_id` 且 `source_ref`）的记录**原样返回**。
    演示/测试里直接喂 recon 记录的调用方不该被再归一化一次——那会把缺 `id`
    的既有记录判成 BAD_RESPONSE。

    字段固定：stable_id / amount / currency / status / occurred_at / source_ref / raw。
    缺 id 就抛 BAD_RESPONSE——没有稳定键记录无法幂等，宁可红也别静默去重。
    """
    if record.get("stable_id") and record.get("source_ref"):
        return dict(record)   # 已归一化：不重复加工
    raw_id = record.get("id") or record.get("record_id")
    if raw_id in (None, ""):
        raise TemplateError("BAD_RESPONSE",
                            f"{domain} 记录缺 id，无法生成幂等键",
                            detail={"source_ref": source_ref})
    raw: dict[str, Any] = {"id": raw_id}
    for key in ("link", "order_id", "fees"):
        if record.get(key) is not None:
            raw[key] = record[key]
    raw.update(extra_raw or {})
    return {
        "stable_id": stable_id(store, domain, raw_id),
        "amount": str(record.get("amount") or record.get("total") or "0"),
        "currency": str(record.get("currency") or default_currency),
        "status": str(record.get("status") or "posted"),
        "occurred_at": record.get("occurred_at") or record.get("date")
        or record.get("created_at"),
        "source_ref": f"{source_ref}:{raw_id}",
        "raw": raw,
    }

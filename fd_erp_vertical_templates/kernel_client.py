"""内核工具面客户端（模板层访问账本的唯一通道）。

模板层运行时代码只允许经本协议访问账本 —— 与 fd-erp-ledger-mcp 的 8 个
MCP 工具一一对应，方法签名同形。生产实现走 MCP 工具面（fastmcp client），
测试/同进程实现委托内核工具函数 —— 两种实现都不给模板层暴露 SQL/DB。

2026-11-07（add-supply-chain-vertical）：协议扩展信用额度与计费工具面
（内核侧由 add-supply-chain-vertical 计费引擎任务提供，签名同形）：
- 信用：reserve_credit / release_credit / query_credit（占用/释放/回冲
  由账本记账，design D4）；
- 计费：bill_event / list_fees / get_fee_detail / submit_billing_rule /
  approve_billing_rule / reverse_fee / simulate_billing（erp-billing-engine）。
新工具未上线的实现 MUST fail-closed（如实报错，不伪造成功）。

boundary.py 审计本包时以「是否 import 本模块以外任何账务通道」为准。
"""
from __future__ import annotations

from typing import Any, Protocol


class KernelClient(Protocol):
    """fd-erp-ledger-mcp 工具的模板层视图（同形签名：8 核心工具 + 信用/计费扩展）。"""

    def submit_proposal(self, stable_id: str, lines: list[dict[str, Any]],
                        source_ref: str | None = None, kind: str = "journal",
                        reversal_of: str | None = None,
                        lane_fleet: str | None = None, lane_post: str | None = None,
                        lane_title: str | None = None, lane_target: str | None = None,
                        tenant_id: str | None = None,
                        dimensions: dict[str, str] | None = None,
                        submitted_by: str = "agent",
                        booked_on: str | None = None) -> dict[str, Any]:
        """提交拟落账事件（proposal）——不改变账本状态。

        `booked_on` 是**业务记账日**（模板按业务日期归期后传下来）。它必须出现在这一层：
        14 处模板调用点带它，而传输实现此前没有这个参数 ⇒ MCP 形态直接 `TypeError`
        （同进程形态正常，因为 in-process 客户端早就加了它）。
        """
        ...

    def list_proposals(self, status: str | None = None,
                       tenant_id: str | None = None,
                       offset: int = 0, limit: int = 100) -> dict[str, Any]:
        ...

    def confirm_proposal(self, proposal_id: int | None = None,
                         stable_id: str | None = None,
                         tenant_id: str | None = None,
                         confirmed_by: str = "human") -> dict[str, Any]:
        ...

    def reject_proposal(self, proposal_id: int | None = None,
                        stable_id: str | None = None,
                        reason: str | None = None,
                        tenant_id: str | None = None,
                        rejected_by: str = "human") -> dict[str, Any]:
        ...

    def query_balance(self, account_code: str | None = None,
                      tenant_id: str | None = None) -> dict[str, Any]:
        ...

    def query_entries(self, account_code: str | None = None,
                      txn_id: str | None = None,
                      tenant_id: str | None = None,
                      dimensions: dict[str, str] | None = None,
                      offset: int = 0, limit: int = 100) -> dict[str, Any]:
        ...

    def run_reconciliation(self, left_source: dict[str, Any],
                           right_source: dict[str, Any],
                           retry_task_id: str | None = None,
                           tenant_id: str | None = None) -> dict[str, Any]:
        ...

    def get_recon_report(self, task_id: str,
                         tenant_id: str | None = None) -> dict[str, Any]:
        ...

    # ---- 信用额度扩展（占用/释放/回冲由账本记账，design D4）----

    def reserve_credit(self, customer_id: str, stable_id: str, amount: str,
                       currency: str, source_ref: str | None = None,
                       expires_at: str | None = None) -> dict[str, Any]:
        """占用信用额度（账本记账；stable_id 幂等，同单重推只占一次）。

        `expires_at` 必填：占用没有失效时点就无法被识别为孤儿占用。
        """
        ...

    def release_credit(self, customer_id: str, stable_id: str,
                       released_by: str = "system",
                       reason: str | None = None) -> dict[str, Any]:
        """释放/回冲信用额度（按占用 stable_id 定位）。"""
        ...

    def query_credit(self, customer_id: str) -> dict[str, Any]:
        """查询信用额度：{limit, ar_balance, reserved, available}。"""
        ...

    def list_credit_reservations(self, customer_id: str | None = None,
                                 include_expired: bool = True) -> dict[str, Any]:
        """列出信用占用明细（只读）。孤儿报表用它取占用侧的事实。"""
        ...

    # ---- 计费引擎扩展（erp-billing-engine 工具面）----

    def bill_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """业务事件实时计费：返回 {fees, stable_id, duplicate}。"""
        ...

    def list_fees(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """费用明细列表（分页）。"""
        ...

    def get_fee_detail(self, fee_id: str) -> dict[str, Any]:
        """费用明细详情（四层溯源：费用项→单据→事件→规则版本）。"""
        ...

    def submit_billing_rule(self, config: dict[str, Any],
                            source_ref: str | None = None) -> dict[str, Any]:
        """提交计费规则（提案待确认，确认车道）。"""
        ...

    def approve_billing_rule(self, rule_id: str, version: str) -> dict[str, Any]:
        """审批确认计费规则版本（确认门）。"""
        ...

    def reverse_fee(self, fee_id: str, reason: str) -> dict[str, Any]:
        """费用红冲（反向明细关联原明细，复用账本冲正基因）。"""
        ...

    def simulate_billing(self, config: dict[str, Any],
                         events: list[dict[str, Any]]) -> dict[str, Any]:
        """历史数据试算（上线前对比，不落任何明细）。"""
        ...

    # ---- 事件层扩展（establish-erp-event-kernel 7.1）----
    #
    # 口径变更（原契约把这四个面记在 intentionally_absent，理由见
    # shared/kernel-client-contract.json 的 note）：供应商到货预告是**外部系统原始事实**
    # （供应商门户回流的到货申报），模板层就是这条事实的接收方；不把它落进事件层，
    # 「到货 → 库存 → 分录」就永远只有前段的一笔 jsonl 记录。规则与金额仍在内核侧：
    # 模板层只提交事实、触发派生，不定义科目、不改金额。

    def ingest_event(self, event_type: str, stable_id: str, occurred_at: Any,
                     payload: dict[str, Any],
                     source_ref: str | None = None,
                     provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        """导入车道写入一条外部事实（同 stable_id 幂等，重复提交返回首次结果）。"""
        ...

    def project_event(self, event_id: int, object_type: str, object_key: str,
                      quantity_delta: Any, unit: str,
                      dimension: dict[str, Any] | None = None) -> dict[str, Any]:
        """把已接受事件投影到对象数量面（幂等：同事件同维度不重复计数）。"""
        ...

    def query_object_balance(self, object_type: str, object_key: str,
                             dimension: dict[str, Any] | None = None
                             ) -> dict[str, Any]:
        """读对象余额（数量面唯一真相；带水位与重算校验）。"""
        ...

    def post_event(self, event_id: int) -> dict[str, Any]:
        """按生效过账规则把事件派生成分录；无规则时内核挂待补并抛
        `POSTING_RULE_MISSING`（调用方据此如实报「未过账」，不得伪造已过账）。"""
        ...

    # ---- 拟稿车道与事件查询（add-invoice-request-lane）----
    #
    # 与 ingest_event 的区别只说一句：ingest 是「外部系统报来的事实，写入即接受」，
    # submit 是「我方替调用方拟的稿，落 pending，等确认门拍板」。后者是唯一能让
    # 「申请」这种需要审核的东西进事件层的入口——直接用 ingest 等于自动通过审核。

    def submit_event(self, event_type: str, stable_id: str, occurred_at: Any,
                     payload: dict[str, Any],
                     source_ref: str | None = None) -> dict[str, Any]:
        """拟稿车道写入（lane=draft，落 pending，不参与投影、不过账直到确认）。"""
        ...

    def query_events(self, event_type: str | None = None,
                     lane: str | None = None, status: str | None = None,
                     limit: int = 100, after_event_id: int = 0) -> dict[str, Any]:
        """查事件（带完整性声明）。`producer` 不暴露：模板层不关心板块归属。"""
        ...

    # ---- 编排面到此为止：期间关账 / 拍板 / 规则族**故意不在协议上** ----
    #
    # 这是**纪律**，不是缺口：模板编排层不代任何一方关账、不代任何一方过门
    # （`shared/kernel-client-contract.json` 的 `intentionally_absent` 逐条记着理由）。
    # 但 api 的 MCP 内层复用本仓的 `McpKernelClient` 作**传输适配器**，它必须 1:1
    # 覆盖内核工具面（含这些工具），否则 api 对应端点在生产上直接 AttributeError。
    # 所以：协议 = 编排层被允许调用的方法集；`McpKernelClient` = 传输层，逐名覆盖
    # 内核 37 工具。两者由契约闸分别校验（2026-11-15 `decide_event` 生产 500 的订正）。


class McpKernelClient:
    """生产实现：经 MCP 工具面调用内核（http/sse transport）。

    fastmcp Client 惰性导入；凭证零落库（内核 BYOK 面经环境变量注入，
    本类不接触任何凭证）。

    **`token`：内核自己那道 bearer 门的钥匙（2026-11-15 生产事故的修复）。**
    内核 `fd_erp_ledger_mcp/auth.py` 在 `MCP_BEARER_TOKEN` 非空时给整条数据面
    （`/mcp`，`/health`/`/metrics`/`/.well-known` 豁免）挂 bearer 门；门一开，
    **不带 Authorization 的调用一律 401**。本类原实现是 `Client(self._url)`，
    一个头都不带 ⇒ 所有碰内核的端点被统一壳兜成 `500 INTERNAL: MCPError`，
    而 `/health`、`/pagesets` 这些不经内核的面照常 200 —— 于是「服务健康」与
    「账本取不到数」同时成立，静默了约 38 小时（2026-11-15 实历：开通财务后台
    才把这个既有断链显形）。

    传 `token` 就用 `BearerAuth` 带上它；不传则**保持原样**（内核没开门的环境、
    单测与 demo 行为逐字节不变）。**不要**在这里读环境变量：钥匙由进程配置注入
    （api 侧 `ERP_KERNEL_TOKEN`），本类只负责把它送到请求头上。
    """

    def __init__(self, url: str, tenant_id: str, token: str | None = None):
        self._url = url
        self._tenant_id = tenant_id
        self._token = (str(token).strip() if token else "") or None

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        """同步阻塞调用（fleet 车道是同步代码）：内部走 async MCP 会话。

        fastmcp Client 仅支持 async with；这里 asyncio.run 包一层，把
        CallToolResult 解包到业务 dict（与 InProcessKernelClient 同形返回），
        使本类成为 InProcess 形态的可替换生产实现。
        2026-11-07 缺陷修复：原实现用同步 with Client(...)，fastmcp Client
        不支持同步上下文管理器——本生产形态此前从未对过 live 服务器，演示
        首跑即暴露（demo e2e 走的是 InProcess 形态）。
        """
        import asyncio
        import concurrent.futures

        from fastmcp import Client  # 惰性导入：仅生产路径需要

        args.setdefault("tenant_id", self._tenant_id)
        # **丢掉值为 None 的参数**（2026-11-15 生产事故的第二层）。
        # 内核工具的 MCP schema 由 fastmcp 从函数签名生成，**在役镜像可能比仓内源码
        # 旧**（实测在役 `query_entries` 的 schema 只有 account_code/txn_id/tenant_id/
        # offset/limit，没有仓内已有的 `dimensions`）。显式传 `dimensions=None` 会被
        # pydantic 判 `unexpected_keyword_argument`，内核包成 `ToolError`、api 再兜成
        # `500 INTERNAL`。而省略该键时内核用自己的默认值，语义完全等价。
        # 所以「None = 不传」是这条链路上唯一稳定的口径：调用方照旧传 None 表达
        # 「这一维不筛」，由这里统一落成「不出现」。
        args = {k: v for k, v in args.items() if v is not None}

        # 只在有钥匙时构造 auth：`Client(url, auth=None)` 与 `Client(url)` 在
        # 各版本 fastmcp 上并不总等价，显式分支避开这个不确定性。
        client_kwargs: dict[str, Any] = {}
        if self._token:
            from fastmcp.client import BearerAuth
            client_kwargs["auth"] = BearerAuth(self._token)

        async def _run() -> Any:
            async with Client(self._url, **client_kwargs) as client:
                result = await client.call_tool(tool, args)
                data = getattr(result, "data", None)
                if data is not None:
                    return data
                return getattr(result, "structured_content", result)

        # 2026-11-08 缺陷修复（产上首现，比 11-07 那个更隐蔽）：REST 面是
        # FastAPI **async** 端点，在运行中的事件循环里调 asyncio.run 会抛
        # `RuntimeError: asyncio.run() cannot be called from a running event
        # loop`，被统一壳兜成 500 INTERNAL —— 凡碰内核的端点全中（对账单的
        # list_proposals、下单的 reserve_credit、对账的 run_reconciliation）。
        # 单测/demo e2e 走同步上下文（无运行循环）故全绿，只有
        # `transport=mcp` + 真 HTTP 请求这个组合才炸。修法：已有运行循环时把
        # 协程丢到独立线程的新循环里跑（线程内 asyncio.run 合法）。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_run())  # 同步上下文（fleet 车道 / 脚本 / 测试）
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _run()).result()

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        reversal_of=None, lane_fleet=None, lane_post=None,
                        lane_title=None, lane_target=None, tenant_id=None,
                        dimensions=None, submitted_by="agent", booked_on=None):
        return self._call("submit_proposal", {
            "stable_id": stable_id, "lines": lines, "source_ref": source_ref,
            "kind": kind, "reversal_of": reversal_of, "lane_fleet": lane_fleet,
            "lane_post": lane_post, "lane_title": lane_title,
            "lane_target": lane_target, "dimensions": dimensions,
            "submitted_by": submitted_by, "booked_on": booked_on,
        })

    def list_proposals(self, status=None, tenant_id=None, offset=0, limit=100):
        return self._call("list_proposals", {
            "status": status, "offset": offset, "limit": limit})

    def confirm_proposal(self, proposal_id=None, stable_id=None, tenant_id=None,
                         confirmed_by="human"):
        return self._call("confirm_proposal", {
            "proposal_id": proposal_id, "stable_id": stable_id,
            "confirmed_by": confirmed_by})

    def reject_proposal(self, proposal_id=None, stable_id=None, reason=None,
                        tenant_id=None, rejected_by="human"):
        return self._call("reject_proposal", {
            "proposal_id": proposal_id, "stable_id": stable_id, "reason": reason,
            "rejected_by": rejected_by})

    def query_balance(self, account_code=None, tenant_id=None):
        return self._call("query_balance", {"account_code": account_code})

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100):
        return self._call("query_entries", {
            "account_code": account_code, "txn_id": txn_id,
            "dimensions": dimensions,
            "offset": offset, "limit": limit})

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        return self._call("run_reconciliation", {
            "left_source": left_source, "right_source": right_source,
            "retry_task_id": retry_task_id})

    def get_recon_report(self, task_id, tenant_id=None):
        return self._call("get_recon_report", {"task_id": task_id})

    # ---- 信用额度扩展（MCP 工具面同形；内核未上线时 fastmcp 如实报 UNKNOWN_TOOL）----

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None, expires_at=None):
        return self._call("reserve_credit", {
            "customer_id": customer_id, "stable_id": stable_id,
            "amount": str(amount), "currency": currency, "source_ref": source_ref,
            "expires_at": expires_at})

    def release_credit(self, customer_id, stable_id, released_by="system", reason=None):
        return self._call("release_credit", {
            "customer_id": customer_id, "stable_id": stable_id,
            "released_by": released_by, "reason": reason})

    def query_credit(self, customer_id):
        return self._call("query_credit", {"customer_id": customer_id})

    def list_credit_reservations(self, customer_id=None, include_expired=True):
        return self._call("list_credit_reservations", {
            "customer_id": customer_id, "include_expired": include_expired})

    # ---- 计费引擎扩展（MCP 工具面同形）----

    def bill_event(self, event):
        return self._call("bill_event", {"event": event})

    def list_fees(self, offset=0, limit=100):
        return self._call("list_fees", {"offset": offset, "limit": limit})

    def get_fee_detail(self, fee_id):
        return self._call("get_fee_detail", {"fee_id": fee_id})

    def submit_billing_rule(self, config, source_ref=None):
        return self._call("submit_billing_rule", {
            "config": config, "source_ref": source_ref})

    def approve_billing_rule(self, rule_id, version):
        return self._call("approve_billing_rule", {
            "rule_id": rule_id, "version": version})

    def reverse_fee(self, fee_id, reason):
        return self._call("reverse_fee", {"fee_id": fee_id, "reason": reason})

    def simulate_billing(self, config, events):
        return self._call("simulate_billing", {"config": config, "events": events})

    # ---- 事件层扩展（7.1）----

    def ingest_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None, provenance=None):
        return self._call("ingest_event", {
            "event_type": event_type, "stable_id": stable_id,
            "occurred_at": occurred_at, "payload": payload,
            "source_ref": source_ref, "provenance": provenance})

    def project_event(self, event_id, object_type, object_key, quantity_delta, unit,
                      dimension=None):
        return self._call("project_event", {
            "event_id": event_id, "object_type": object_type,
            "object_key": object_key, "quantity_delta": quantity_delta,
            "unit": unit, "dimension": dimension})

    def query_object_balance(self, object_type, object_key, dimension=None):
        return self._call("query_object_balance", {
            "object_type": object_type, "object_key": object_key,
            "dimension": dimension})

    def post_event(self, event_id):
        return self._call("post_event", {"event_id": event_id})

    # ---- 拟稿车道与事件查询（add-invoice-request-lane）----

    def submit_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None):
        return self._call("submit_event", {
            "event_type": event_type, "stable_id": stable_id,
            "occurred_at": occurred_at, "payload": payload,
            "source_ref": source_ref})

    def query_events(self, event_type=None, lane=None, status=None,
                     producer=None, limit=100, after_event_id=0):
        return self._call("query_events", {
            "event_type": event_type, "lane": lane, "status": status,
            "producer": producer, "limit": limit,
            "after_event_id": after_event_id})

    # ---- 期间关账（add-period-close；内核工具 33-37）----
    #
    # 2026-11-15 补：内核侧这 5 个工具早就在（`tools.py` 33-37，`add-period-close`
    # 已归档），而 MCP 形态的客户端**一个都没转发** ⇒ 生产上 `/fi/periods` 直接
    # `NOT_WIRED: 当前传输形态未开期间关账工具（list_periods）`，而同进程形态是好的。
    # 断链之所以能挂很久：单测全走 InProcess，`NOT_WIRED` 又是「如实报未接线」的
    # 合法语义，于是「没实现」在日志里长得像「设计如此」。
    #
    # 只转发，不在这里判「顺序关账 / 只重开最近一期」——那些判定在内核 `periods`
    # 模块里，复制一份就是第二份口径。参数按内核工具签名逐字对齐（`note` 可省）。
    # `_call` 已统一把值为 None 的参数落成「不出现」，所以 `note=None` 不会变成
    # `note: null` 传给内核。

    def close_period(self, period, actor, note=None):
        return self._call("close_period", {
            "period": period, "actor": actor, "note": note})

    def reopen_period(self, period, actor, reason):
        return self._call("reopen_period", {
            "period": period, "actor": actor, "reason": reason})

    def period_status(self, period):
        return self._call("period_status", {"period": period})

    def period_report(self, period):
        return self._call("period_report", {"period": period})

    def list_periods(self):
        return self._call("list_periods", {})

    # ---- 传输面补齐：编排面故意不盖的七个工具（2026-11-15 decide 生产 500）----
    #
    # 这七个别当成「模板层能力扩张」：`KernelClient` 协议上一个都没有（纪律不变），
    # 它们存在的唯一理由是 api 的 MCP 内层就是这个类。api 的 SE 审核端点
    # （`faces.py` 的 `/se/invoice-requests/{id}/decide`）经 `KernelProxy` 直接调
    # `decide_event`，而这里没有它 ⇒ `AttributeError` ⇒ 裸 500，且 `faces.py` 只兜
    # `ApiError` 所以连 4xx 都不是。
    #
    # 参数**按内核工具签名逐字对齐**（`fd-erp-ledger-mcp/tools.py`）：少一个名字
    # 就是 `unexpected_keyword_argument`，多一个名字就是 `TypeError`。内核
    # `decide_event` 不收 `lane`/`actor`（工具内硬编码 draft/human），所以这里
    # 也不收——api 侧传了就由 api 侧显式拒绝，不许静默丢弃。

    def decide_event(self, stable_id, decision, note=""):
        return self._call("decide_event", {
            "stable_id": stable_id, "decision": decision, "note": note})

    def submit_posting_rule(self, event_type, lines, source_ref=None):
        return self._call("submit_posting_rule", {
            "event_type": event_type, "lines": lines,
            "source_ref": source_ref})

    def approve_posting_rule(self, event_type, version, approved_by):
        return self._call("approve_posting_rule", {
            "event_type": event_type, "version": version,
            "approved_by": approved_by})

    def list_posting_rules(self, event_type=None):
        return self._call("list_posting_rules", {"event_type": event_type})

    def list_posting_gaps(self):
        return self._call("list_posting_gaps", {})

    def bill_period_snapshot(self, period, snapshots, source_ref=None):
        return self._call("bill_period_snapshot", {
            "period": period, "snapshots": snapshots,
            "source_ref": source_ref})

    def list_billing_rules(self, status=None, offset=0, limit=100):
        return self._call("list_billing_rules", {
            "status": status, "offset": offset, "limit": limit})


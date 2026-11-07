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
                        submitted_by: str = "agent") -> dict[str, Any]:
        """提交拟落账事件（proposal）——不改变账本状态。"""
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
                       currency: str, source_ref: str | None = None) -> dict[str, Any]:
        """占用信用额度（账本记账；stable_id 幂等，同单重推只占一次）。"""
        ...

    def release_credit(self, customer_id: str, stable_id: str) -> dict[str, Any]:
        """释放/回冲信用额度（按占用 stable_id 定位）。"""
        ...

    def query_credit(self, customer_id: str) -> dict[str, Any]:
        """查询信用额度：{limit, ar_balance, reserved, available}。"""
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


class McpKernelClient:
    """生产实现：经 MCP 工具面调用内核（http/sse transport）。

    fastmcp Client 惰性导入；凭证零落库（内核 BYOK 面经环境变量注入，
    本类不接触任何凭证）。
    """

    def __init__(self, url: str, tenant_id: str):
        self._url = url
        self._tenant_id = tenant_id

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

        async def _run() -> Any:
            async with Client(self._url) as client:
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
                        submitted_by="agent"):
        return self._call("submit_proposal", {
            "stable_id": stable_id, "lines": lines, "source_ref": source_ref,
            "kind": kind, "reversal_of": reversal_of, "lane_fleet": lane_fleet,
            "lane_post": lane_post, "lane_title": lane_title,
            "lane_target": lane_target, "submitted_by": submitted_by,
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
                      offset=0, limit=100):
        return self._call("query_entries", {
            "account_code": account_code, "txn_id": txn_id,
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
                       source_ref=None):
        return self._call("reserve_credit", {
            "customer_id": customer_id, "stable_id": stable_id,
            "amount": str(amount), "currency": currency, "source_ref": source_ref})

    def release_credit(self, customer_id, stable_id):
        return self._call("release_credit", {
            "customer_id": customer_id, "stable_id": stable_id})

    def query_credit(self, customer_id):
        return self._call("query_credit", {"customer_id": customer_id})

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

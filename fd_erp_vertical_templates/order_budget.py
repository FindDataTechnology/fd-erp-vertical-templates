"""下单预算闸（change establish-ai-native-ecommerce-stack，agent 自主下单）。

**为什么需要它**：店面现在允许匿名下单（预付，不占信用额度）。这对人没问题——
人要自己掏钱。但 agent 自主下单时，「委托人给了多少额度」必须有一道闸，否则
agent 一旦被提示注入或判断失误，可以在没有任何上限的情况下把订单堆到任意金额。

**与既有预算闸的关系**：`fd-mcp-registry/auth_server/grant_budget.py` 管的是
「MCP 工具调用次数」（一回合算一次）。下单不是 MCP 调用，走店面 REST，
且计的是**金额**不是次数——所以这里单独一条，但用同一份账本口径：

  - 有 budget 的委托 → 硬上限，超了拒绝（与 grant_budget 同语义）
  - 无 budget 的委托 → 只受租户额度约束（本层放行，语义同 grant_budget）
  - 账本不可达 → fail-closed 拒绝，绝不「读不到就当没超」

**金额从哪来**：订单总价必须由服务端算（零售取价），不接受调用方声明的金额。
调用方声明只会让闸变成摆设。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import TemplateError


#: 预算作用域：一次委托（grant）在一段时间内的累计消费。
#: 与 grant_budget 的 scope 同构（grant_jti + run），只是这里记金额不记次数。
@dataclass(frozen=True)
class BudgetScope:
    grant_jti: str
    run_id: str | None = None

    def key(self) -> str:
        return f"{self.grant_jti}|{self.run_id or '-'}"


@dataclass(frozen=True)
class BudgetVerdict:
    allowed: bool
    reason: str = ""
    limit: str | None = None
    used: str | None = None
    remaining: str | None = None


def _dec(value: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what} 非法：{value!r}") from exc
    return d


def check_order_budget(*, budget: Any, used: Any, order_total: Any) -> BudgetVerdict:
    """纯判定：不碰存储，方便单测与复用。

    `budget` 为 None/空 ⇒ 只受租户额度约束（allowed=True，不限）。
    `used` 是该 scope 已消费的金额。
    `order_total` 是本单金额——**必须由服务端取价算出**，不能用调用方声明。
    """
    if budget in (None, "", {}):
        return BudgetVerdict(allowed=True, reason="no_grant_budget(tenant_bounded)")

    try:
        cap = _dec(budget, "委托预算 budget")
        spent = _dec(used or 0, "已消费金额")
        total = _dec(order_total, "订单金额")
    except TemplateError as exc:
        return BudgetVerdict(allowed=False, reason=exc.message)

    if total < 0:
        return BudgetVerdict(allowed=False, reason="订单金额为负")
    remaining = cap - spent
    if total > remaining:
        return BudgetVerdict(
            allowed=False,
            reason="GRANT_BUDGET_EXHAUSTED",
            limit=format(cap, "f"), used=format(spent, "f"),
            remaining=format(remaining, "f") if remaining > 0 else "0")
    return BudgetVerdict(
        allowed=True, reason="within_budget",
        limit=format(cap, "f"), used=format(spent, "f"),
        remaining=format(remaining - total, "f"))


def enforce_order_budget(ledger: Any, *, scope: BudgetScope, budget: Any,
                         order_total: Any) -> BudgetVerdict:
    """读账本 → 判定 → 通过则记账。账本不可用即 fail-closed。

    `ledger` 需要提供两个方法：
      - `spent(scope_key) -> Decimal | str`：该 scope 已消费金额
      - `consume(scope_key, amount) -> None`：记一笔消费
    两个都抛异常时本函数拒绝（不降级成「放行」）。
    """
    try:
        used = ledger.spent(scope.key())
    except Exception as exc:  # noqa: BLE001 账本读失败必须 fail-closed
        raise TemplateError(
            "RUNTIME_UNREACHABLE",
            f"预算账本不可达，拒绝下单（fail-closed）：{exc}",
            detail={"scope": scope.key(), "remedy": "恢复账本后重试"}) from exc

    verdict = check_order_budget(budget=budget, used=used, order_total=order_total)
    if not verdict.allowed:
        return verdict

    if budget not in (None, "", {}):
        try:
            ledger.consume(scope.key(), _dec(order_total, "订单金额"))
        except Exception as exc:  # noqa: BLE001 记账失败 → 这单不能算通过
            raise TemplateError(
                "RUNTIME_UNREACHABLE",
                f"预算记账失败，拒绝下单（否则额度可被绕过）：{exc}",
                detail={"scope": scope.key()}) from exc
    return verdict

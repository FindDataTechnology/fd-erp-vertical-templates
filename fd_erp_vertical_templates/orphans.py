"""孤儿信用占用报表（change: close-orphan-credit-reservations，只读）。

为什么放在模板层而不是内核
------------------------
判断"这笔占用有没有对应单据"需要同时看到**内核占用**与**模板订单**。内核的
`source_ref` 只是一个字符串键，它不知道 `order:` 的语义；让它认识供应链业务键
会把行业无关的内核耦合进垂直模板，违反该层边界铁律。所以：内核提供"占用是什么
样"的只读原语，模板层做 join 与判定。

为什么报表不自动释放
------------------
风险不对称：误杀一笔"确认慢但真实"的占用 → 同一客户可重复下单 → 额度被击穿；
漏放一笔孤儿 → 额度偏保守，业务能感知并人工处理。保守的错比冒进的错便宜，
所以本模块**只打印**，释放必须显式。
"""
from __future__ import annotations

import os
from typing import Any

from .eventlog import EventLog

#: 存量行（迁移前写入、没有 expires_at）的判定上界（天）。与内核
#: `credit_reservation_max_days` 同源口径，但报表侧需要独立可配，便于先跑一段
#: 观察期而不动内核配置。
DEFAULT_LEGACY_CEILING_DAYS = 90


def _legacy_ceiling_days() -> int:
    try:
        return max(1, int(os.environ.get("ERP_CREDIT_LEGACY_CEILING_DAYS",
                                         str(DEFAULT_LEGACY_CEILING_DAYS))))
    except ValueError:
        return DEFAULT_LEGACY_CEILING_DAYS


def _orders_by_reservation(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按订单 payload 里记录的占用 stable_id 索引订单当前态。

    后写覆盖前写：同一订单的 proposed → confirmed / cancelled 以最后一条为准。
    """
    out: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("record") != "order":
            continue
        res = ((ev.get("payload") or {}).get("credit_reservation") or {})
        stable = res.get("stable_id")
        if not stable:
            continue
        out[str(stable)] = {
            "ref": ev.get("ref"),
            "status": ev.get("status"),
            "event": ev.get("event"),
        }
    return out


def orphan_report(log: EventLog, kernel: Any, *,
                  legacy_ceiling_days: int | None = None) -> dict[str, Any]:
    """列出过期且无对应订单的占用（只读，无副作用）。

    返回三组：`orphans`（已过期 + 单据侧查无）、`expired_with_order`（已过期但
    确实有单，供人工判断）、`legacy_candidates`（没有失效时点的存量行，按
    `created_at + 上界` 判为疑似过期）。`live` 其余占用原样列出。
    """
    payload = kernel.list_credit_reservations(include_expired=True) or {}
    # 两种形状都要认：内核最初返回 `reservations`，后按 result 契约改为 paginate
    # 的 `data`（为了 rows_returned 计量戳正确）。只认一个键会让报表静默读到 0 行，
    # 然后把"我没读到"报成"没有孤儿"——2026-11-09 生产实测踩过。
    rows = payload.get("data")
    if rows is None:
        rows = payload.get("reservations")
    if rows is None:
        rows = []

    # 形状不匹配要响亮：计量戳说有行、而按已知键取不到行，说明契约变了。
    stamped = payload.get("rows_returned")
    if isinstance(stamped, int) and stamped > 0 and not rows:
        raise RuntimeError(
            f"占用明细形状不匹配：rows_returned={stamped} 但按 data/reservations 都取不到行"
            f"（返回键={sorted(payload)}）——先对齐契约再跑报表，别把读不到当成没有"
        )
    orders = _orders_by_reservation(log.events())

    ceiling = legacy_ceiling_days if legacy_ceiling_days is not None else _legacy_ceiling_days()
    ceiling_seconds = ceiling * 86400

    orphans: list[dict[str, Any]] = []
    expired_with_order: list[dict[str, Any]] = []
    legacy_candidates: list[dict[str, Any]] = []
    unjudged: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []

    for row in rows:
        stable = str(row.get("stable_id") or "")
        order = orders.get(stable)
        entry = {
            "customer_id": row.get("customer_id"),
            "stable_id": stable,
            "amount": row.get("amount"),
            "currency": row.get("currency"),
            "status": row.get("status"),
            "expires_at": row.get("expires_at"),
            "created_at": row.get("created_at"),
            "source_ref": row.get("source_ref"),
            "order_ref": order["ref"] if order else None,
            "order_status": order["status"] if order else None,
        }

        if row.get("legacy_no_expiry"):
            age = row.get("age_seconds")
            if isinstance(age, int) and age > ceiling_seconds:
                entry["verdict"] = "legacy_suspected_orphan"
                entry["basis"] = (
                    f"无失效时点（迁移前存量），已存在 {age // 86400} 天 > 上界 {ceiling} 天；"
                    + (f"订单侧查无 {row.get('source_ref')!r}" if order is None
                       else f"订单 {order['ref']} 存在（{order['status']}）")
                )
                legacy_candidates.append(entry)
            else:
                entry["verdict"] = "legacy_within_ceiling"
                entry["basis"] = f"无失效时点但未超过上界 {ceiling} 天"
                live.append(entry)
            continue

        if row.get("status") == "expired":
            if order is None:
                if not orders:
                    # 订单侧一条都没读到 —— 这说明"我没看到订单"，不是"没有订单"。
                    # 把它当孤儿会让人去释放真实占用，所以单独归组、不出判定。
                    entry["verdict"] = "unjudged_orders_side_empty"
                    entry["basis"] = ("订单侧未读到任何单据（事件档为空或路径不对），"
                                      "无法判定是否为孤儿")
                    unjudged.append(entry)
                    continue
                entry["verdict"] = "orphan"
                entry["basis"] = (
                    f"已过期（expires_at={row.get('expires_at')}）且 source_ref="
                    f"{row.get('source_ref')!r} 在订单侧查无对应单据"
                )
                orphans.append(entry)
            else:
                entry["verdict"] = "expired_with_order"
                entry["basis"] = f"已过期但订单 {order['ref']} 存在，状态 {order['status']}"
                expired_with_order.append(entry)
        else:
            entry["verdict"] = str(row.get("status"))
            entry["basis"] = "未过期" if row.get("status") == "reserved" else "已释放"
            live.append(entry)

    return {
        "ok": True,
        "scanned": len(rows),
        "orders_scanned": len(orders),
        "orphans": orphans,
        "expired_with_order": expired_with_order,
        "legacy_candidates": legacy_candidates,
        "unjudged": unjudged,
        "orders_side_empty": not orders,
        "live": live,
        "auto_released": 0,  # 恒为 0：本报表不释放任何占用（见模块 docstring）
    }

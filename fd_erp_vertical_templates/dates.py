"""分录日期的唯一取法（change: add-period-close）。

内核从迁移 0014 起，每条分录同时有两个时间：`booked_on`（业务日期，这笔记的是哪一天的业务）
与 `posted_at`（落账时刻，系统什么时候记下它）。**报表、账龄、预算实际、对账都该用前者**——
否则「10 月的单据 11 月才确认」就会被算进 11 月，而同一份报表今天跑和下月跑还会不同。

## 为什么只有一个 helper

四个消费方各自 `row.get("posted_at")` 是上一次「两套词表直接传＝聚合恒零」的同款隐患：
一处改了字段名、另一处没改，结果不报错，只是两边数字对不上，而且各自都「自洽」。
取法只写一次，谁要日期谁调这里。

## 为什么允许回落

历史行（0014 之前）与测试替身只有 `posted_at`。回落是**兼容**，不是等价：新写入的分录
一定有 `booked_on`（NOT NULL），所以生产路径永远走第一分支。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any


def entry_when(row: Any) -> Any:
    """分录的业务日期原值：`booked_on` 优先，回落 `posted_at`。"""
    if not isinstance(row, dict):
        return None
    return row.get("booked_on") or row.get("posted_at")


def entry_date(row: Any) -> date | None:
    """解析成 `date`；解析不了返回 None（调用方按「无日期」处理，不猜今天）。"""
    raw = str(entry_when(row) or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def entry_period(row: Any, grain: str = "monthly") -> str:
    """分录所属期间（monthly=YYYY-MM / weekly=YYYY-Www）；无日期即 `unknown`。"""
    day = entry_date(row)
    if day is None:
        return "unknown"
    if grain == "weekly":
        iso = day.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return day.strftime("%Y-%m")

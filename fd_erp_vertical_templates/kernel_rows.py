"""内核取数的唯一入口（change: add-ledger-books）。

分录与提案都是**分页**接口，各车道各写一份分页循环的后果已经出现过两次：一处按
`len(page) < limit` 停、另一处忘了跟进 `offset`，结果「读到 100 条就停」——不报错，
只是数字小了一截，而每条车道自己都自洽。所以分页只写在这里。

`read_proposals` 额外认 `rows_returned`：内核代理会回总行数，拿到总数就可以在一页读满时
直接停，不必再多走一次空查询；没有总数的实现（测试替身）退化成「一页不满即止」。
"""
from __future__ import annotations

from typing import Any

PAGE = 200


def _rows(out: Any) -> list[dict[str, Any]]:
    if not isinstance(out, dict):
        return []
    for key in ("entries", "proposals", "items", "data"):
        rows = out.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def read_entries(kernel: Any) -> list[dict[str, Any]]:
    """读完该租户的全部分录（内核按 booked_on, posted_at, txn_id, line_no 排序）。"""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _rows(kernel.query_entries(offset=offset, limit=PAGE))
        if not page:
            break
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += len(page)
    return rows


def read_proposals(kernel: Any, status: str | None = None) -> list[dict[str, Any]]:
    """读完提案（可按状态过滤）。"""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        out = kernel.list_proposals(status=status, offset=offset, limit=PAGE)
        page = _rows(out)
        if not page:
            break
        rows.extend(page)
        total = (out or {}).get("rows_returned") if isinstance(out, dict) else None
        if len(page) < PAGE or (total is not None and offset + len(page) >= int(total)):
            break
        offset += len(page)
    return rows


__all__ = ["PAGE", "read_entries", "read_proposals"]

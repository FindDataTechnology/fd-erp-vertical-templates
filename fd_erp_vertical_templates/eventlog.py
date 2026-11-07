"""模板层事件档（JSON Lines append-only 存储原语）。

主数据/编排/履约/取价/派工等模板层参考与工作流记录的统一存储形态：
- append-only：每行一个事件；确认/拒绝只改状态字段（重写时原始 payload
  零改动）——与 stores.py 同一纪律；
- 按租户隔离（文件名带 tenant）；
- 不是账本：不承担复式记账事实（账务事实唯一真源 = 内核）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class EventLog:
    """单文件 JSONL 事件档：append / 回放 / 状态改写（原始 payload 零改动）。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def events(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def append(self, event: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def rewrite(self, events: list[dict[str, Any]]) -> None:
        """原子重写（确认/拒绝只改状态字段；原始 payload 零改动）。"""
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        os.replace(tmp, self._path)

    def set_status(self, request_id: str, decision: str, *, decided_by: str,
                   reason: str | None = None) -> dict[str, Any] | None:
        """把 pending 请求置为 confirmed/rejected（其余事件零改动）。"""
        events = self.events()
        target = next((e for e in events
                       if e.get("request_id") == request_id
                       and e.get("status") == "pending"), None)
        if target is None:
            return None
        target["status"] = decision
        target["decided_by"] = decided_by
        target["decided_at_note"] = "confirmed-lane"
        if reason:
            target["reason"] = reason
        self.rewrite(events)
        return target

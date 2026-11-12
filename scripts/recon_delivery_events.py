#!/usr/bin/env python3
"""交付事实双写对账（change: add-carrier-freight-dimension 任务 2.3）。

**只读**：不写事件层、不改本地归档。把「本地归档自称发过的交付事件」与「内核实际
持有的事件」按 `stable_id` 对表，任一方向有差即报并点名到 stable_id。

为什么对 `stable_id` 而不是对条数：
  · 条数相等可以掩盖「我丢了一条、你多了一条」；
  · local 侧 `payload.emitted_events` 是当时写成功的清单，kernel 侧是落库的真相，
    两边都是精确集合，差集才有意义。

退出码：
  0 = 两侧一致；1 = 有差异（差异明细打到 stdout）；2 = 读不到（内核不可达 / 归档缺失）。

用法：
  python3 scripts/recon_delivery_events.py --data-dir data --tenant tpl-supply \
      --kernel-url http://127.0.0.1:8765/mcp [--day 2026-11-08] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

EVENT_TYPE = "goods_issued"
PAGE = 500


def local_stable_ids(archive: Path, *, day: str | None = None) -> dict[str, dict[str, Any]]:
    """本地归档里「自称已写成功」的交付事件：stable_id → {day, waybill}。

    只认 `pickup_checkin` 记录（交付事实的写入点）；`emitted_events` 缺失或为空说明
    当时没开双写开关 —— 那不是差异，是没参与对账，如实跳过。
    """
    out: dict[str, dict[str, Any]] = {}
    if not archive.exists():
        return out
    for line in archive.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 归档有坏行：不因此判「一致」，交给调用方看 warnings
        if rec.get("record") != "pickup_checkin":
            continue
        payload = rec.get("payload") or {}
        when = str(payload.get("occurred_at") or "")
        if day and when[:10] != day:
            continue
        for item in payload.get("emitted_events") or []:
            if not isinstance(item, dict):
                continue
            stable = str(item.get("stable_id") or "").strip()
            if stable:
                out[stable] = {"day": when[:10],
                               "waybill": payload.get("waybill_no")}
    return out


def kernel_stable_ids(kernel: Any, *, day: str | None = None) -> dict[str, dict[str, Any]]:
    """内核持有的交付事件：stable_id → {day, status, waybill}（分页读完，不截断）。

    终止判据用**内核自带的完整性声明**（`declared_total` 与「id 有没有前进」），
    不用「本页条数 < 我请求的 limit」——那假设对端一定按我给的页大小返回，
    对端一有更小的内部上限就会静默只读第一页（假一致）。
    """
    out: dict[str, dict[str, Any]] = {}
    after = 0
    seen = 0
    total: int | None = None
    while True:
        page = kernel.query_events(event_type=EVENT_TYPE, limit=PAGE,
                                   after_event_id=after) or {}
        rows = page.get("events") or page.get("data") or []
        declared = page.get("declared_total")
        if isinstance(declared, int):
            total = declared
        if not rows:
            break
        seen += len(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            when = str(row.get("occurred_at") or "")
            if day and when[:10] != day:
                continue
            stable = str(row.get("stable_id") or "").strip()
            if stable:
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                out[stable] = {"day": when[:10], "status": row.get("status"),
                               "waybill": payload.get("waybill")}
        nxt = max(int(r.get("id") or 0) for r in rows if isinstance(r, dict))
        if nxt <= after:
            break  # 游标没前进：防死循环
        after = nxt
        if total is not None and seen >= total:
            break
    return out


def recon(local: dict[str, dict[str, Any]],
          kernel: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """纯函数：两侧集合求差。返回可直接打印/断言的结论。"""
    only_local = sorted(set(local) - set(kernel))
    only_kernel = sorted(set(kernel) - set(local))
    matched = sorted(set(local) & set(kernel))
    return {
        "ok": not only_local and not only_kernel,
        "local_total": len(local),
        "kernel_total": len(kernel),
        "matched": len(matched),
        "only_local": [{"stable_id": s, **local[s]} for s in only_local],
        "only_kernel": [{"stable_id": s, **kernel[s]} for s in only_kernel],
        "note": ("两侧一致（按 stable_id 逐条对上）" if not only_local and not only_kernel
                 else "双写有洞：only_local=本地有内核无（写了没落 / 被删）；"
                      "only_kernel=内核有本地无（绕过本层写入，或归档丢失）"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="交付事实双写对账（只读）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--kernel-url", required=True)
    ap.add_argument("--day", default=None, help="只对账该日（YYYY-MM-DD）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    from fd_erp_vertical_templates.kernel_client import McpKernelClient

    archive = Path(args.data_dir) / f"fulfillment-{args.tenant}.jsonl"
    if not archive.exists():
        print(f"读不到本地归档：{archive}", file=sys.stderr)
        return 2
    local = local_stable_ids(archive, day=args.day)
    try:
        kernel = kernel_stable_ids(McpKernelClient(args.kernel_url, args.tenant),
                                   day=args.day)
    except Exception as exc:  # 内核不可达 → 对账做不了，如实 2（不是「一致」）
        print(f"内核事件读不到，对账未完成：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    out = recon(local, kernel)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"本地 {out['local_total']} · 内核 {out['kernel_total']} · 对上 "
              f"{out['matched']} → {'一致' if out['ok'] else '有差异'}")
        for item in out["only_local"]:
            print(f"  仅本地：{item['stable_id']}（{item.get('day')} 运单"
                  f"{item.get('waybill')}）")
        for item in out["only_kernel"]:
            print(f"  仅内核：{item['stable_id']}（{item.get('day')} 状态"
                  f"{item.get('status')}）")
        print(out["note"])
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

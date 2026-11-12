"""双写对账（change: add-carrier-freight-dimension 任务 2.3）。

判据：
1. 按 `stable_id` 逐条对表，条数相等但有替换也报（条数不是判据）；
2. 两个方向分别点名（本地有内核无 / 内核有本地无）；
3. `--day` 只圈该业务日，两侧同一口径过滤；
4. 内核读不到 → 退出码 2（不是「一致」）；有差异 → 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from recon_delivery_events import (  # noqa: E402
    kernel_stable_ids, local_stable_ids, main, recon)

TENANT = "tpl-supply"
DAY = "2026-11-08"


class _StubKernel:
    """分页替身：一页只回 2 行，逼出调用方的 after_event_id 翻页逻辑。"""

    def __init__(self, rows: list[dict]):
        self._rows = sorted(rows, key=lambda r: r["id"])
        self.calls: list[dict] = []

    def query_events(self, event_type=None, lane=None, status=None, limit=100,
                     after_event_id=0):
        self.calls.append({"event_type": event_type, "limit": limit,
                           "after": after_event_id})
        rows = [r for r in self._rows if r["id"] > after_event_id][:2]
        return {"events": rows, "declared_total": len(self._rows)}


def _archive(tmp_path, records) -> Path:
    p = tmp_path / f"fulfillment-{TENANT}.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records),
                 encoding="utf-8")
    return p


def _pickup(stable_ids, *, when=f"{DAY}T09:00:00", waybill="WB-1"):
    return {"record": "pickup_checkin", "ref": "PC-1", "event": "created",
            "payload": {"waybill_no": waybill, "occurred_at": when,
                        "emitted_events": [{"stable_id": s, "duplicate": False}
                                           for s in stable_ids]}}


def _event(eid, stable_id, *, when=f"{DAY}T09:00:00", status="pending",
           waybill="WB-1"):
    return {"id": eid, "stable_id": stable_id, "occurred_at": when,
            "status": status, "payload": {"waybill": waybill}}


def test_parity_when_both_sides_match(tmp_path):
    archive = _archive(tmp_path, [_pickup(["A", "B"])])
    local = local_stable_ids(archive)
    kernel = kernel_stable_ids(_StubKernel([_event(1, "A"), _event(2, "B")]))
    out = recon(local, kernel)
    assert out["ok"] is True and out["matched"] == 2
    assert out["only_local"] == [] and out["only_kernel"] == []


def test_equal_counts_but_swapped_ids_is_a_difference(tmp_path):
    """条数相等不等于一致：这一条就是本对账存在的理由。"""
    archive = _archive(tmp_path, [_pickup(["A", "B"])])
    out = recon(local_stable_ids(archive),
                kernel_stable_ids(_StubKernel([_event(1, "A"), _event(2, "C")])))
    assert out["ok"] is False
    assert out["local_total"] == out["kernel_total"] == 2
    assert [i["stable_id"] for i in out["only_local"]] == ["B"]
    assert [i["stable_id"] for i in out["only_kernel"]] == ["C"]


def test_both_directions_are_named(tmp_path):
    archive = _archive(tmp_path, [_pickup(["A", "LOST"])])
    out = recon(local_stable_ids(archive),
                kernel_stable_ids(_StubKernel([_event(1, "A"),
                                               _event(2, "ORPHAN")])))
    assert [i["stable_id"] for i in out["only_local"]] == ["LOST"]
    assert [i["stable_id"] for i in out["only_kernel"]] == ["ORPHAN"]
    assert "写了没落" in out["note"] and "绕过本层" in out["note"]


def test_day_filter_same_caliber_on_both_sides(tmp_path):
    archive = _archive(tmp_path, [_pickup(["A"], when=f"{DAY}T09:00:00"),
                                 _pickup(["B"], when="2026-11-09T09:00:00")])
    kernel = _StubKernel([_event(1, "A", when=f"{DAY}T09:00:00"),
                          _event(2, "B", when="2026-11-09T09:00:00")])
    out = recon(local_stable_ids(archive, day=DAY),
                kernel_stable_ids(kernel, day=DAY))
    assert out["ok"] is True and out["matched"] == 1


def test_kernel_paging_reads_everything(tmp_path):
    rows = [_event(i, f"S{i}") for i in range(1, 6)]
    archive = _archive(tmp_path, [_pickup([f"S{i}" for i in range(1, 6)])])
    stub = _StubKernel(rows)
    out = recon(local_stable_ids(archive), kernel_stable_ids(stub))
    assert out["ok"] is True and out["matched"] == 5
    assert len(stub.calls) >= 3  # 一页 2 行：5 条至少要翻 3 次


def test_records_without_emitted_events_are_skipped(tmp_path):
    """没开双写的归档（无 emitted_events）不算差异，也不进对账。"""
    archive = _archive(tmp_path, [{"record": "pickup_checkin", "ref": "PC-1",
                                   "payload": {"occurred_at": f"{DAY}T09:00:00"}}])
    assert local_stable_ids(archive) == {}


def test_exit_codes(tmp_path, monkeypatch, capsys):
    archive = _archive(tmp_path, [_pickup(["A"])])
    # 缺归档 → 2
    assert main(["--data-dir", str(tmp_path / "nope"), "--tenant", TENANT,
                 "--kernel-url", "http://x/mcp"]) == 2
    # 一致 → 0（把 kernel_stable_ids 换成替身，避免真连网）
    import recon_delivery_events as mod
    monkeypatch.setattr(mod, "kernel_stable_ids",
                        lambda kernel, day=None: {"A": {"day": DAY}})
    assert main(["--data-dir", str(tmp_path), "--tenant", TENANT,
                 "--kernel-url", "http://x/mcp"]) == 0
    assert archive.exists()
    # 差异 → 1
    monkeypatch.setattr(mod, "kernel_stable_ids",
                        lambda kernel, day=None: {"Z": {"day": DAY}})
    assert main(["--data-dir", str(tmp_path), "--tenant", TENANT,
                 "--kernel-url", "http://x/mcp", "--json"]) == 1
    assert '"ok": false' in capsys.readouterr().out

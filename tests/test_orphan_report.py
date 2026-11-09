"""孤儿占用报表的契约测试（change: close-orphan-credit-reservations）。

三条要害：① 过期且无单 → 进孤儿清单且给出判定依据；② 过期但有单 → 不算孤儿；
③ 报表无副作用（不释放、不改状态、两次输出一致）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fd_erp_vertical_templates.eventlog import EventLog
from fd_erp_vertical_templates.orphans import orphan_report


class StubKernel:
    """只提供报表需要的唯一原语：占用明细。"""

    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def list_credit_reservations(self, customer_id=None, include_expired=True):
        self.calls += 1
        rows = [dict(r) for r in self._rows]
        if customer_id is not None:
            rows = [r for r in rows if r["customer_id"] == customer_id]
        if not include_expired:
            rows = [r for r in rows if r.get("status") != "expired"]
        return {"ok": True, "rows_returned": len(rows), "reservations": rows}


def _row(stable, status, *, source_ref="order:SO-1", legacy=False, age_seconds=None):
    return {
        "stable_id": stable, "customer_id": "cust-1", "amount": "10.000000",
        "currency": "CNY", "status": status,
        "expires_at": None if legacy else "2026-11-01T00:00:00+00:00",
        "created_at": "2026-10-20T00:00:00+00:00", "source_ref": source_ref,
        "legacy_no_expiry": legacy, "age_seconds": age_seconds,
    }


def _log(tmp_path: Path, orders=(), tenant: str = "acme") -> EventLog:
    # 与 OrderOrchestrator 同名的真实事件档
    log = EventLog(tmp_path / f"orchestration-{tenant}.jsonl")
    # 即使没有任何订单也让文件存在：报表"无副作用"的断言要能比较前后字节。
    log.path.touch(exist_ok=True)
    for ref, stable, status in orders:
        log.append({
            "record": "order", "ref": ref, "event": status, "status": status,
            "payload": {"customer_id": "cust-1",
                        "credit_reservation": {"stable_id": stable}},
        })
    return log


def test_expired_without_order_is_orphan_with_basis(tmp_path):
    # 订单侧有一条**无关**订单：证明"读到了订单侧"，只是这笔占用对不上任何单
    log = _log(tmp_path, [("SO-OTHER", "res-other", "confirmed")])
    kernel = StubKernel([_row("res-orphan", "expired", source_ref="order:SO-404")])

    report = orphan_report(log, kernel)

    assert [o["stable_id"] for o in report["orphans"]] == ["res-orphan"]
    basis = report["orphans"][0]["basis"]
    assert "已过期" in basis and "SO-404" in basis, "必须给出可复算的判定依据"
    assert report["auto_released"] == 0


def test_expired_with_order_is_not_orphan(tmp_path):
    log = _log(tmp_path, [("SO-1", "res-live", "confirmed")])
    kernel = StubKernel([_row("res-live", "expired", source_ref="order:SO-1")])

    report = orphan_report(log, kernel)

    assert report["orphans"] == [], "有单就不是孤儿"
    assert [r["stable_id"] for r in report["expired_with_order"]] == ["res-live"]
    assert report["expired_with_order"][0]["order_ref"] == "SO-1"
    assert "confirmed" in report["expired_with_order"][0]["basis"]


def test_cancelled_order_still_counts_as_order(tmp_path):
    """取消的单据仍是一份存在过的单据：判孤儿要人看，不能自动当无单。"""
    log = _log(tmp_path, [("SO-2", "res-cancel", "cancelled")])
    kernel = StubKernel([_row("res-cancel", "expired", source_ref="order:SO-2")])

    report = orphan_report(log, kernel)

    assert report["orphans"] == []
    assert report["expired_with_order"][0]["order_status"] == "cancelled"


def test_report_has_no_side_effects(tmp_path):
    log = _log(tmp_path)
    before = (tmp_path / "orchestration-acme.jsonl").read_text(encoding="utf-8")
    kernel = StubKernel([_row("res-orphan", "expired"), _row("res-live", "reserved")])

    first = orphan_report(log, kernel)
    second = orphan_report(log, kernel)

    assert first == second, "数据未变时两次输出必须一致"
    assert (tmp_path / "orchestration-acme.jsonl").read_text(encoding="utf-8") == before, \
        "报表不得改写事件档"
    assert kernel.calls == 2


def test_report_never_calls_release(tmp_path):
    """静态证明：本模块不产生释放动作（报表到 release 无调用路径）。"""

    class ReleasingKernel(StubKernel):
        def release_credit(self, *a, **kw):  # pragma: no cover - 被调用即失败
            raise AssertionError("报表不得释放占用")

    log = _log(tmp_path, [("SO-OTHER", "res-other", "confirmed")])
    report = orphan_report(log, ReleasingKernel([_row("res-orphan", "expired")]))
    assert report["orphans"], "前提：确有孤儿"
    assert report["auto_released"] == 0


def test_unexpired_reservation_is_live_not_orphan(tmp_path):
    log = _log(tmp_path)
    kernel = StubKernel([_row("res-ok", "reserved", source_ref="order:SO-9")])

    report = orphan_report(log, kernel, legacy_ceiling_days=90)

    assert report["orphans"] == [] and report["legacy_candidates"] == []
    assert [r["stable_id"] for r in report["live"]] == ["res-ok"]


def test_legacy_row_without_expiry_is_judged_by_age(tmp_path):
    """存量行没有失效时点：按 created_at 起的存续时长与上界比较，而不是补一个未来时间。"""
    log = _log(tmp_path)
    kernel = StubKernel([
        _row("res-old", "reserved", legacy=True, age_seconds=100 * 86400),
        _row("res-new", "reserved", legacy=True, age_seconds=3 * 86400),
    ])

    report = orphan_report(log, kernel, legacy_ceiling_days=90)

    assert [r["stable_id"] for r in report["legacy_candidates"]] == ["res-old"]
    assert report["legacy_candidates"][0]["verdict"] == "legacy_suspected_orphan"
    assert "100 天" in report["legacy_candidates"][0]["basis"]
    assert [r["stable_id"] for r in report["live"]] == ["res-new"]


# ---------------- CLI 入口（任务 4.3 的执行面） ----------------

def test_cli_orphans_exit_code_and_output(tmp_path, capsys, monkeypatch):
    """`orphans` 子命令：有孤儿则 rc=1（便于定时巡检报警），并打印判定依据。"""
    from fd_erp_vertical_templates import cli

    log = _log(tmp_path, [("SO-1", "res-live", "confirmed")])
    log.append({"record": "order", "ref": "SO-2", "event": "confirmed", "status": "confirmed",
                "payload": {"customer_id": "cust-1",
                            "credit_reservation": {"stable_id": "res-ok"}}})

    class _K:
        def __init__(self, url, tenant_id): pass
        def list_credit_reservations(self, customer_id=None, include_expired=True):
            return {"ok": True, "rows_returned": 2, "reservations": [
                _row("res-orphan", "expired", source_ref="order:SO-404"),
                _row("res-ok", "reserved", source_ref="order:SO-2"),
            ]}

    monkeypatch.setattr("fd_erp_vertical_templates.kernel_client.McpKernelClient", _K)

    rc = cli._run_orphans("http://kernel", str(tmp_path), "acme", False, None)
    out = capsys.readouterr().out

    assert rc == 1, "有孤儿时退出码必须非零"
    assert "res-orphan" in out and "SO-404" in out
    assert "自动释放：0" in out, "报表必须显式声明它没有释放任何东西"


def test_cli_orphans_clean_exit_zero(tmp_path, capsys, monkeypatch):
    from fd_erp_vertical_templates import cli

    log = _log(tmp_path, [("SO-1", "res-1", "confirmed")])

    class _K:
        def __init__(self, url, tenant_id): pass
        def list_credit_reservations(self, customer_id=None, include_expired=True):
            return {"ok": True, "rows_returned": 1,
                    "reservations": [_row("res-1", "reserved", source_ref="order:SO-1")]}

    monkeypatch.setattr("fd_erp_vertical_templates.kernel_client.McpKernelClient", _K)

    assert cli._run_orphans("http://kernel", str(tmp_path), "acme", False, None) == 0
    assert "孤儿（已过期 + 单据侧查无）：0" in capsys.readouterr().out


def test_empty_orders_side_never_declares_orphans(tmp_path):
    """订单侧一条都没读到 → 不许判孤儿（"我没看到"不是"没有"）。

    这是本报表最危险的默认：指错 --data-dir 会把每一笔过期占用都列成孤儿，
    而人照着清单去释放，就是误杀真实占用。
    """
    log = _log(tmp_path)  # 空事件档
    kernel = StubKernel([_row("res-expired", "expired", source_ref="order:SO-1")])

    report = orphan_report(log, kernel)

    assert report["orders_side_empty"] is True
    assert report["orphans"] == [], "订单侧为空时不得产出孤儿结论"
    assert [r["stable_id"] for r in report["unjudged"]] == ["res-expired"]
    assert report["unjudged"][0]["verdict"] == "unjudged_orders_side_empty"


def test_cli_exit_two_when_orders_side_empty(tmp_path, capsys, monkeypatch):
    """CLI 在订单侧读不到时非零退出（2），避免被当成"已检查、无异常"。"""
    from fd_erp_vertical_templates import cli

    _log(tmp_path)

    class _K:
        def __init__(self, url, tenant_id): pass
        def list_credit_reservations(self, customer_id=None, include_expired=True):
            return {"ok": True, "rows_returned": 1,
                    "reservations": [_row("res-x", "expired", source_ref="order:SO-9")]}

    monkeypatch.setattr("fd_erp_vertical_templates.kernel_client.McpKernelClient", _K)

    rc = cli._run_orphans("http://kernel", str(tmp_path), "acme", False, None)

    assert rc == 2, "订单侧为空时必须是 2（检查无效），不是 0 也不是 1"
    assert "不能用来判断孤儿" in capsys.readouterr().out


# ---- 消费方形状兼容（2026-11-09 生产实测：内核改 paginate 后报表静默读到 0 行）----

def test_report_reads_paginate_shape(tmp_path):
    """内核按 result 契约返回 data/total/rows_returned 时，报表必须能读到行。"""
    log = _log(tmp_path, [("SO-OTHER", "res-other", "confirmed")])

    class _K:
        def list_credit_reservations(self, customer_id=None, include_expired=True):
            return {"ok": True, "data": [_row("res-orphan", "expired")],
                    "total": 1, "rows_returned": 1, "truncated": False}

    report = orphan_report(log, _K())
    assert report["scanned"] == 1
    assert [o["stable_id"] for o in report["orphans"]] == ["res-orphan"]


def test_report_raises_on_shape_mismatch(tmp_path):
    """计量戳说有行、却按已知键取不到 → 必须响亮失败，不许报"0 孤儿"。"""
    log = _log(tmp_path, [("SO-OTHER", "res-other", "confirmed")])

    class _K:
        def list_credit_reservations(self, customer_id=None, include_expired=True):
            return {"ok": True, "rows_returned": 2, "unexpected_key": [{"a": 1}]}

    with pytest.raises(RuntimeError, match="形状不匹配"):
        orphan_report(log, _K())

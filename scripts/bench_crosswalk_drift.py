#!/usr/bin/env python3
"""平台 crosswalk 三态 vs 本地派生的漂移闸（change prove-line-seams）。

**为什么是比对而不是取代**：ERP 有三类平台没有的输入——provider 测试明细、
live 验收记录、模板角色绑定。把权威单点化成平台会丢掉它们。所以这里是两份
**互相证伪**的派生：本地派生回答「我们这边查到的三态」，平台读面回答「本体侧
记的三态」，两者对同一 provider 同一态给出不同 ok 就红。

**为什么不自己再写一份投影**：平台响应 → 三态的那个投影已经存在，就在
``fd_erp_vertical_templates.bindings._load_from_read_face`` 里（`load_status(fetch=…)`）。
本脚本通过传 ``fetch`` 复用它——一份投影，两个读者（生成器将来若要切源，走的也是这条）。

**为什么这个文件可以发 HTTP**：模板包有一条机械边界——不许自建出网
（`scripts/payment_direct_call_gate.py`，ALLOWLIST 只有 `runtime_client.py`）。
本脚本住在 `scripts/`，不在该闸的扫描面（`PACKAGE = REPO / "fd_erp_vertical_templates"`）
之内。

shortcut: 本文件的出网豁免依赖「闸只扫 fd_erp_vertical_templates/ 包」这一事实；
闸若扩到全仓，改成由调用方注入 fetch（与本脚本现在的形状一致，改动很小）。

退出码（与 `line-seam-proof` 的三值口径一致）：
  0  无漂移
  1  有漂移（两侧同一 provider 同一态，ok 不同）
  2  判不出（读面未配置 / 不可达 / 令牌缺失 / 平台投影不可用）

用法：
  python3 scripts/bench_crosswalk_drift.py
  python3 scripts/bench_crosswalk_drift.py --json
  python3 scripts/bench_crosswalk_drift.py --local out.json

环境变量：
  BENCH_CROSSWALK_READ_FACE   读面 base URL（空 = 永不出网 → 退出码 2）
  BENCH_CROSSWALK_DOMAIN      域键，默认 erp
  BENCH_CONSUMER_TOKEN        机器的 X-Consumer-Token（只从环境读，不落盘不打日志）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATUS_PATH = REPO / "connector-status.json"

READ_FACE_ENV = "BENCH_CROSSWALK_READ_FACE"
TOKEN_ENV = "BENCH_CONSUMER_TOKEN"
TIMEOUT_SECONDS = 3.0

STATES = ("catalog", "executor_tests", "live")
READ_FACE_SOURCE = "bench-read-face"

EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_UNDETERMINED = 2


def _http_fetch(token: str):
    """A ``fetch(url) -> dict`` for ``bindings.load_status``; short timeout.

    The URL is built by ``bindings`` (it owns the path shape); this closure only
    performs the request. The token rides the header and never appears in a
    reason string — callers report the exception CLASS, not its message.
    """

    def fetch(url: str) -> dict:
        req = urllib.request.Request(url, headers={"X-Consumer-Token": token})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("crosswalks"), list):
            raise ValueError("读面响应缺 crosswalks 数组")
        return payload

    return fetch


def _states(status: dict) -> dict[str, dict[str, dict]]:
    """``provider -> state -> {ok, evidence}`` from a status-shaped body.

    Only ``ok: true`` states are recorded: absence is absence, and comparing
    "no entry" against "entry with ok false" would manufacture drift that is
    really just two ways of saying the same thing.
    """
    out: dict[str, dict[str, dict]] = {}
    for integ in status.get("integrations") or []:
        provider = integ.get("provider")
        if not provider:
            continue
        bucket = out.setdefault(provider, {})
        for state in STATES:
            value = ((integ.get("states") or {}).get(state)) or {}
            if value.get("ok") is True:
                bucket[state] = value
    return out


def diff(local: dict, remote: dict) -> dict:
    """Three buckets, deliberately not one number.

    - ``drift``          both sides have provider+state and disagree on ``ok``
                         → a contradiction; red.
    - ``evidence_only``  same ``ok``, different evidence text → warning. The
                         vendor catalog and the ontology crosswalk may legally
                         name the same evidence differently.
    - ``not_in_*``       only one side has the provider at all → coverage, not
                         contradiction.
    """
    drift: list[dict] = []
    evidence_only: list[dict] = []
    for provider in sorted(set(local) & set(remote)):
        l, r = local[provider], remote[provider]
        for state in STATES:
            lv, rv = l.get(state), r.get(state)
            l_ok, r_ok = bool(lv), bool(rv)
            if l_ok != r_ok:
                drift.append({
                    "provider": provider, "state": state,
                    "local_ok": l_ok, "platform_ok": r_ok,
                })
            elif l_ok and (lv or {}).get("evidence") != (rv or {}).get("evidence"):
                evidence_only.append({
                    "provider": provider, "state": state,
                    "local_evidence": (lv or {}).get("evidence"),
                    "platform_evidence": (rv or {}).get("evidence"),
                })
    return {
        "drift": drift,
        "evidence_only": evidence_only,
        "not_in_read_face": sorted(set(local) - set(remote)),
        "not_in_local": sorted(set(remote) - set(local)),
    }


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload.get("reason") or payload.get("text") or "", file=sys.stderr)


def _undetermined(reason: str, *, as_json: bool) -> int:
    _emit({"verdict": "undetermined", "reason": reason}, as_json=as_json)
    return EXIT_UNDETERMINED


def _report_text(result: dict, *, source: str) -> str:
    lines = [f"读面: {source}"]
    lines.append(
        "漂移 %d / 证据措辞差异 %d / 平台未覆盖 %d / 本地未引用 %d"
        % (
            len(result["drift"]), len(result["evidence_only"]),
            len(result["not_in_read_face"]), len(result["not_in_local"]),
        )
    )
    for d in result["drift"]:
        lines.append(
            f"  DRIFT {d['provider']}/{d['state']}: 本地 ok={d['local_ok']} 平台 ok={d['platform_ok']}"
        )
    for e in result["evidence_only"]:
        lines.append(
            f"  warn  {e['provider']}/{e['state']}: evidence 不同"
            f"（{e['local_evidence']} vs {e['platform_evidence']}）"
        )
    if result["not_in_read_face"]:
        lines.append(f"  平台未覆盖: {', '.join(result['not_in_read_face'])}")
    if result["not_in_local"]:
        lines.append(f"  本地未引用: {', '.join(result['not_in_local'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="平台 crosswalk 三态 vs 本地派生的漂移比对")
    ap.add_argument("--json", dest="as_json", action="store_true")
    ap.add_argument("--local", help="本地派生文件（默认 connector-status.json）")
    args = ap.parse_args(argv)

    base = os.environ.get(READ_FACE_ENV, "").strip()
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not base:
        return _undetermined(
            f"判不出：{READ_FACE_ENV} 未配置（本地派生照常，但没有可比对的平台答案）",
            as_json=args.as_json,
        )
    if not token:
        return _undetermined(f"判不出：{TOKEN_ENV} 未注入", as_json=args.as_json)

    local_path = Path(args.local) if args.local else STATUS_PATH
    try:
        local_raw = json.loads(local_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _undetermined(
            f"判不出：本地派生不可读（{local_path.name}）：{type(exc).__name__}",
            as_json=args.as_json,
        )

    # The platform projection is NOT reimplemented here — it lives in the
    # template package, which is also what a future generator switch would use.
    from fd_erp_vertical_templates import bindings

    try:
        platform_body = bindings.load_status(fetch=_http_fetch(token))
    except Exception as exc:  # noqa: BLE001 — any failure is "cannot tell"
        return _undetermined(
            f"判不出：读面不可达或投影失败（{type(exc).__name__}）", as_json=args.as_json
        )
    if platform_body.get("source") != READ_FACE_SOURCE:
        return _undetermined(
            "判不出：读面未返回平台答案（走了本地回退）", as_json=args.as_json
        )

    result = diff(_states(local_raw), _states(platform_body))
    verdict = "drift" if result["drift"] else "clean"
    if args.as_json:
        _emit({"verdict": verdict, **result}, as_json=True)
    else:
        print(_report_text(result, source=base))
    return EXIT_DRIFT if result["drift"] else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())

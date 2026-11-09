"""模板包 CLI：边界审计自跑 + 模板清单校验 + 孤儿占用报表（全只读，零写路径）。

用法：
  fd-erp-vertical-templates audit                # 边界审计（包 + 资产）
  fd-erp-vertical-templates validate --template templates/crossborder
  fd-erp-vertical-templates orphans --url <kernel-mcp-url> [--data-dir DIR] [--json]

`orphans` 是 change `close-orphan-credit-reservations` 任务 4.3 的**执行入口**：
它把"过期且无对应订单"的信用占用列出来，供人判断。它不释放任何占用——
自动释放会把"确认慢但真实"的单子误杀成额度可用（见 orphans.py 模块 docstring）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .boundary import audit_template
from .manifest import load_template_manifest

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent


def _run_audit() -> int:
    report = audit_template(PKG_DIR, asset_dirs=[
        REPO_ROOT / "templates" / "crossborder",
        REPO_ROOT / "templates" / "restaurant-chain",
        REPO_ROOT / "templates" / "supply-chain",
    ])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _run_validate(template: str) -> int:
    manifest = load_template_manifest(Path(template))
    print(json.dumps({"ok": True, "vertical": manifest["vertical"],
                      "version": manifest["version"],
                      "assets": {k: a["version"] for k, a in manifest["assets"].items()}},
                     ensure_ascii=False, indent=2))
    return 0


def _run_orphans(url: str, data_dir: str, tenant_id: str, as_json: bool,
                 legacy_ceiling_days: int | None) -> int:
    """孤儿占用报表（只读）。退出码：0 = 无孤儿；1 = 有孤儿（便于定时巡检报警）。"""
    from .eventlog import EventLog
    from .kernel_client import McpKernelClient
    from .orphans import orphan_report

    kernel = McpKernelClient(url, tenant_id)
    # 订单事件档的真实命名由 OrderOrchestrator 决定（orchestration-<tenant>.jsonl），
    # CLI 不得另发明一个：写死一个错名字会让报表永远读到空订单侧，
    # 进而把每笔过期占用都列成孤儿。
    log = EventLog(Path(data_dir) / f"orchestration-{tenant_id}.jsonl")
    report = orphan_report(log, kernel, legacy_ceiling_days=legacy_ceiling_days)

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        c = report
        print(f"scanned {c['scanned']} reservation(s) / {c['orders_scanned']} order(s)")
        print(f"孤儿（已过期 + 单据侧查无）：{len(c['orphans'])}")
        for o in c["orphans"]:
            print(f"  - {o['customer_id']} {o['amount']} {o['currency']} "
                  f"stable_id={o['stable_id']} | {o['basis']}")
        print(f"过期但有单（人工判断）：{len(c['expired_with_order'])}")
        for o in c["expired_with_order"]:
            print(f"  - {o['stable_id']} -> {o['order_ref']} ({o['order_status']})")
        print(f"存量无失效时点且超上界：{len(c['legacy_candidates'])}")
        for o in c["legacy_candidates"]:
            print(f"  - {o['stable_id']} | {o['basis']}")
        if c.get("unjudged"):
            print(f"未判定（订单侧读不到）：{len(c['unjudged'])}")
            for o in c["unjudged"]:
                print(f"  - {o['stable_id']} | {o['basis']}")
        if c.get("orders_side_empty"):
            print("⚠ 订单侧一条单据都没读到：本次结果不能用来判断孤儿。"
                  "先确认 --data-dir 与 --tenant-id 指向真实事件档，再重跑。")
        print(f"其余在用：{len(c['live'])}   |  自动释放：{c['auto_released']}（本报表恒不释放）")

    # 订单侧读不到时结论无效：非零退出，避免被当成"已检查、无异常"。
    if report.get("orders_side_empty") and report["scanned"]:
        return 2
    return 1 if (report["orphans"] or report["legacy_candidates"]) else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="fd-erp-vertical-templates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit", help="模板边界审计（机械化，只读）")
    val = sub.add_parser("validate", help="校验模板四件套清单")
    val.add_argument("--template", required=True)
    orp = sub.add_parser("orphans", help="孤儿信用占用报表（只读，永不自动释放）")
    orp.add_argument("--url", required=True, help="内核 MCP 工具面地址")
    orp.add_argument("--data-dir", default="./data", help="模板层事件档目录（读订单侧）")
    orp.add_argument("--tenant-id", default="default")
    orp.add_argument("--legacy-ceiling-days", type=int, default=None,
                     help="存量行（无 expires_at）判定上界，缺省读 ERP_CREDIT_LEGACY_CEILING_DAYS")
    orp.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.cmd == "audit":
        sys.exit(_run_audit())
    if args.cmd == "validate":
        sys.exit(_run_validate(args.template))
    if args.cmd == "orphans":
        sys.exit(_run_orphans(args.url, args.data_dir, args.tenant_id, args.json,
                              args.legacy_ceiling_days))


if __name__ == "__main__":
    main()

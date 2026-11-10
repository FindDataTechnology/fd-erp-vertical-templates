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


def _run_validate(template: str, as_json: bool = True,
                  install_gate: bool = False) -> int:
    """四件套清单 + 资产版本对拍 + 发布注记 + 能力面一起校验。

    只看清单会漏掉「矩阵改了、preset 没重生成」这类漂移——那正是我们要防的。
    机器可读结果由 `validation.validate_template` 产出（模板名/资产名/违规码/定位，
    不依赖网络、两次运行一致），CLI 只负责把它打到 stdout。

    `install_gate=True`（`install-check` 子命令）时角色候选集为空会压红 `ok`；
    默认不压红——角色缺 provider 是安装前置条件，不是模板结构缺陷（见 bindings.py）。
    """
    from .capability import matrix_path_for
    from .capability_check import check_template, render_lint
    from .validation import render_validation, validate_template

    capability = None
    if matrix_path_for(template).is_file():
        capability = check_template(template, doc_paths=[])
    result = validate_template(template, install_gate=install_gate,
                               capability=capability)
    # 清单里可能本身就有违规（缺资产/版本漂移）：不能再用会抛错的加载器取展示字段，
    # 否则「删一个资产」这类最该被点名的场景会变成 traceback 而不是校验结果。
    raw = _read_manifest_raw(Path(template))
    assets = raw.get("assets") if isinstance(raw.get("assets"), dict) else {}
    payload = {
        "ok": result["ok"],
        "schema": result["schema"],
        "vertical": raw.get("vertical") or result["vertical"],
        "version": raw.get("version"),
        "assets": {k: (a or {}).get("version") for k, a in assets.items()
                   if isinstance(a, dict)},
        "asset_versions": result["assets"],
        "violations": result["violations"],
        "warnings": result["warnings"],
        "install": result["install"],
        "capability": ({"summary": capability.get("summary"),
                        "fingerprint": capability.get("fingerprint"),
                        "checks": capability["checks"]}
                       if capability is not None else
                       {"adopted": False,
                        "note": "该行业线未采纳能力矩阵（仅 crossborder 采纳）"}),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_validation(result))
    if capability is not None and not capability["ok"]:
        # 能力面冲突在 JSON 里已按 CAPABILITY_* 码列出；人读面照旧给 lint 摘要。
        print(render_lint(capability), file=sys.stderr)
    return 0 if result["ok"] else 1


def _read_manifest_raw(root: Path) -> dict:
    """只读清单原文（不校验）——校验失败时仍要能报 vertical/version 等展示字段。"""
    try:
        return json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _run_install_check(template: str, as_json: bool) -> int:
    """安装门：角色绑定候选集为空即阻断，点名角色与原因（task 3.5）。"""
    from .bindings import check_template_role_bindings, render_role_block

    blocked = check_template_role_bindings(template)
    if as_json:
        print(json.dumps({"ok": not blocked, "template": template,
                          "blocked": blocked}, ensure_ascii=False, indent=2))
    else:
        print(render_role_block(blocked))
    return 0 if not blocked else 1


def _run_new_vertical(args) -> int:
    """铸造新模板骨架（task 2.1 / 2.2）：生成后立刻过校验。"""
    from .foundry import scaffold_template

    result = scaffold_template(
        args.dest, vertical=args.vertical, name_zh=args.name_zh,
        name_en=args.name_en, description_zh=args.description_zh,
        description_en=args.description_en, version=args.version,
        with_pricing_rules=args.with_pricing_rules,
        with_pagesets=args.with_pagesets)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _run_publish_notes(template: str | None, *, write: bool, as_json: bool) -> int:
    """从发布记录派生三处注记（task 2.6）：默认只报差异，`--write` 才落盘。"""
    from .publish import (DEFAULT_RECORD_PATH, apply_notes, check_notes,
                          load_record)

    record = load_record()
    targets = ([template] if template else
               sorted(str(REPO_ROOT / "templates" / key.split("/")[0])
                      for key in record["packs"] if "/" in key))
    payload: list[dict] = []
    failures = 0
    for tpl in targets:
        drift = check_notes(tpl, record=record)
        entry = {"template": tpl,
                 "drift": [{"code": d["code"], "place": d.get("place"),
                            "message": d["message"]} for d in drift]}
        if write and any(d.get("severity", "error") == "error" for d in drift):
            entry["applied"] = apply_notes(tpl, record=record)
        payload.append(entry)
        failures += sum(1 for d in drift if d.get("severity", "error") == "error")
    result = {"ok": failures == 0, "record": str(DEFAULT_RECORD_PATH),
              "templates": payload, "written": bool(write)}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("发布注记：" + ("一致" if result["ok"] else f"漂移 {failures} 处"))
        for item in payload:
            for d in item["drift"]:
                print(f"  x [{d['code']}] {item['template']} {d['place']}: {d['message']}")
            if item.get("applied"):
                print(f"  → 已重写 {item['template']}：{item['applied']['changed']}")
    return 0 if result["ok"] or write else 1


def _run_publish_pack(template: str, *, author: str, dry_run: bool,
                      as_json: bool) -> int:
    """封装兄弟仓 publish_pack.py（task 2.7）：只做 dry-run，不透传真发版。"""
    from .publish import render_publish_result, run_publish_tool

    result = run_publish_tool(template, author=author, dry_run=dry_run)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_publish_result(result))
    return 0 if result["ok"] else 1


def _run_publish_gate(doc: str | None, as_json: bool) -> int:
    """发布口径门（task 2.8）：记录 ↔ 三处注记 ↔ 文档表格 ↔ 市场台账。"""
    from .publish import check_docs, check_fleet_ledger, check_notes, load_record

    record = load_record()
    violations: list[dict] = []
    for key in record["packs"]:
        tpl = REPO_ROOT / "templates" / key.split("/")[0]
        if not tpl.is_dir():
            violations.append({"code": "PUBLISH_TEMPLATE_MISSING",
                               "message": f"发布记录里的 {key} 找不到模板目录 {tpl}"})
            continue
        for item in check_notes(tpl, record=record):
            violations.append({**item, "template": key})
    doc_path = Path(doc) if doc else (REPO_ROOT / "docs" / "connector-onboarding-supply-chain.md")
    violations.extend(check_docs(doc_path, record=record))
    ledger = check_fleet_ledger(record=record)
    violations.extend(ledger["violations"])
    errors = [v for v in violations if v.get("severity", "error") == "error"]
    result = {"ok": not errors, "doc": str(doc_path),
              "ledger": ledger.get("ledger"),
              "ledger_verified": ledger.get("verified"),
              "violations": errors,
              "warnings": [v for v in violations if v.get("severity") == "warning"],
              "checks": len(record["packs"]) + 2}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("发布口径门：" + ("通过" if result["ok"] else f"阻断（{len(errors)} 处）"))
        if not ledger.get("verified"):
            print(f"  ! 市场台账未核对：{ledger.get('reason')}")
        for item in result["violations"]:
            print(f"  x [{item['code']}] {item.get('template') or item.get('path') or ''}")
            print(f"      {item['message']}")
        for item in result["warnings"]:
            print(f"  ! [{item['code']}] {item['message']}")
    return 0 if result["ok"] else 1


def _run_gaps(check_flag: bool, as_json: bool, catalog_dir: str | None) -> int:
    """缺口登记 + 跨仓回填校验（task 3.8）。"""
    from .gaps import check as gaps_check
    from .gaps import render

    result = gaps_check(catalog_dir=catalog_dir)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0 if result["ok"] else 1



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


def _run_release_check(template: str, ledger: str, out: str | None,
                       build: bool, dry_run: bool, as_json: bool) -> int:
    from .release import appliance_build_runner, render_gate, run_release_gate

    result = run_release_gate(
        template, ledger_path=ledger, out_dir=out,
        build_runner=appliance_build_runner if build else None,
        record=not dry_run)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_gate(result))
    return 0 if result["ok"] else 1


def _run_capability_check(template: str, docs: list[str] | None,
                          as_json: bool) -> int:
    from .capability_check import check_template, render_lint

    doc_paths = docs if docs else _default_capability_docs()
    result = check_template(template, doc_paths=doc_paths)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_lint(result))
    return 0 if result["ok"] else 1


def _default_capability_docs(workspace: Path | None = None,
                             snapshot_root: Path | None = None) -> list[Path]:
    """对外能力表述文档：活文件优先，**仓内快照兜底**。

    这些文档的主副本在工作区根下的非 git 目录。原先只认活文件：干净检出时四个
    文件全不在 → 锚点 0 个而门仍然绿，等于「没扫到 = 没问题」（本仓踩过的老坑）。
    仓里同时留了同日快照（与主副本逐字节相等，另有测试守着），所以活文件缺席时
    改读快照——CI 里这道门才真的在验，而不是空转。

    两个参数只为可测：默认值就是真实工作区与真实快照目录。
    """
    workspace = workspace or REPO_ROOT.parent
    snapshot_root = snapshot_root or REPO_ROOT / "docs" / "collateral-snapshots"
    pairs = (
        ("docs/crossborder-supply-map.md", "crossborder-supply-map.md"),
        ("docs/presales-yichang/bossai.md", "bossai.md"),
        ("collateral-erp/demo-guide-crossborder.md", "demo-guide-crossborder.md"),
        ("collateral-solution/00-方案说明.md", "collateral-solution-00-方案说明.md"),
    )
    out: list[Path] = []
    for live_rel, snap_name in pairs:
        live = workspace / live_rel
        if live.is_file():
            out.append(live)
            continue
        # 快照目录按日期命名，取最新的那个——写死日期的话下次留档就失效了
        candidates = sorted(snapshot_root.glob(f"*/{snap_name}"))
        if candidates:
            out.append(candidates[-1])
    return out


def _run_coverage(action: str, args) -> int:
    """覆盖矩阵（optimize-erp-solution tasks 1.1-1.7）：证据 / 派生 / 校验 /
    分层导出 / 受控导入。全只读（`build|export|import|evidence` 只写 coverage/）。"""
    from . import coverage as cov

    if action == "evidence":
        snapshot = cov.build_evidence_snapshot(
            api_package=args.api_package, tool_surface=args.tool_surface)
        target = Path(args.out or cov.DEFAULT_EVIDENCE_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "evidence": str(target),
                          "counts": snapshot["counts"]},
                         ensure_ascii=False, indent=2))
        return 0

    evidence = cov.load_evidence(args.evidence) if args.evidence else None

    if action == "verify":
        # 快照 vs 现测：兄弟仓不在位时如实标 unverified，不假装绿。
        try:
            live = cov.build_evidence_snapshot(api_package=args.api_package,
                                               tool_surface=args.tool_surface)
        except Exception as exc:  # 证据源不在位：import/文件缺失都归到「未验证」
            print(json.dumps({"ok": True, "verified": False,
                              "reason": f"跨仓证据源不在位，跳过快照核对：{exc}"},
                             ensure_ascii=False, indent=2))
            return 0
        committed = cov.load_evidence(args.evidence)
        drift: list[str] = []
        old_routes = {(r.method, r.path) for r in committed.routes.values()}
        new_routes = {(r["method"], r["path"]) for r in live["routes"]}
        for method, path in sorted(new_routes - old_routes):
            drift.append(f"新增路由 {method} {path}（快照未重算）")
        for method, path in sorted(old_routes - new_routes):
            drift.append(f"删除路由 {method} {path}（快照未重算）")
        old_tools, new_tools = set(committed.mcp_tools), set(live["mcp_tools"])
        for tool in sorted(new_tools - old_tools):
            drift.append(f"新增 MCP 工具 {tool}（快照未重算）")
        for tool in sorted(old_tools - new_tools):
            drift.append(f"删除 MCP 工具 {tool}（快照未重算）")
        print(json.dumps({"ok": not drift, "verified": True,
                          "counts": live["counts"], "drift": drift},
                         ensure_ascii=False, indent=2))
        return 1 if drift else 0

    if action == "import":
        if not (args.external and args.out and args.mapping):
            print("coverage import 需要 --external --out --mapping（映射必须落仓外）",
                  file=sys.stderr)
            return 2
        result = cov.import_external_list(
            args.external, out_matrix=args.out, out_mapping=args.mapping,
            salt=args.salt, evidence=evidence)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0

    matrix_path = args.matrix
    if action == "build":
        if not args.template:
            print("coverage build 需要 --template", file=sys.stderr)
            return 2
        index = evidence or cov.load_evidence()
        matrix = cov.build_template_matrix(args.template, index)
        cov.validate_matrix(matrix, evidence=index)
        target = Path(args.out or (cov.DEFAULT_MATRIX_DIR / f"{matrix.vertical}.json"))
        cov.save_matrix(matrix, target)
        print(json.dumps({"ok": True, "matrix": str(target),
                          "counts": cov.coverage_counts(matrix)},
                         ensure_ascii=False, indent=2))
        return 0

    if action == "validate":
        if not matrix_path:
            print("coverage validate 需要 --matrix", file=sys.stderr)
            return 2
        matrix = cov.load_matrix(matrix_path)
        cov.validate_matrix(matrix, evidence=evidence)
        print(json.dumps({"ok": True, "matrix": str(matrix_path),
                          "fingerprint": cov.matrix_fingerprint(matrix),
                          "counts": cov.coverage_counts(matrix)},
                         ensure_ascii=False, indent=2))
        return 0

    if action == "export":
        if not matrix_path:
            print("coverage export 需要 --matrix", file=sys.stderr)
            return 2
        matrix = cov.load_matrix(matrix_path)
        claims = []
        if args.claims:
            claims = json.loads(Path(args.claims).read_text(encoding="utf-8"))
            if isinstance(claims, dict):
                claims = claims.get("claims") or []
        out_dir = Path(args.out or cov.DEFAULT_LAYERS_DIR)
        index = cov.export_layers(matrix, out_dir, commercial_claims=claims)
        print(json.dumps({"ok": True, "out_dir": str(out_dir), **index},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"未知 coverage action：{action}", file=sys.stderr)
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(prog="fd-erp-vertical-templates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit", help="模板边界审计（机械化，只读）")
    val = sub.add_parser("validate", help="校验模板四件套清单")
    val.add_argument("--template", required=True)
    val.add_argument("--json", action="store_true", default=True,
                     help="机器可读结果（默认；两次运行逐字节一致）")
    val.add_argument("--install", action="store_true",
                     help="同时按安装门判定（角色候选集为空压红；等同 install-check）")
    inst = sub.add_parser("install-check",
                          help="安装门：角色绑定候选集从目录实测派生，空即阻断")
    inst.add_argument("--template", required=True)
    inst.add_argument("--json", action="store_true")
    new = sub.add_parser("new-vertical", help="铸造新行业模板骨架（生成后立刻过校验）")
    new.add_argument("--dest", required=True, help="目标目录（须不存在或为空）")
    new.add_argument("--vertical", required=True, help="行业键（小写 kebab-case）")
    new.add_argument("--name-zh", required=True)
    new.add_argument("--name-en", required=True)
    new.add_argument("--description-zh", required=True)
    new.add_argument("--description-en", required=True)
    new.add_argument("--version", default=None, help="资产版本（缺省 0.1.0）")
    new.add_argument("--with-pricing-rules", action="store_true",
                     help="同时生成取价规则 pack（可选扩展件）")
    new.add_argument("--with-pagesets", action="store_true",
                     help="同时生成页面集 pack（可选扩展件）")
    pn = sub.add_parser("publish-notes",
                        help="从发布记录派生 pack 三处注记（默认只报差异，--write 落盘）")
    pn.add_argument("--template", default=None, help="缺省记录里全部模板")
    pn.add_argument("--write", action="store_true")
    pn.add_argument("--json", action="store_true")
    pp = sub.add_parser("publish-pack",
                        help="封装兄弟仓 publish_pack.py（只做 dry-run，不透传真发版）")
    pp.add_argument("--template", required=True)
    pp.add_argument("--author", required=True)
    pp.add_argument("--dry-run", action="store_true", default=True)
    pp.add_argument("--json", action="store_true")
    pg = sub.add_parser("publish-gate",
                        help="发布口径门：记录 ↔ 三处注记 ↔ 文档表格 ↔ 市场台账")
    pg.add_argument("--doc", default=None)
    pg.add_argument("--json", action="store_true")
    gp = sub.add_parser("gaps", help="连接器缺口登记与跨仓回填校验")
    gp.add_argument("--check", action="store_true", default=True)
    gp.add_argument("--catalog-dir", default=None, help="目录快照根（测试/复现用）")
    gp.add_argument("--json", action="store_true")
    cap = sub.add_parser("capability-check",
                         help="能力面一致性检查（矩阵 ↔ 产物 ↔ 对外文档锚点）")
    cap.add_argument("--template", required=True)
    cap.add_argument("--doc", action="append", default=None,
                     help="带锚点的对外文档（可多次；缺省用仓库内已知四处）")
    cap.add_argument("--json", action="store_true")
    rel = sub.add_parser("release-check",
                         help="发布门：能力面 → 模板清单 → 资产摘要/版本单调 →（可选）出包验证")
    rel.add_argument("--template", required=True)
    rel.add_argument("--ledger", default="releases/ledger.jsonl")
    rel.add_argument("--out", default=None, help="出包输出目录（配合 --build）")
    rel.add_argument("--build", action="store_true",
                     help="跑 appliance 出包验证（需 PYTHONPATH 含 fd-appliance）")
    rel.add_argument("--dry-run", action="store_true", help="只跑门，不写台账")
    rel.add_argument("--json", action="store_true")
    orp = sub.add_parser("orphans", help="孤儿信用占用报表（只读，永不自动释放）")
    orp.add_argument("--url", required=True, help="内核 MCP 工具面地址")
    orp.add_argument("--data-dir", default="./data", help="模板层事件档目录（读订单侧）")
    orp.add_argument("--tenant-id", default="default")
    orp.add_argument("--legacy-ceiling-days", type=int, default=None,
                     help="存量行（无 expires_at）判定上界，缺省读 ERP_CREDIT_LEGACY_CEILING_DAYS")
    orp.add_argument("--json", action="store_true")
    covp = sub.add_parser("coverage", help="功能点覆盖矩阵：证据枚举 / 派生 / 校验 / 分层导出 / 受控导入")
    covp.add_argument("action", choices=("evidence", "build", "validate", "verify",
                                         "export", "import"))
    covp.add_argument("--template", help="模板目录（build 用）")
    covp.add_argument("--matrix", help="矩阵文件（validate/export 用）")
    covp.add_argument("--out", help="输出路径：证据快照 / 矩阵 / 分层目录 / 导入矩阵")
    covp.add_argument("--evidence", help="跨仓证据快照（缺省 coverage/evidence.json）")
    covp.add_argument("--api-package", help="服务端端点包目录（缺省兄弟仓 fd-erp-vertical-api）")
    covp.add_argument("--tool-surface", help="MCP 工具面 tool-surface.json（缺省兄弟仓）")
    covp.add_argument("--external", help="外部功能清单（import；MUST 位于仓外）")
    covp.add_argument("--mapping", help="脱敏编号→原文映射（import；MUST 位于仓外）")
    covp.add_argument("--salt", help="编号 salt（import；缺省复用映射文件内 salt）")
    covp.add_argument("--claims", help="价格/工期/规模数字出处清单（export 用）")
    args = parser.parse_args()
    if args.cmd == "audit":
        sys.exit(_run_audit())
    if args.cmd == "validate":
        sys.exit(_run_validate(args.template, as_json=args.json,
                               install_gate=args.install))
    if args.cmd == "install-check":
        sys.exit(_run_install_check(args.template, args.json))
    if args.cmd == "new-vertical":
        if not args.version:
            from .foundry import DEFAULT_VERSION
            args.version = DEFAULT_VERSION
        sys.exit(_run_new_vertical(args))
    if args.cmd == "publish-notes":
        sys.exit(_run_publish_notes(args.template, write=args.write,
                                    as_json=args.json))
    if args.cmd == "publish-pack":
        sys.exit(_run_publish_pack(args.template, author=args.author,
                                   dry_run=args.dry_run, as_json=args.json))
    if args.cmd == "publish-gate":
        sys.exit(_run_publish_gate(args.doc, args.json))
    if args.cmd == "gaps":
        sys.exit(_run_gaps(args.check, args.json, args.catalog_dir))
    if args.cmd == "capability-check":
        sys.exit(_run_capability_check(args.template, args.doc, args.json))
    if args.cmd == "release-check":
        sys.exit(_run_release_check(args.template, args.ledger, args.out,
                                    args.build, args.dry_run, args.json))
    if args.cmd == "orphans":
        sys.exit(_run_orphans(args.url, args.data_dir, args.tenant_id, args.json,
                              args.legacy_ceiling_days))
    if args.cmd == "coverage":
        sys.exit(_run_coverage(args.action, args))


if __name__ == "__main__":
    main()

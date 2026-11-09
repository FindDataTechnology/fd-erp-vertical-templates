#!/usr/bin/env python3
"""上架就绪清单（change `ship-ecommerce-commercial` 任务 2.x 的前置计算）。

回答的是「这次上架还缺什么」，而不是「我记得还缺什么」：

- 每个资产当前版本 vs 台账里最新版本的差（版本没 bump 就上不去）；
- 语言面：市场要 en·ja·es·fr，缺哪几门逐项列出（缺了上架门会拒）；
- 资产类型：哪些是市场上架单元（pack / fleet），哪些只是随模板分发；
- 台账状态：`draft` 表示过了发布门但还没上架；`blocked` 表示门未过。

输出人读表格 + Markdown（`releases/READINESS.md`），可复跑。
用法：python3 scripts/release_readiness.py [--template templates/crossborder]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fd_erp_vertical_templates.release import Ledger, artifact_digest  # noqa: E402

#: 市场上架要求覆盖的语言（fleet-packs 台账口径：en·ja·es·fr）。
REQUIRED_LOCALES = ("en", "ja", "es", "fr")
#: 属于「市场上架单元」的资产（其余随模板分发，不单独上架）。
MARKET_UNITS = ("connector-pack", "account-mapping-pack", "recon-fleet")


def _locales(pack: dict) -> set[str]:
    """已覆盖语言：顶层 name/description 视为 zh，另加 i18n 的键。"""
    langs = {"zh"}
    for key in ("name_en", "description_en"):
        if pack.get(key):
            langs.add("en")
    langs |= set((pack.get("i18n") or {}).keys())
    return langs


def build_report(template_dir: Path, ledger_path: Path) -> dict:
    manifest = json.loads((template_dir / "manifest.json").read_text(encoding="utf-8"))
    ledger = Ledger(ledger_path)
    rows = []
    for asset_id, rel in (
        ("capability-matrix", "capability-matrix.json"),
        ("connector-pack", "connector-pack/pack.json"),
        ("report-preset", "report-preset/preset.json"),
        ("account-mapping-pack", "account-mapping/pack.json"),
        ("recon-fleet", "recon-fleet/template.json"),
    ):
        path = template_dir / rel
        if not path.is_file():
            rows.append({"asset_id": asset_id, "path": rel, "missing_file": True})
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        version = ""
        for asset in (manifest.get("assets") or {}).values():
            if str(asset.get("path") or "") == rel:
                version = str(asset.get("version") or "")
        last = ledger.latest(asset_id)
        langs = _locales(body)
        digest = artifact_digest(path)
        rows.append({
            "asset_id": asset_id,
            "path": rel,
            "version": version,
            "digest": digest,
            "market_unit": asset_id in MARKET_UNITS,
            "locales": sorted(langs),
            "missing_locales": [l for l in REQUIRED_LOCALES if l not in langs],
            "ledger_version": last.version if last else None,
            "ledger_status": last.status if last else None,
            # 同版本且同摘要 = 内容已入账，下一步是「上架」而不是「bump」；
            # 摘要变了才是必须 bump（否则发布门会拒）。
            "content_changed_vs_ledger": bool(
                last and last.version == version and last.artifact_hash != digest),
        })
    return {"template": str(template_dir), "ledger": str(ledger_path), "rows": rows}


def render(report: dict) -> str:
    lines = ["| 资产 | 版本 | 台账 | 上架单元 | 语言 | 缺语言 | 待办 |",
             "|---|---|---|---|---|---|---|"]
    for row in report["rows"]:
        if row.get("missing_file"):
            lines.append(f"| {row['asset_id']} | — | — | — | — | — | 文件缺失：{row['path']} |")
            continue
        todos = []
        if row["market_unit"] and row["missing_locales"]:
            todos.append("补 " + "/".join(row["missing_locales"]))
        if row["content_changed_vs_ledger"]:
            todos.append(f"bump 版本（v{row['ledger_version']} 内容已变）")
        if row["market_unit"]:
            if row["ledger_status"] is None:
                todos.append("先过发布门")
            elif row["ledger_status"] == "draft":
                todos.append("走市场上架")
            elif row["ledger_status"] == "blocked":
                todos.append("门未过：先解阻断")
        elif row["ledger_status"] is None:
            todos.append("先过发布门（随模板分发）")
        lines.append(
            f"| {row['asset_id']} | v{row.get('version') or '—'} | "
            f"{row.get('ledger_status') or '—'}"
            f"{' v' + row['ledger_version'] if row.get('ledger_version') else ''} | "
            f"{'是' if row['market_unit'] else '否（随模板）'} | "
            f"{'/'.join(row['locales'])} | "
            f"{'/'.join(row['missing_locales']) or '—'} | "
            f"{'；'.join(todos) or '就绪'} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release_readiness")
    ap.add_argument("--template", default=str(REPO / "templates" / "crossborder"))
    ap.add_argument("--ledger", default=str(REPO / "releases" / "ledger.jsonl"))
    ap.add_argument("--write", action="store_true",
                    help="同时写 releases/READINESS.md")
    args = ap.parse_args(argv)

    report = build_report(Path(args.template), Path(args.ledger))
    table = render(report)
    print(table)

    pending = [r for r in report["rows"] if r.get("missing_locales")
               or r.get("ledger_status") != "published"]
    if args.write:
        out = REPO / "releases" / "READINESS.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "# 上架就绪清单（自动生成，勿手改）\n\n"
            f"> 模板：`{Path(args.template).name}` · 台账：`{Path(args.ledger).name}`\n"
            f"> 市场要求语言：{'/'.join(REQUIRED_LOCALES)}\n\n"
            + table + "\n\n"
            "## 口径\n\n"
            "- 「上架单元」= 市场上单独发布的资产（pack / fleet）；能力矩阵与报表 preset 随模板分发。\n"
            "- 语言缺项按 `i18n` 键判定，顶层 name/description 视为中文。\n"
            "- 版本与台账一致时不能重发同内容（发布门会拒），必须 bump。\n",
            encoding="utf-8")
        print(f"\n已写 {out}")
    return 0 if not pending else 0   # 清单是信息，不是门


if __name__ == "__main__":
    raise SystemExit(main())

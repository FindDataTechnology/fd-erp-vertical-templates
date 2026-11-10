#!/usr/bin/env python3
"""入口矩阵（spec `ecommerce-release-gates`，任务 4.2；原 change `ship-ecommerce-commercial` 已于 2026-11-10 归档）。

对外入口只认台账，不认「我觉得已经发了」：

- `published` → 可列试用；
- `draft` → **未发布**，不得出现在试用列表（这是本脚本存在的理由：上架前就
  把入口挂出去，客户点一下打不开，比没有入口更伤）；
- `blocked` → 阻断，附外部原因。

看板入口另查产物是否存在（公开 spec + HTML）；只有 spec 没有 HTML 的算「未发布」，
与 `ecommerce-board-publication` 的口径一致。

用法：
  python3 scripts/entry_matrix.py            # 打印并写 releases/ENTRY-MATRIX.md
  python3 scripts/entry_matrix.py --check    # 只校验试用列表不含未发布项
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fd_erp_vertical_templates.release import Ledger  # noqa: E402

DASHBOARDS = Path(REPO).parent / "dashboards"
#: 部署记录（可选）：看板「已上线」的唯一证据来源。没有这张表 = 只有本地产物，
#: 而本地产物 ≠ 客户点得开。缺表时看板一律不入试用列表。
DEPLOYMENTS = REPO / "releases" / "deployments.jsonl"

#: (入口 id, 展示名, 类型, 台账 asset_id 或 None, 产物前缀)
ENTRIES = (
    ("connector-pack", "跨境电商连接器包", "pack", "connector-pack", None),
    ("account-mapping-pack", "跨境电商科目映射包", "pack", "account-mapping-pack", None),
    ("recon-fleet", "跨境电商对账舰队", "fleet", "recon-fleet", None),
    ("board-trade-profile", "跨境电商·外圈贸易画像", "board", None,
     "crossborder-trade-profile-public"),
    ("board-policy-heat", "跨境电商·政策热度", "board", None,
     "crossborder-policy-heat-public"),
    ("board-logistics", "跨境电商·物流压力", "board", None,
     "crossborder-logistics-pressure-public"),
    ("board-express", "国内电商·快递物流", "board", None,
     "express-logistics-public"),
    ("board-online-retail", "国内电商·网上零售（缺位板）", "board", None,
     "domestic-online-retail-public"),
)


def _board_state(prefix: str | None) -> str:
    if not prefix:
        return ""
    spec = DASHBOARDS / f"{prefix}.spec.json"
    html = DASHBOARDS / f"{prefix}.html"
    if spec.is_file() and html.is_file():
        return "产物齐（spec+html）"
    if spec.is_file():
        return "仅 spec，缺 html → 未发布"
    return "产物缺失"


def _deployed_ids() -> set[str]:
    if not DEPLOYMENTS.is_file():
        return set()
    ids = set()
    for line in DEPLOYMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        import json as _json
        try:
            body = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if body.get("status") in ("live", "published"):
            ids.add(str(body.get("entry_id")))
    return ids


def collect(ledger_path: Path) -> dict:
    ledger = Ledger(ledger_path)
    deployed_ids = _deployed_ids()
    rows = []
    for entry_id, label, kind, asset_id, prefix in ENTRIES:
        last = ledger.latest(asset_id) if asset_id else None
        status = last.status if last else "unpublished"
        version = last.version if last else ""
        board = _board_state(prefix)
        # 看板只要有产物就算「已发布候选」，但台账没有它们（看板是静态产物，
        # 不走 pack 台账）——所以看板状态以产物为准，README 里如实写「静态产物」。
        if kind == "board":
            built = "产物齐" in board
            deployed = entry_id in deployed_ids
            # 三态分开：没产物 / 产物就绪待上线 / 已上线。
            # 只有「已上线」才进试用列表——本地产物客户点不开。
            status = ("published" if deployed else
                      "built" if built else "unpublished")
            published = deployed
            version = ""
        else:
            published = status == "published"
        rows.append({
            "entry_id": entry_id, "label": label, "kind": kind,
            "version": version, "status": status,
            "trial": published, "board": board,
            "external_reason": getattr(last, "external_reason", "") if last else "",
        })
    return {"ledger": str(ledger_path), "rows": rows}


def render(data: dict) -> str:
    lines = ["# 对外入口矩阵（派生件，勿手改）", "",
             f"> 台账：`{Path(data['ledger']).name}` · 试用列表只收 `published`。", "",
             "| 入口 | 类型 | 版本 | 状态 | 进试用列表 | 备注 |", "|---|---|---|---|---|---|"]
    for r in data["rows"]:
        note = r["board"] or r["external_reason"] or "—"
        lines.append(f"| {r['label']} | {r['kind']} | {('v'+r['version']) if r['version'] else '—'} "
                     f"| {r['status']} | {'是' if r['trial'] else '否'} | {note} |")
    lines += ["", "## 口径", "",
              "- 市场单元（pack/fleet）状态取台账最新一行：`draft` = 未发布，`published` = 已上架。",
              "- 看板三态：`unpublished`（无产物）/ `built`（产物就绪，未上线）/ "
              "`published`（已上线，需 `releases/deployments.jsonl` 里的 live 记录）。",
              "- **本地产物 ≠ 客户点得开**：没有部署记录一律不入试用列表。",
              "- 未发布入口**不得**出现在任何试用列表；上架后由台账翻 `published` 再刷新本表。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="entry_matrix")
    ap.add_argument("--ledger", default=str(REPO / "releases" / "ledger.jsonl"))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="校验：所有 draft/blocked 入口都不在试用列表")
    args = ap.parse_args(argv)

    data = collect(Path(args.ledger))
    text = render(data)
    print(text)

    if args.check:
        leaked = [r for r in data["rows"] if r["trial"] and r["status"] != "published"]
        if leaked:
            print(f"FAIL：{len(leaked)} 个未发布入口被列进试用列表："
                  + ", ".join(r["entry_id"] for r in leaked))
            return 1
        print(f"OK：{sum(1 for r in data['rows'] if r['trial'])} 个入口可试用，"
              f"{sum(1 for r in data['rows'] if not r['trial'])} 个未发布且已排除")
        return 0

    if args.write:
        out = REPO / "releases" / "ENTRY-MATRIX.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\n已写 {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

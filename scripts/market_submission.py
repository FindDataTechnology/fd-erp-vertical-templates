#!/usr/bin/env python3
"""导出市场上架提交材料（change `ship-ecommerce-commercial` 任务 2.x 的交付面）。

上架后台要逐语言填 name / description / tags。手工从 JSON 里抄，抄错一个字母
就把「需一次接入」说成「已支持」；所以这里直接从资产的 `i18n` 块取值导出，
导出文件本身是**派生件**，改了资产重跑即得，不允许手改。

两条自检（不合格直接拒，不出文件）：
1. 四语齐全（en/ja/es/fr）——市场门要求；
2. tags 数组长度逐语言一致——上架时按同长数组校验，长度不一会被拒。

用法：
  python3 scripts/market_submission.py            # 打印并写 releases/MARKET-SUBMISSION.md
  python3 scripts/market_submission.py --check    # 只校验，不写
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "crossborder"
OUT = REPO / "releases" / "MARKET-SUBMISSION.md"

LANGS = ("en", "ja", "es", "fr")
LANG_NAMES = {"zh": "中文（基准）", "en": "English", "ja": "日本語",
              "es": "Español", "fr": "Français"}

#: (asset_id, 资产相对路径, 说明)
UNITS = (
    ("connector-pack", "connector-pack/pack.json",
     "连接器 pack：能力面逐平台由 capability-matrix.json 生成"),
    ("account-mapping-pack", "account-mapping/pack.json",
     "科目映射 pack：呈现层口径，替换映射不改账"),
    ("recon-fleet", "recon-fleet/template.json",
     "对账舰队模板：巡检/核对/拍板三岗"),
)


def _load(rel: str) -> dict:
    return json.loads((TEMPLATE / rel).read_text(encoding="utf-8"))


def _zh_copy(body: dict) -> dict:
    """中文基准：顶层 name/description/tags 即中文口径。"""
    return {"name": body.get("name", ""),
            "description": body.get("description", ""),
            "tags": list(body.get("tags") or [])}


def collect() -> dict:
    rows, problems = [], []
    for asset_id, rel, note in UNITS:
        body = _load(rel)
        i18n = body.get("i18n") or {}
        langs = {lang: i18n.get(lang) for lang in LANGS}
        missing = [lang for lang, v in langs.items() if not v]
        if missing:
            problems.append(f"{asset_id}: 缺语言 {missing}（市场门要求 {'/'.join(LANGS)}）")
        tag_counts = {lang: len((v or {}).get("tags") or []) for lang, v in langs.items()}
        if missing:
            continue
        if len(set(tag_counts.values())) > 1:
            problems.append(
                f"{asset_id}: tags 长度逐语言不一致 {tag_counts}（上架按同长数组校验）")
        rows.append({
            "asset_id": asset_id,
            "path": rel,
            "version": str(body.get("version") or ""),
            "note": note,
            "zh": _zh_copy(body),
            "i18n": {lang: {"name": langs[lang]["name"],
                            "description": langs[lang]["description"],
                            "tags": list(langs[lang]["tags"])} for lang in LANGS},
            "tag_count": tag_counts,
        })
    return {"rows": rows, "problems": problems}


def render(data: dict) -> str:
    lines = [
        "# 上架提交材料（派生件，勿手改）",
        "",
        "> 由 `scripts/market_submission.py` 从资产 `i18n` 块导出；资产改动后重跑即得。",
        f"> 模板：`{TEMPLATE.name}` · 语言：zh（基准）/ en / ja / es / fr",
        "> 各平台的长原因不在此表：见随包分发的 `capability-matrix.json`（机器可读、可追溯）。",
        "",
    ]
    if data["problems"]:
        lines.append("## 自检未过")
        lines.append("")
        for p in data["problems"]:
            lines.append(f"- {p}")
        lines.append("")
        return "\n".join(lines)

    lines += ["## 一览", "",
              "| 资产 | 版本 | 路径 | tags 长度（逐语言） |",
              "|---|---|---|---|"]
    for row in data["rows"]:
        counts = "/".join(str(row["tag_count"][l]) for l in LANGS)
        lines.append(f"| {row['asset_id']} | v{row['version']} | `{row['path']}` | {counts} |")
    lines.append("")

    for row in data["rows"]:
        lines += [f"## {row['asset_id']} · v{row['version']}", "",
                  f"> {row['note']}", ""]
        for lang in ("zh",) + LANGS:
            block = row["zh"] if lang == "zh" else row["i18n"][lang]
            lines += [f"### {LANG_NAMES[lang]}（{lang}）", "",
                      "**name**", "", "```", block["name"], "```", "",
                      "**description**", "", "```", block["description"], "```", "",
                      "**tags**", "", "```",
                      " / ".join(block["tags"]), "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="market_submission")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    data = collect()
    text = render(data)
    if data["problems"]:
        print(text)
        return 1
    if args.check:
        print(f"OK：{len(data['rows'])} 件资产四语齐全、tags 同长")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"已写 {OUT.relative_to(REPO)}（{len(data['rows'])} 件 × 5 种语言）")
    for row in data["rows"]:
        print(f"  {row['asset_id']} v{row['version']} · tags {row['tag_count']['en']} 项 × 4 语")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

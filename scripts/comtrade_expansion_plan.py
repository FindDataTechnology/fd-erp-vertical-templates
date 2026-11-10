#!/usr/bin/env python3
"""un-comtrade 组合扩量规划器（C 3.1-3.3 的可测部分）。

扩量本身要花配额（免费档 500 次/日、每次调用=1 请求），所以真正难的不是「发请求」，
而是**组合矩阵、幂等键、断点续跑、对账**这四件事——它们在无配额时也能被完整验证。
真实批次执行留到有生产库与 key 时跑；本脚本负责把批次规划成可复跑的计划。

设计要点：
- 组合键 = `(reporter, period, cmd_code, flow_code, partner)`；同键重复规划只保留一条
  （幂等），跨批次重跑靠「已完成组合」清单跳过（断点续跑）。
- 计划里带 `budget` 与逐日切分：500 次/日硬上限在规划期就切好，不靠运行时自觉。
- 不做任何网络调用；`--check` 只校验计划与已完成清单是否自洽。

用法：
  python3 scripts/comtrade_expansion_plan.py --out /tmp/plan.json
  python3 scripts/comtrade_expansion_plan.py --check /tmp/plan.json --done /tmp/done.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

#: 免费档每日请求上限（README：500 次/天；每次调用 1 请求）。
DAILY_BUDGET = 500

#: 默认扩量矩阵：HS 码 × 伙伴国 × 年份（首批锚定 11-03 基线里的三个 HS 码）。
DEFAULT_HS = ("854142", "851762", "610910")
DEFAULT_PARTNERS = (0, 842, 276, 392, 826)   # 世界/美/德/日/英
DEFAULT_YEARS = ("2022", "2023", "2024", "2025")
REPORTER = 156                                # 中国
FLOWS = ("X", "M")                            # 出口/进口


@dataclass(frozen=True, order=True)
class Combo:
    """一个取数组合（= 一次上游请求）。"""

    reporter: int
    period: str
    cmd_code: str
    flow_code: str
    partner_code: int

    def key(self) -> str:
        return f"{self.reporter}|{self.period}|{self.cmd_code}|{self.flow_code}|{self.partner_code}"

    def as_dict(self) -> dict:
        return {"reporter": self.reporter, "period": self.period,
                "cmd_code": self.cmd_code, "flow_code": self.flow_code,
                "partner_code": self.partner_code, "key": self.key()}


def build_matrix(hs=DEFAULT_HS, partners=DEFAULT_PARTNERS, years=DEFAULT_YEARS,
                 flows=FLOWS, reporter: int = REPORTER) -> list[Combo]:
    """生成组合矩阵（去重 + 确定性排序）。排序固定是为了让批次可复现。"""
    combos = {
        Combo(reporter, year, code, flow, partner)
        for code in hs for partner in partners for year in years for flow in flows
    }
    return sorted(combos)


def split_by_day(combos: list[Combo], budget: int = DAILY_BUDGET) -> list[list[Combo]]:
    """按日切分：每批不超过 budget（免费档硬上限在规划期就切好）。"""
    if budget <= 0:
        raise ValueError("budget 必须为正")
    return [combos[i:i + budget] for i in range(0, len(combos), budget)]


def plan(combos: list[Combo], *, done: set[str] | None = None,
         budget: int = DAILY_BUDGET) -> dict:
    """产出批次计划：跳过已完成组合（断点续跑），其余按日切分。"""
    done = done or set()
    pending = [c for c in combos if c.key() not in done]
    batches = split_by_day(pending, budget)
    return {
        "total_combos": len(combos),
        "already_done": len(combos) - len(pending),
        "pending": len(pending),
        "budget_per_day": budget,
        "days_needed": len(batches),
        "batches": [{"day": i + 1, "count": len(b),
                     "combos": [c.as_dict() for c in b]}
                    for i, b in enumerate(batches)],
    }


def load_done(path: Path) -> set[str]:
    """已完成组合清单（JSONL，一行一个 key、{key: ...}，或纯文本 key）。缺文件=全未做。

    容错有边界：
    - 纯文本行（运维手写的断点清单）照收——只认 JSON 会把人工记录的断点当成
      「全未做」，重跑直接超配额；
    - 但**是 JSON 却不带 key 的行要报错**。早先这类行会被 `str(None)` 变成字面量
      "None" 塞进已完成集合：既不匹配任何组合，又让人以为这行被认了。
      配额按次计费，含糊的清单比没有清单更危险。
    """
    if not path.is_file():
        return set()
    out: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            out.add(line)
            continue
        if isinstance(body, dict):
            key = body.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ValueError(
                    f"{path}:{lineno} 是 JSON 对象但缺字符串 key：{line[:120]}"
                    "——已完成清单每行必须能定位到一个组合（key 或纯文本行）")
            out.add(key.strip())
        elif isinstance(body, str):
            out.add(body)
        else:
            raise ValueError(
                f"{path}:{lineno} 解析为 {type(body).__name__}（{line[:80]}），"
                "无法当作组合 key——只接受 key 字符串、{key: ...} 或纯文本行")
    return out


def check(plan_body: dict, combos: list[Combo], done: set[str]) -> list[str]:
    """计划自洽性检查：结构完整、不重复、不漏、不超预算、已完成项不重排。

    结构缺失按「问题」报告，不抛 KeyError——这道是门，门要给出可读结论。
    """
    problems: list[str] = []
    budget = plan_body.get("budget_per_day")
    if not isinstance(budget, int) or budget <= 0:
        problems.append(f"计划缺合法 budget_per_day（拿到 {budget!r}）")
        budget = 0
    batches = plan_body.get("batches")
    if not isinstance(batches, list):
        problems.append(f"计划缺 batches 列表（拿到 {type(batches).__name__}）")
        batches = []

    keys: list[str] = []
    for i, batch in enumerate(batches, start=1):
        if not isinstance(batch, dict):
            problems.append(f"第 {i} 批不是对象（{type(batch).__name__}）")
            continue
        items = batch.get("combos")
        if not isinstance(items, list):
            problems.append(f"第 {i} 批缺 combos 列表")
            continue
        actual = len(items)
        declared = batch.get("count")
        if declared is not None and actual != declared:
            problems.append(
                f"第 {i} 批声明 {declared} 条但实际 {actual} 条（清单与内容不一致）")
        if budget and actual > budget:
            problems.append(f"第 {i} 批 {actual} 条超过日预算 {budget}")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                problems.append(f"第 {i} 批里有条目缺字符串 key：{item!r}"[:120])
                continue
            keys.append(item["key"])
    if len(keys) != len(set(keys)):
        problems.append("计划内出现重复组合（幂等键失效）")
    if set(keys) & done:
        problems.append(f"计划里含已完成组合：{sorted(set(keys) & done)[:3]}")
    expected = {c.key() for c in combos} - done
    if set(keys) != expected:
        problems.append(
            f"计划与期望不一致：缺 {len(expected - set(keys))} 条、多 {len(set(keys) - expected)} 条")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="comtrade_expansion_plan")
    ap.add_argument("--out", default=None, help="写计划 JSON")
    ap.add_argument("--done", default=None, help="已完成组合清单（JSONL）")
    ap.add_argument("--check", default=None, help="校验既有计划文件")
    ap.add_argument("--budget", type=int, default=DAILY_BUDGET)
    args = ap.parse_args(argv)

    combos = build_matrix()
    try:
        done = load_done(Path(args.done)) if args.done else set()
    except ValueError as exc:
        print(f"已完成清单不可用：{exc}")
        return 1

    if args.check:
        body = json.loads(Path(args.check).read_text(encoding="utf-8"))
        problems = check(body, combos, done)
        if problems:
            print("计划不自洽：\n  " + "\n  ".join(problems))
            return 1
        print(f"OK：{body.get('pending')} 条待取 / {body.get('days_needed')} 批 / "
              f"预算 {body.get('budget_per_day')}/日")
        return 0

    body = plan(combos, done=done, budget=args.budget)
    text = json.dumps(body, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"已写 {args.out}：{body['pending']} 条待取 / {body['days_needed']} 批")
    else:
        print(text[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

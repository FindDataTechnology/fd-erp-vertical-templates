#!/usr/bin/env python3
"""国别映射覆盖闸（change: add-cn-localization-pack 任务 5.2，只读）。

它回答一个只能靠机器保证的问题：**内核每一个记账科目，是不是都能落到准则报表项目上。**

为什么必须成闸：漏项的报表照样平。新加一个内核科目（比如又补一个税费明细），忘了给它
映射或忘了把它挂进某个报表项目——资产负债表两边同时少一笔，合计仍然相等，利润表照样
出「净利润」，人眼看不出来。等到审计问「这笔钱在表上哪一行」，才发现表一直少一行。

判据四条（**两套准则各跑一遍**）：
1. 国别包自校验通过（条数、sha256、映射目标科目存在、行次科目与引用可解析）；
2. `statements.CHART` 的每个科目在两套准则里都有映射；
3. 每个映射目标都被该准则至少一个资产负债表或利润表项目覆盖（不覆盖即漏项）；
4. `statements.CHART` 与 `consolidation.CHART` 完全一致（分叉会让合并报表少科目）。

为什么要两套：同一科目码在两张准则表里语义不同（5401 小表是主营业务成本、大表是工程施工），
共用一份映射或只查一套，另一套就会静默少行——而报表照样平。

用法：python3 scripts/check_cn_localization.py [--json]
退出码：0 通过 / 1 有违规。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _covers(codes: list[str], target: str) -> bool:
    return any(target == c or target.startswith(c + ".") for c in codes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from fd_erp_vertical_templates.consolidation import CHART as CONSOLIDATION_CHART
    from fd_erp_vertical_templates.localization import CnPack
    from fd_erp_vertical_templates.statements import CHART as STATEMENTS_CHART

    problems: list[str] = []
    per_chart: dict[str, dict[str, int]] = {}

    for chart in ("accounts-small.json", "accounts-large.json"):
        try:
            pack = CnPack(chart=chart)
        except Exception as exc:                 # noqa: BLE001 - 加载失败即红
            problems.append(f"{chart} 加载失败：{exc}")
            continue
        label = pack.statements().get("chart") or chart
        mapping = pack.kernel_to_cn()
        rules = pack.statements()
        bs = rules["balanceSheet"]
        is_ = rules["incomeStatement"]
        all_lines = bs["assets"] + bs["liabilities"] + bs["equity"] + is_
        covered = [c for line in all_lines for c in (line.get("codes") or [])]
        per_chart[label] = {"kernelAccounts": len(STATEMENTS_CHART),
                            "mapped": len(mapping), "statementLines": len(all_lines)}

        # 2) 每个内核科目在该准则下都要有映射
        for code in sorted(STATEMENTS_CHART):
            if code not in mapping:
                problems.append(f"[{label}] 内核科目 {code} 没有映射到准则科目"
                                "——报表会静默少这一行")

        # 3) 每个映射目标都要被该准则的某个报表项目覆盖
        for code, target in sorted(mapping.items()):
            if code not in STATEMENTS_CHART:
                problems.append(f"[{label}] 映射里的 {code} 不是内核科目（内核里没有它）")
                continue
            if not _covers(covered, target):
                problems.append(
                    f"[{label}] 内核科目 {code} → 准则 {target}，"
                    "但该准则科目不在任何报表项目里（漏项）")

    # 4) 两张 CHART 必须一致
    if STATEMENTS_CHART != CONSOLIDATION_CHART:
        only_s = sorted(set(STATEMENTS_CHART) - set(CONSOLIDATION_CHART))
        only_c = sorted(set(CONSOLIDATION_CHART) - set(STATEMENTS_CHART))
        problems.append(f"statements.CHART 与 consolidation.CHART 分叉："
                        f"仅报表 {only_s}；仅合并 {only_c}")

    summary = {
        "ok": not problems,
        "kernelAccounts": len(STATEMENTS_CHART),
        "charts": per_chart,
        "problems": problems,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        if problems:
            print("国别映射覆盖闸：命中 " + str(len(problems)) + " 处")
            for p in problems:
                print("  ✗", p)
        else:
            detail = "；".join(f"{name}：{c['mapped']} 映射 / {c['statementLines']} 行"
                              for name, c in per_chart.items())
            print(f"国别映射覆盖闸：通过（{len(STATEMENTS_CHART)} 个内核科目在两套准则下"
                  f"均映射并落到报表项目——{detail}；两张 CHART 一致）")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""一次跑完全部一致性门（CI 与人工共用）。

为什么要有这个入口：门散在 CLI、scripts 与 tests/ 里。人的做法是记住三五个、
漏掉剩下几个——而漏掉的那道门，恰恰是防止「绿得名不副实」的那道。

纪律：
- **顺序固定**（先真源、后派生），前一道红了仍继续跑完后几道——一次看全，
  不要挤牙膏式修一个跑一次。
- 每道门打印 `ok/x` 与一行结果；末行给总账与退出码。
- 不自动修任何东西：门只报告，修由人决定。
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 有 venv 就用 venv 的解释器——pytest 类门只在那里可用；用 sys.executable
#: 会在「系统 python 没有 pytest」的机器上假红（本仓就踩过一次）。
def _python() -> str:
    for cand in (REPO / ".venv" / "bin" / "python",
                 REPO / ".venv" / "bin" / "python3"):
        if cand.is_file():
            return str(cand)
    return sys.executable


PY = _python()


@dataclass(frozen=True)
class Gate:
    name: str
    argv: tuple[str, ...]
    why: str


#: 顺序有意义：真源在前，派生件在后。
GATES: tuple[Gate, ...] = (
    Gate("能力矩阵自洽", ("-m", "fd_erp_vertical_templates.cli", "capability-check",
                      "--template", "templates/crossborder"),
         "矩阵 ↔ 产物 ↔ 对外文档锚点"),
    Gate("派生资产同步", ("scripts/gen_capability_artifacts.py", "--check"),
         "矩阵改了没重生成即红"),
    Gate("跨链互证", ("-m", "pytest", "tests/test_matrix_status_consistency.py", "-q"),
         "矩阵 ↔ 连接器对接三态"),
    Gate("发布注记", ("-m", "fd_erp_vertical_templates.cli", "publish-notes"),
         "三处注记 ↔ 发布记录"),
    Gate("发布门", ("-m", "fd_erp_vertical_templates.cli", "release-check",
                 "--template", "templates/crossborder",
                 "--ledger", "releases/ledger.jsonl"),
         "能力面/清单/摘要/版本单调/台账"),
    Gate("上架就绪", ("-m", "pytest", "tests/test_release.py", "-q", "-k", "derived_views"),
         "READINESS / ENTRY-MATRIX ↔ 台账"),
    Gate("提交材料", ("scripts/market_submission.py", "--check"),
         "四语齐全 + tags 同长 + 版本新鲜"),
    Gate("入口矩阵", ("scripts/entry_matrix.py", "--check"),
         "未发布不得进试用列表"),
    Gate("对接三态", ("scripts/build_connector_status.py", "--check"),
         "catalog / executor_tests / live 与派生文件一致"),
    Gate("对外声明", ("scripts/claim_gate.py", "check"),
         "能力声明不得越界（对方线，只读调用）"),
    # 最后一道：全套测试。上面十道是「单点门」，脚本级的；而门级断言大量写在
    # tests/ 里（骨架契约审计、人工门记录、快照一致、售前对齐、扩量规划…）。
    # 只跑其中两个文件等于「声称全部通过，实际七个门没跑」——本脚本自己踩过这个坑。
    Gate("全套测试", ("-m", "pytest", "tests", "-q"),
         "tests/ 下全部门级断言 + 既有回归"),
)


def main(argv: list[str] | None = None) -> int:
    only = set((argv or [])[1:])
    rows: list[tuple[str, bool, str]] = []
    for gate in GATES:
        if only and gate.name not in only:
            continue
        proc = subprocess.run([PY, *gate.argv], cwd=REPO,
                              capture_output=True, text=True)
        tail = [l for l in (proc.stdout or proc.stderr).strip().splitlines() if l.strip()]
        summary = tail[-1][:88] if tail else ""
        rows.append((gate.name, proc.returncode == 0, summary))

    width = max(len(r[0]) for r in rows) if rows else 0
    for name, ok, summary in rows:
        print(f"  {'ok ' if ok else 'x  '} {name:<{width}}  {summary}")
    failed = [r[0] for r in rows if not r[1]]
    print()
    if failed:
        print(f"共 {len(rows)} 道门，{len(failed)} 道红：{'、'.join(failed)}")
        return 1
    print(f"共 {len(rows)} 道门，全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

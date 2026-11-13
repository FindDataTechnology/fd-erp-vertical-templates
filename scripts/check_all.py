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
    # 矩阵的凭据（evidence ref）本身出过事：三条跨仓 ref 里两条指向无关代码，因为
    # 行号锚随编辑漂移而没人核。这道门禁行号锚、验符号真实存在。
    Gate("凭据可核", ("scripts/check_evidence_refs.py",),
         "evidence ref 禁行号锚 + 符号必须真实存在于所指文件"),
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
    # 本仓全套测试。上面是「单点门」；门级断言大量写在 tests/ 里（骨架契约审计、
    # 人工门记录、快照一致、售前对齐、扩量规划…）。只跑其中两个文件等于
    # 「声称全部通过，实际七个门没跑」——本脚本自己踩过这个坑。
    # 国别映射覆盖：内核每个记账科目都要落到准则报表项目上。漏项的报表照样平——
    # 资产负债表两边同时少一笔，利润表照样出「净利润」，人眼看不出来。
    Gate("国别映射覆盖", ("scripts/check_cn_localization.py",),
         "内核科目 ↔ 准则科目 ↔ 报表项目全覆盖；两张 CHART 不分叉"),
    Gate("本仓全套测试", ("-m", "pytest", "tests", "-q"),
         "tests/ 下全部门级断言 + 既有回归"),
    # 跨仓门：电商方案的门横跨两个仓，看板侧的门（公开面守卫、数据源红线）
    # 写在 ../dashboards/tests。不跑它们等于「本仓绿了就说全线绿了」。
    Gate("看板仓测试", ("-m", "pytest", "../dashboards/tests", "-q"),
         "公开面守卫 + 数据源红线（跨仓）"),
    # 演示脚本是销售要现场跑的东西，改坏 demo_tracks/矩阵/夹具只有到客户面前才暴露。
    # 两轨各跑一次（合计 <0.2s，无内核依赖），缺腿/合成数据标注都在断言范围内。
    Gate("演示双轨", ("../collateral-erp/scripts/demo_recon_only.py", "--track", "shopify"),
         "shopify 轨：真实矩阵说缺腿 + 假设形状"),
    Gate("演示双轨-国内", ("../collateral-erp/scripts/demo_recon_only.py", "--track", "domestic"),
         "domestic 轨：经商家 ERP 三腿"),
)


def _available(gate: "Gate") -> bool:
    """跨仓门在缺少兄弟仓时如实报「不可用」，不假装通过也不判红。"""
    for arg in gate.argv:
        if arg.startswith("../"):
            return (REPO / arg).exists()
    return True


def main(argv: list[str] | None = None) -> int:
    only = set((argv or [])[1:])
    rows: list[tuple[str, bool, str]] = []
    skipped: list[str] = []
    for gate in GATES:
        if only and gate.name not in only:
            continue
        if not _available(gate):
            skipped.append(gate.name)
            rows.append((gate.name, True, "不可用（兄弟仓不在本机），未执行"))
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
    if skipped:
        print(f"共 {len(rows)} 道门，全部通过（其中 {len(skipped)} 道因兄弟仓缺失未执行："
              f"{'、'.join(skipped)}）")
    else:
        print(f"共 {len(rows)} 道门，全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

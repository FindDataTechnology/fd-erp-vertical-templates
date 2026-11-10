"""交接页列的门 = 聚合器实际跑的门（防「文档说 12 道、实际 15 道」）。

交接页是下一个人唯一会读的东西。它的门清单曾经写着「门数不写死」却又逐条列了
命令——列了就必然过期：今天新增 `check_evidence_refs.py` 时它就没跟上，而没有任何
东西会响。

这里断言两件事（都只读仓内快照，干净检出也能跑）：
1. `check_all.py` 的每一道门，在交接页里都有对应入口；
2. 交接页里出现的每个 `scripts/*.py` / `cli <子命令>` 都真实存在，不是幻觉入口。

为什么读快照：交接页主副本在工作区根的非 git 目录，CI 里不在本机。快照是它的
逐字节副本（另有测试守着），所以对快照的断言等于对主副本的断言。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SNAP_ROOT = REPO / "docs" / "collateral-snapshots"


def _handoff_text() -> str:
    candidates = sorted(SNAP_ROOT.glob("*/ecommerce-solution-handoff.md"))
    if not candidates:
        pytest.fail("交接页快照缺失：docs/collateral-snapshots/<日期>/ecommerce-solution-handoff.md")
    return candidates[-1].read_text(encoding="utf-8")


def _gate_tokens() -> dict[str, str]:
    """把每道门映射成它在交接页里应出现的命令片段。"""
    from scripts.check_all import GATES

    out: dict[str, str] = {}
    for gate in GATES:
        argv = list(gate.argv)
        if "-m" in argv and "pytest" in argv:
            # 测试级门由交接页的 pytest 两行代表（cross-repo 与仓内）
            out[gate.name] = "../dashboards/tests" if "../dashboards" in " ".join(argv) else "pytest tests"
            continue
        script = next((a for a in argv if a.startswith("scripts/")), None)
        if script:
            out[gate.name] = script
            continue
        if "-m" in argv and "fd_erp_vertical_templates.cli" in argv:
            idx = argv.index("fd_erp_vertical_templates.cli")
            out[gate.name] = f"cli {argv[idx + 1]}"
            continue
        out[gate.name] = argv[-1]
    return out


def test_every_gate_is_documented_in_handoff():
    text = _handoff_text()
    missing = [f"{name}（{token}）"
               for name, token in _gate_tokens().items() if token not in text]
    assert not missing, (
        "交接页漏了这些门的入口（门加了但文档没跟上）：\n  "
        + "\n  ".join(missing)
        + "\n补进 docs/ecommerce-solution-handoff.md 的「各道门的独立入口」再 cp 同步快照")


def test_handoff_does_not_list_phantom_entries():
    """交接页里写的入口必须真实可跑——文档不能编出不存在的脚本或子命令。"""
    text = _handoff_text()
    from scripts.check_all import GATES  # noqa: F401  (确保路径可导入)

    problems: list[str] = []
    for script in sorted(set(re.findall(r"python3 (scripts/[\w./-]+\.py)", text))):
        if not (REPO / script).is_file():
            problems.append(f"脚本不存在：{script}")
    cli_known = _cli_subcommands()
    for cmd in sorted(set(re.findall(r"fd_erp_vertical_templates\.cli ([\w-]+)", text))):
        if cmd not in cli_known:
            problems.append(f"cli 子命令不存在：{cmd}")
    assert not problems, "交接页有幻觉入口：\n  " + "\n  ".join(problems)


def _cli_subcommands() -> set[str]:
    """从 cli.py 里扫出真实子命令名（add_parser 的第一参数）。"""
    src = (REPO / "fd_erp_vertical_templates" / "cli.py").read_text(encoding="utf-8")
    return set(re.findall(r'add_parser\(\s*"([\w-]+)"', src))

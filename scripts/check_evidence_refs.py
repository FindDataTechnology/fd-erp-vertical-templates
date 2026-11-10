#!/usr/bin/env python3
"""能力矩阵 evidence ref 的可用性门（`ecommerce-capability-matrix` 证据规则）。

## 为什么需要这道门

矩阵是本仓「哪条腿能读到」的唯一真源，每一格的 `evidence[].ref` 是它的凭据。
凭据本身出过事：三条跨仓 ref 里两条指向了无关代码——

    actions.ts:665  声称 list_orders，实际是 const locationConnectionInput
    actions.ts:669  声称 list_inventory_items，实际是 after: cursor,

原因是**行号锚会漂**：文件一编辑，行号指向别处，而 ref 看上去仍然合法，
没有任何东西会响。这和「判据挂在不稳定的东西上」是同一种错。

## 两条规则

1. **禁止行号锚**：`path:N` 形式直接判错，改用 `path#符号`。符号比行号稳定得多，
   而且能被机器验证「还在不在」。
2. **符号必须在文件里**：`#符号` 必须在该文件中真实出现（按词边界）。文件或符号
   找不到就报错，不是警告——凭据指不到东西，这条证据就等于没有。

## 兄弟仓缺席时

跨仓 ref（首段是兄弟仓目录名）在兄弟仓不在本机时**无法验证存在性**，如实报
「不可用（未验证）」并计入 summary，不假装通过、也不判红。格式规则（规则 1）
不依赖任何外部文件，任何机器都能跑。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fd_erp_vertical_templates.capability import load_matrix  # noqa: E402

MATRIX = REPO / "templates" / "crossborder" / "capability-matrix.json"

#: `path:123` —— 行号锚（禁止）。
_LINE_ANCHOR = re.compile(r":\d+$")

#: 非文件型证据的 scheme 前缀。`runtime:` = 一次真实探针的记录（不可按路径校验），
#: 只有这类 ref 允许不指向文件——它记的是「我打了哪个端点、拿到什么」，凭据在这里
#: 是那次观测本身。除此之外一律要求可解析到文件（有符号时还要能解析到符号）。
_SCHEMES = ("runtime:",)


#: 工作区里与跨仓 ref 相关的兄弟仓目录名。**按名单判定，与它是否在本机无关**：
#: 早先按「目录存在且是 git 仓」判定，结果兄弟仓缺席时 ref 被当成仓内相对路径 →
#: 文件不存在 → 门假红（CI 上必红）。名单制才能区分「真缺失」与「本机没检出」。
_KNOWN_SIBLINGS = ("open-connector-mt", "dashboards", "fd-appliance",
                   "fit", "fleet-packs", "fd-industry-data")


def _resolve(path_part: str) -> tuple[Path, str, bool]:
    """返回 (绝对路径, 归属说明, 是否跨仓)。

    跨仓与否只看首段是否在名单里——不看该仓是否检出，否则本机缺仓会被误判成
    「仓内路径写错」。
    """
    head, _, rest = path_part.partition("/")
    if head in _KNOWN_SIBLINGS:
        return REPO.parent / path_part, f"兄弟仓 {head}", True
    return REPO / path_part, "本仓", False


def check(matrix_path: Path = MATRIX) -> tuple[list[str], list[str]]:
    """返回 (错误清单, 未验证清单)。错误非空即门红。"""
    matrix = load_matrix(matrix_path)
    errors: list[str] = []
    unverified: list[str] = []
    seen: set[tuple[str, str]] = set()

    for (platform, flow), cell in sorted(matrix.cells.items()):
        for i, ev in enumerate(cell.evidence):
            ref = ev.ref
            where = f"{platform}.{flow} evidence[{i}] ({ev.kind})"
            if (where, ref) in seen:
                continue
            seen.add((where, ref))

            if _LINE_ANCHOR.search(ref):
                errors.append(
                    f"{where}: ref 用行号锚（{ref}）——行号会随编辑漂移，"
                    "改成 `<path>#<符号>`（符号可被机器验证）")
                continue

            scheme = next((s for s in _SCHEMES if ref.startswith(s)), None)
            if scheme:
                if not ref[len(scheme):].strip():
                    errors.append(f"{where}: `{scheme}` ref 后面是空的（{ref}）")
                continue  # 非文件型证据：只要求非空，不做路径校验

            path_part, sep, symbol = ref.partition("#")
            target, owner, cross = _resolve(path_part)
            repo_dir = REPO.parent / path_part.split("/", 1)[0]

            if cross and not repo_dir.exists():
                unverified.append(f"{where}: {ref}（{owner}不在本机，未验证存在性）")
                continue
            if not target.is_file():
                errors.append(f"{where}: ref 指向的文件不存在（{ref} → {target}）")
                continue
            if sep:
                if not symbol:
                    errors.append(f"{where}: ref 有 `#` 但没写符号（{ref}）")
                    continue
                text = target.read_text(encoding="utf-8", errors="replace")
                if not re.search(rf"\b{re.escape(symbol)}\b", text):
                    errors.append(
                        f"{where}: 符号 `{symbol}` 不在 {path_part} 里——"
                        "凭据指空了，改成真实符号或删掉这条证据")
    return errors, unverified


def main(argv: list[str] | None = None) -> int:
    matrix_path = Path(argv[1]) if argv and len(argv) > 1 else MATRIX
    errors, unverified = check(matrix_path)
    if errors:
        print("evidence ref 不合格：\n  " + "\n  ".join(errors))
        if unverified:
            print(f"另有 {len(unverified)} 条跨仓 ref 未验证（兄弟仓不在本机）")
        return 1
    if unverified:
        print(f"OK：ref 格式合规；{len(unverified)} 条跨仓 ref 因兄弟仓不在本机未验证存在性")
        for line in unverified:
            print(f"  - {line}")
    else:
        print("OK：全部 evidence ref 均可解析到真实符号/文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

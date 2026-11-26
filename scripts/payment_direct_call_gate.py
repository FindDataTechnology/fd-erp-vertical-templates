#!/usr/bin/env python3
"""支付直连调用门（optimize-erp-solution task 3.6 / design D6）。

`connectors/payments.py` 里的 generic HTTP 结算适配已被撤（改经连接器运行时
`wechatpay.download_bill` / `alipay.download_settlement_bill`）。本门守的是它
**不许回来**：模板包内任何新增的支付渠道直连 HTTP 调用都判红，并指名文件、行号
与所属载体。

判据（三条，任一命中即红）：
1. 模板包内 import 出网客户端（httpx / requests / aiohttp / urllib3 /
   urllib.request）——唯一允许的出网口是 `runtime_client.py`；
2. 出现支付渠道主机名字面量（api.mch.weixin.qq.com / openapi.alipay.com /
   unionpay / chinaums / 美团开放平台等）；
3. 出现直连调用形态（`httpx.post(` / `requests.get(` / `urlopen(` …）且不在允许面。

为什么按「新增调用」判而不是按「存在调用」判：撤掉适配后本应零命中——今后每
命中一条都是新引入的。门对**新增**报红，不靠人记得「不要再写回来」。

用法：
  python3 scripts/payment_direct_call_gate.py            # 门（退出码 0/1）
  python3 scripts/payment_direct_call_gate.py --json
  python3 scripts/payment_direct_call_gate.py --root <目录>   # 自定义扫描面（测试用）
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "fd_erp_vertical_templates"

HTTP_MODULES = {"httpx", "requests", "aiohttp", "urllib3"}
HTTP_RUNTIME_CALLS = {"httpx.post", "httpx.get", "httpx.request",
                      "requests.post", "requests.get", "requests.request",
                      "aiohttp.request", "urlopen", "urllib.request.urlopen"}

#: 支付渠道主机名（含常见沙箱/开放平台域名）。
PAYMENT_HOSTS = (
    "api.mch.weixin.qq.com",
    "api.weixin.qq.com",
    "openapi.alipay.com",
    "openapi.alipaydev.com",
    "openapi.unionpay.com",
    "gateway.95516.com",
    "chinaums.com",
    "open.meituan.com",
    "waimai.meituan.com",
    "pay.meituan.com",
    # PayPal（店面在线收款通道，establish-ai-native-ecommerce-stack 0.13）：
    # 出网唯一口是连接器运行时，模板层出现这些主机名 = 有人想绕过运行时直连。
    "api-m.paypal.com",
    "api-m.sandbox.paypal.com",
    "sandbox.paypal.com",
)

#: 允许面：文件相对路径 → 理由。出网单口就是这一个文件。
ALLOWLIST = {
    "runtime_client.py": "连接器运行时客户端（模板层唯一出网口，目标由 provider/action 决定）",
}


def scan(root: str | Path | None = None) -> list[dict]:
    """扫模板包，返回违规清单（空 = 通过）。"""
    base = Path(root) if root else PACKAGE
    violations: list[dict] = []
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base)
        allowed = rel.name in ALLOWLIST
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # 语法错的模块不猜，直接报（并指名行）
            violations.append({"code": "PAYMENT_GATE_UNPARSABLE",
                               "path": str(rel), "line": exc.lineno or 0,
                               "detail": str(exc),
                               "fix": "修语法后重跑"})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0]: a.name for a in node.names}
                for top, full in names.items():
                    hit = (top in HTTP_MODULES or full.startswith("urllib.request"))
                    if hit and not allowed:
                        violations.append(_v(
                            "PAYMENT_DIRECT_HTTP_IMPORT", rel, node.lineno,
                            f"import {full}",
                            "模板层不得自建出网；经 runtime_client.RuntimeClient 调 provider 动作"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                hit = (top in HTTP_MODULES or node.module.startswith("urllib.request"))
                if hit and not allowed:
                    violations.append(_v(
                        "PAYMENT_DIRECT_HTTP_IMPORT", rel, node.lineno,
                        f"from {node.module} import …",
                        "模板层不得自建出网；经 runtime_client.RuntimeClient 调 provider 动作"))
            elif isinstance(node, ast.Call):
                dotted = _dotted(node.func)
                if dotted in HTTP_RUNTIME_CALLS and not allowed:
                    violations.append(_v(
                        "PAYMENT_DIRECT_CALL", rel, node.lineno, f"{dotted}(…)",
                        "直连调用一律改经 ActionRef(provider, action, connection)"))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                for host in PAYMENT_HOSTS:
                    if host in lowered and not allowed:
                        violations.append(_v(
                            "PAYMENT_CHANNEL_HOST", rel, getattr(node, "lineno", 0),
                            node.value[:120],
                            "支付渠道端点只存在于连接器目录；模板层不持 base URL"))
                        break
    return violations


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _v(code: str, rel: Path, line: int, snippet: str, fix: str) -> dict:
    return {"code": code, "severity": "error", "path": str(rel), "line": line,
            "snippet": snippet, "fix": fix}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="支付直连调用门")
    ap.add_argument("--root", default=None, help="扫描根（缺省模板包）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    violations = scan(args.root)
    result = {"schema": "erp-payment-direct-call-gate/v1",
              "ok": not violations, "violations": violations,
              "allowlist": ALLOWLIST}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("支付直连门：" + ("通过（零直连调用）" if result["ok"]
                              else f"阻断（{len(violations)} 处）"))
        for item in violations:
            print(f"  x [{item['code']}] {item['path']}:{item['line']}  {item['snippet']}")
            print(f"      修法：{item['fix']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""模板包 CLI：边界审计自跑 + 模板清单校验（只读，零写路径）。

用法：
  fd-erp-vertical-templates audit                # 边界审计（包 + 资产）
  fd-erp-vertical-templates validate --template templates/crossborder
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .boundary import audit_template
from .manifest import load_template_manifest

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent


def _run_audit() -> int:
    report = audit_template(PKG_DIR, asset_dirs=[
        REPO_ROOT / "templates" / "crossborder",
        REPO_ROOT / "templates" / "restaurant-chain",
        REPO_ROOT / "templates" / "supply-chain",
    ])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _run_validate(template: str) -> int:
    manifest = load_template_manifest(Path(template))
    print(json.dumps({"ok": True, "vertical": manifest["vertical"],
                      "version": manifest["version"],
                      "assets": {k: a["version"] for k, a in manifest["assets"].items()}},
                     ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="fd-erp-vertical-templates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit", help="模板边界审计（机械化，只读）")
    val = sub.add_parser("validate", help="校验模板四件套清单")
    val.add_argument("--template", required=True)
    args = parser.parse_args()
    if args.cmd == "audit":
        sys.exit(_run_audit())
    if args.cmd == "validate":
        sys.exit(_run_validate(args.template))


if __name__ == "__main__":
    main()

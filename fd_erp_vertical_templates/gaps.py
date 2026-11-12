"""连接器缺口登记与跨仓回填校验（optimize-erp-solution task 3.8）。

缺口清单只此一处：`gaps.json`。每项写清责任仓、阻塞条件、当前状态与**可机器
判定的解除条件**（`detect`）——「已经补好了」这句话必须能被跨仓实测证实或推翻。

四条必填缺口（spec 场景点名）：
- `unionpay-no-provider`      银联无 provider
- `wms-no-provider`           客户 WMS 无 provider（角色绑定候选为空）
- `payments-not-wired`        支付有 provider 而 ERP 未接线
- `kingdee-no-executor-test`  金蝶执行器无测试

回填纪律：`detect` 判定为「已解除」而登记仍写 open → 判红（本仓没回填）；
登记写 resolved 而 `detect` 判定仍未解除 → 也判红（虚报修好）。跨仓源不在位
（没检出 open-connector-mt）时如实记 unverified——不假装绿，也不因为别人仓库
不在位而红。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import TemplateError

REPO = Path(__file__).resolve().parent.parent
FINDDATA = REPO.parent
DEFAULT_GAPS_PATH = REPO / "gaps.json"
DEFAULT_STATUS_PATH = REPO / "connector-status.json"
DEFAULT_CATALOG_DIR = FINDDATA / "open-connector-mt" / "catalog" / "apps"
DEFAULT_PROVIDERS_DIR = FINDDATA / "open-connector-mt" / "src" / "providers"

GAPS_SCHEMA = "erp-connector-gaps/v1"
REQUIRED_GAPS = ("unionpay-no-provider", "wms-no-provider",
                 "payments-not-wired", "kingdee-no-executor-test")
STATUSES = ("open", "resolved")


def load_gaps(path: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(path, dict):
        body = path
    else:
        target = Path(path) if path else DEFAULT_GAPS_PATH
        try:
            body = json.loads(target.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TemplateError("NOT_FOUND", f"缺口清单不可读：{target}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise TemplateError("MANIFEST_INVALID",
                                f"缺口清单不是合法 JSON（{target}）：{exc}") from exc
    _validate_structure(body)
    return body


def _validate_structure(body: Any) -> None:
    if not isinstance(body, dict) or body.get("schema") != GAPS_SCHEMA:
        raise TemplateError("MANIFEST_INVALID", f"缺口清单 schema 须为 {GAPS_SCHEMA}")
    gaps = body.get("gaps")
    if not isinstance(gaps, list) or not gaps:
        raise TemplateError("MANIFEST_INVALID", "缺口清单 gaps 须为非空数组")
    seen: set[str] = set()
    for i, gap in enumerate(gaps):
        if not isinstance(gap, dict):
            raise TemplateError("MANIFEST_INVALID", f"gaps[{i}] 须为对象")
        gap_id = str(gap.get("id") or "")
        if not gap_id:
            raise TemplateError("MANIFEST_INVALID", f"gaps[{i}] 缺 id")
        if gap_id in seen:
            raise TemplateError("MANIFEST_INVALID", f"缺口 id 重复：{gap_id}")
        seen.add(gap_id)
        if str(gap.get("status")) not in STATUSES:
            raise TemplateError("MANIFEST_INVALID",
                                f"{gap_id}.status 只能是 {'/'.join(STATUSES)}")
        for field in ("capability", "responsible_repo", "blocking", "fix"):
            if not str(gap.get(field) or "").strip():
                raise TemplateError("MANIFEST_INVALID", f"{gap_id}.{field} 必填")
        if not isinstance(gap.get("detect"), dict):
            raise TemplateError("MANIFEST_INVALID",
                                f"{gap_id}.detect 必填（没有机器判据就无法回填校验）")
    missing = [g for g in REQUIRED_GAPS if g not in seen]
    if missing:
        raise TemplateError(
            "MANIFEST_INVALID",
            f"缺口清单缺必填项：{', '.join(missing)}（四项缺口是能力清单的最小如实集）")


def check(*, gaps: str | Path | dict[str, Any] | None = None,
          catalog_dir: str | Path | None = None,
          providers_dir: str | Path | None = None,
          status: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    """逐缺口跑机器判据，返回 `{"ok", "gaps": [...], "unverified": [...]}`。"""
    body = load_gaps(gaps)
    catalog = Path(catalog_dir) if catalog_dir else DEFAULT_CATALOG_DIR
    providers = Path(providers_dir) if providers_dir else DEFAULT_PROVIDERS_DIR
    ctx = {
        "catalog": _catalog_providers(catalog),
        "providers": providers,
        "status": _load_status(status),
        "catalog_dir": catalog,
        "providers_dir": providers,
    }
    results: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    for gap in body["gaps"]:
        outcome = _evaluate(gap["detect"], ctx)
        item = {"id": gap["id"], "status": gap["status"],
                "capability": gap["capability"],
                "responsible_repo": gap["responsible_repo"],
                "detect": gap["detect"], "verdict": outcome["verdict"],
                "evidence": outcome.get("evidence")}
        results.append(item)
        if outcome["verdict"] == "unverified":
            unverified.append({"id": gap["id"], "reason": outcome["evidence"]})
            continue
        resolved_now = outcome["verdict"] == "resolved"
        if resolved_now and gap["status"] == "open":
            violations.append({
                "code": "GAP_BACKFILL_REQUIRED", "severity": "error",
                "gap": gap["id"],
                "message": (f"缺口 {gap['id']}（{gap['capability']}）在跨仓实测上已解除："
                            f"{outcome['evidence']}；本仓登记仍写 open——回填 gaps.json "
                            f"（status=resolved + resolved_at + evidence）"),
                "fix": "改 gaps.json 该条 status 为 resolved 并补证据，或说明为何仍阻塞",
            })
        elif not resolved_now and gap["status"] == "resolved":
            violations.append({
                "code": "GAP_STALE_RESOLUTION", "severity": "error",
                "gap": gap["id"],
                "message": (f"缺口 {gap['id']} 登记为 resolved，但跨仓实测仍未解除："
                            f"{outcome['evidence']}——虚报修好比不修更贵"),
                "fix": "把该条改回 open，或修好跨仓条件后重跑",
            })
    ok = not violations
    return {"ok": ok, "schema": GAPS_SCHEMA, "gaps": results,
            "violations": violations, "unverified": unverified,
            "open_ids": [g["id"] for g in results if g["status"] == "open"]}


def _catalog_providers(catalog: Path) -> set[str] | None:
    if not catalog.is_dir():
        return None
    return {p.stem for p in catalog.glob("*.json")}


def _load_status(status: str | Path | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(status, dict):
        return status
    target = Path(status) if status else DEFAULT_STATUS_PATH
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _evaluate(detect: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    kind = str(detect.get("kind") or "")
    if kind == "catalog_provider":
        provider = str(detect.get("provider") or "")
        catalog = ctx["catalog"]
        if catalog is None:
            return {"verdict": "unverified",
                    "evidence": f"目录不在位（{ctx['catalog_dir']}）"}
        if provider in catalog:
            return {"verdict": "resolved",
                    "evidence": f"目录已有 provider {provider}"}
        return {"verdict": "open",
                "evidence": f"目录无 provider {provider}（{len(catalog)} 个已登记）"}
    if kind == "role_candidates":
        role = str(detect.get("role") or "")
        status = ctx["status"]
        if status is None:
            return {"verdict": "unverified", "evidence": "connector-status.json 不在位"}
        entry = next((r for r in status.get("role_bindings") or []
                      if r.get("role") == role), None)
        if entry is None:
            return {"verdict": "unverified",
                    "evidence": f"状态面没有角色 {role} 条目"}
        candidates = entry.get("candidates") or []
        if candidates:
            return {"verdict": "resolved",
                    "evidence": f"角色 {role} 候选：{', '.join(map(str, candidates))}"}
        return {"verdict": "open",
                "evidence": f"角色 {role} 候选集为空（{entry.get('reason')}）"}
    if kind == "executor_tests":
        provider = str(detect.get("provider") or "")
        minimum = int(detect.get("min") or 1)
        d = ctx["providers"] / provider
        if not ctx["providers"].is_dir():
            return {"verdict": "unverified",
                    "evidence": f"执行器目录不在位（{ctx['providers_dir']}）"}
        count = len(list(d.glob("*.test.ts"))) if d.is_dir() else 0
        if count >= minimum:
            return {"verdict": "resolved",
                    "evidence": f"{provider} 执行器测试 {count} 个"}
        return {"verdict": "open",
                "evidence": f"{provider} 执行器测试 {count} 个（要求 ≥{minimum}）"}
    if kind == "erp_runtime_path":
        module = REPO / str(detect.get("module") or "")
        providers = [str(p) for p in detect.get("providers") or []]
        if not module.is_file():
            return {"verdict": "open", "evidence": f"ERP 模块不存在：{module}"}
        text = module.read_text(encoding="utf-8")
        direct = re.search(r"^\s*(import|from)\s+(httpx|requests|aiohttp|urllib3)\b",
                           text, re.MULTILINE)
        wired = all(f'"{p}"' in text for p in providers) and "RuntimeClient" in text
        if wired and not direct:
            return {"verdict": "resolved",
                    "evidence": (f"{module.name} 经连接器运行时读取（{', '.join(providers)}），"
                                 "无自建 HTTP 通道")}
        details = []
        if not wired:
            details.append("缺少经运行时的渠道映射")
        if direct:
            details.append("仍有自建 HTTP import")
        return {"verdict": "open", "evidence": "；".join(details)}
    if kind == "event_type_producer":
        # 「这个事实类型有没有真生产者」：在指定的仓/目录里找**除迁移与注册表之外**
        # 的代码是否出现过该类型名。只出现在迁移里＝只登记了类型、没人生产事实——
        # 这正是「先登记类型再找人填数」的形状。
        event_type = str(detect.get("event_type") or "")
        roots = [FINDDATA / str(p) for p in detect.get("search") or []]
        if not roots:
            return {"verdict": "unverified", "evidence": "detect.search 未指定搜索范围"}
        hits: list[str] = []
        missing: list[str] = []
        for root in roots:
            if not root.exists():
                missing.append(root.name)
                continue
            for f in root.rglob("*.py"):
                if "migrations" in f.parts or "tests" in f.parts:
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if event_type in text:
                    hits.append(f"{f.relative_to(FINDDATA)}")
        if hits:
            return {"verdict": "resolved",
                    "evidence": f"已找到生产 {event_type} 的代码：{', '.join(sorted(hits)[:3])}"}
        where = "、".join(str(r.relative_to(FINDDATA)) for r in roots if r.exists())
        extra = f"（不在位：{'、'.join(missing)}）" if missing else ""
        # 两种「没有生产者」要分开说，否则读的人会以为类型已经登记了：
        #   · 类型在注册表里、没人生产 → 先补载体（登记了没人填）
        #   · 类型连注册表里都没有   → 这件事根本还没立项（连形状都没定）
        registered = _type_registered(event_type)
        if registered:
            detail = f"{event_type} 只在类型注册表里，{where} 下无任何生产代码"
        else:
            detail = (f"{event_type} 既未在事件层登记、{where} 下也无任何生产代码"
                      "——这道车道还没立项")
        return {"verdict": "open", "evidence": f"{detail}{extra}"}
    return {"verdict": "unverified", "evidence": f"未知 detect.kind：{kind}"}


def _type_registered(event_type: str) -> bool:
    """该类型名是否出现在内核迁移的种子 SQL 里（注册=事实类型的唯一出处）。"""
    mig = FINDDATA / "fd-erp-ledger-mcp" / "fd_erp_ledger_mcp" / "migrations"
    if not mig.is_dir():
        return False
    return any(event_type in f.read_text(encoding="utf-8", errors="ignore")
               for f in mig.glob("*.sql"))


def render(result: dict[str, Any]) -> str:
    lines = [("缺口登记：一致" if result["ok"] else
              f"缺口登记：需回填（{len(result['violations'])} 处）")]
    for gap in result["gaps"]:
        mark = {"open": "open ", "resolved": "done ", "unverified": "???  "}[gap["verdict"]]
        lines.append(f"  {mark}{gap['id']}：{gap['capability']}（责任仓 {gap['responsible_repo']}）")
        if gap.get("evidence"):
            lines.append(f"        {gap['evidence']}")
    for item in result["violations"]:
        lines.append(f"  x [{item['code']}] {item['gap']}")
        lines.append(f"      {item['message']}")
    for item in result.get("unverified") or []:
        lines.append(f"  ! 未验证 {item['id']}：{item['reason']}")
    return "\n".join(lines)


__all__ = ["DEFAULT_GAPS_PATH", "GAPS_SCHEMA", "REQUIRED_GAPS", "check", "load_gaps",
           "render"]

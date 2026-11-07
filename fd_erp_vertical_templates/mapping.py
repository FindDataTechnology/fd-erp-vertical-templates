"""科目映射 pack 契约（erp-vertical-templates 任务 1.2 / design D2）。

- 账本内核保持行业无关的通用科目最小集（真源 = 内核迁移 0002 预设表）。
- 行业科目经映射 pack 对齐到内核科目：映射只影响报表与查询呈现，
  不影响已落账分录 —— 替换映射不改账。
- 映射 pack MUST NOT 向内核注入行业专属科目行（本模块没有任何写内核的路径）。

一致性约束：一个内核通用科目在同一映射内至多被一个行业科目对齐
（呈现层重算才不会重复计数；行业细目如需拆分，在报表行聚合面处理）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import TemplateError

#: 内核通用科目最小集 v1（与内核迁移 0002 universal_accounts 预设逐字对齐；
#: tests/test_mapping.py 交叉核对内核迁移 SQL，两处不得漂移）。
UNIVERSAL_ACCOUNTS: dict[str, dict[str, str]] = {
    "1000": {"name": "库存现金及银行存款", "name_en": "Cash and bank", "type": "asset"},
    "1100": {"name": "应收账款", "name_en": "Accounts receivable", "type": "asset"},
    "1200": {"name": "存货", "name_en": "Inventory", "type": "asset"},
    "1300": {"name": "在途结算款", "name_en": "Settlements in transit", "type": "asset"},
    "2000": {"name": "应付账款", "name_en": "Accounts payable", "type": "liability"},
    "2100": {"name": "预收及暂收款", "name_en": "Advances and temporary receipts", "type": "liability"},
    "3000": {"name": "业主投入资本", "name_en": "Owner contributed capital", "type": "equity"},
    "3100": {"name": "留存收益", "name_en": "Retained earnings", "type": "equity"},
    "4000": {"name": "营业收入", "name_en": "Operating revenue", "type": "revenue"},
    "5000": {"name": "营业成本", "name_en": "Cost of sales", "type": "expense"},
    "5100": {"name": "平台及支付费用", "name_en": "Platform and payment fees", "type": "expense"},
    "5200": {"name": "其他经营费用", "name_en": "Other operating expenses", "type": "expense"},
}

UNIVERSAL_ACCOUNT_CODES = frozenset(UNIVERSAL_ACCOUNTS)
MAPPING_SCHEMA = "erp-account-mapping/v1"


def load_mapping_pack(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """加载并校验科目映射 pack（路径或已解析 dict）。非法即抛 MAPPING_INVALID。

    支持两种载体：独立映射载荷（schema=erp-account-mapping/v1）或内嵌于
    pack 清单的载荷（顶层 account_mapping 键 —— 科目映射 pack 的市场分发形态）。
    """
    body: dict[str, Any]
    if isinstance(source, dict):
        body = source
    else:
        try:
            body = json.loads(Path(source).read_text(encoding="utf-8"))
        except OSError as exc:
            raise TemplateError("MAPPING_INVALID", f"映射 pack 不可读：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise TemplateError("MAPPING_INVALID", f"映射 pack 不是合法 JSON：{exc}") from exc
    if body.get("schema") != MAPPING_SCHEMA and isinstance(body.get("account_mapping"), dict):
        body = body["account_mapping"]
    _validate(body)
    return body


def _validate(pack: dict[str, Any]) -> None:
    if not isinstance(pack, dict):
        raise TemplateError("MAPPING_INVALID", "映射 pack 须为对象")
    if pack.get("schema") != MAPPING_SCHEMA:
        raise TemplateError("MAPPING_INVALID",
                            f"schema 须为 {MAPPING_SCHEMA}",
                            detail={"found": pack.get("schema")})
    if not str(pack.get("version") or "").strip():
        raise TemplateError("MAPPING_INVALID", "缺 version")
    industry = pack.get("industry_accounts")
    if not isinstance(industry, list) or not industry:
        raise TemplateError("MAPPING_INVALID", "industry_accounts 须为非空数组")
    seen_codes: set[str] = set()
    seen_targets: set[str] = set()
    for i, acc in enumerate(industry):
        if not isinstance(acc, dict):
            raise TemplateError("MAPPING_INVALID", f"industry_accounts[{i}] 须为对象")
        code = str(acc.get("code") or "").strip()
        name = str(acc.get("name_zh") or "").strip()
        target = str(acc.get("maps_to") or "").strip()
        if not code or not name or not target:
            raise TemplateError(
                "MAPPING_INVALID",
                f"industry_accounts[{i}] 须含 code/name_zh/maps_to",
                detail={"code": code, "maps_to": target},
            )
        if code in seen_codes:
            raise TemplateError("MAPPING_INVALID", f"行业科目编码重复：{code}")
        seen_codes.add(code)
        if target not in UNIVERSAL_ACCOUNT_CODES:
            raise TemplateError(
                "MAPPING_INVALID",
                f"行业科目 {code} 对齐目标 {target} 不在内核通用科目最小集"
                "（映射 MUST NOT 注入行业科目行到内核）",
                detail={"industry_code": code, "maps_to": target},
            )
        kind = str(acc.get("kind") or "").strip()
        want_type = UNIVERSAL_ACCOUNTS[target]["type"]
        if kind and kind != want_type:
            raise TemplateError(
                "MAPPING_INVALID",
                f"行业科目 {code} kind={kind} 与内核科目 {target} 类型 {want_type} 不符",
            )
        if target in seen_targets:
            raise TemplateError(
                "MAPPING_INVALID",
                f"内核科目 {target} 被多个行业科目对齐（呈现重算会重复计数）",
                detail={"maps_to": target},
            )
        seen_targets.add(target)
    rows = pack.get("report_rows")
    if not isinstance(rows, list) or not rows:
        raise TemplateError("MAPPING_INVALID", "report_rows 须为非空数组")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TemplateError("MAPPING_INVALID", f"report_rows[{i}] 须为对象")
        includes = row.get("includes")
        if not isinstance(includes, list) or not includes:
            raise TemplateError("MAPPING_INVALID", f"report_rows[{i}].includes 须为非空数组")
        for ref in includes:
            if ref not in seen_codes:
                raise TemplateError(
                    "MAPPING_INVALID",
                    f"report_rows[{i}] 引用未定义行业科目 {ref}",
                )


def apply_mapping(balances: list[dict[str, Any]],
                  pack: dict[str, Any]) -> dict[str, Any]:
    """呈现层重算：把内核余额（query_balance 输出，按内核科目聚合）
    重排为行业报表口径。

    - 只读：不修改传入 balances，不触碰账本；无任何写路径。
    - 行业科目 = 内核科目的呈现别名（一一对齐），金额按原样进入报表行。
    - 未被映射覆盖的内核科目余额如实列在 unmapped_balances（不静默吞掉）。
    """
    industry_by_target: dict[str, dict[str, Any]] = {
        str(a["maps_to"]): a for a in pack["industry_accounts"]
    }
    row_defs: dict[str, dict[str, Any]] = {}
    for row in pack["report_rows"]:
        for ref in row["includes"]:
            row_defs[ref] = row

    rows: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    covered_targets: set[str] = set()
    # 同一行业科目可能收到多条内核余额行（不同币种）；聚合到报表行。
    agg: dict[str, dict[str, Any]] = {}

    def _blank(key: str) -> dict[str, Any]:
        row = row_defs.get(key, {})
        return {
            "row_key": row.get("key", key),
            "label_zh": row.get("label_zh", ""),
            "label_en": row.get("label_en", ""),
            "debit_total": 0.0, "credit_total": 0.0, "balance": 0.0,
            "industry_accounts": {},
        }

    for bal in balances:
        code = str(bal.get("account_code") or "")
        industry = industry_by_target.get(code)
        if industry is None:
            unmapped.append(dict(bal))
            continue
        covered_targets.add(code)
        key = industry["code"]
        bucket = agg.setdefault(key, _blank(key))
        bucket["industry_accounts"][key] = {
            "code": industry["code"], "name_zh": industry["name_zh"],
            "name_en": industry.get("name_en", ""), "maps_to": code,
            "name_kernel": UNIVERSAL_ACCOUNTS[code]["name"],
        }
        for field in ("debit_total", "credit_total", "balance"):
            bucket[field] = round(bucket[field] + float(bal.get(field) or 0), 6)

    # 报表行按 report_rows 顺序输出；有余额的行业科目归入其报表行。
    emitted_keys: set[str] = set()
    for row in pack["report_rows"]:
        row_out = None
        for ref in row["includes"]:
            if ref in agg:
                if row_out is None:
                    row_out = agg[ref]
                    rows.append(row_out)
                    emitted_keys.add(ref)
                else:
                    # 同一报表行多个行业科目：并列成多条行（不合并金额，
                    # 防跨科目混算），后到的科目另起一行、label 相同。
                    extra = agg[ref]
                    extra["row_key"] = f"{row['key']}:{ref}"
                    rows.append(extra)
                    emitted_keys.add(ref)
    for key, bucket in agg.items():  # 有余额但不在任何报表行的行业科目（如实）
        if key not in emitted_keys:
            bucket["row_key"] = f"unassigned:{key}"
            rows.append(bucket)

    return {
        "mapping_version": pack["version"],
        "vertical": pack.get("vertical"),
        "rows": rows,
        "unmapped_balances": unmapped,
        "caliber_note": "呈现层口径：科目映射只重排报表与查询呈现，"
                        "已落账分录零改动（kernel-of-truth 不变）",
    }


def replace_mapping(old_pack: dict[str, Any],
                    new_pack: dict[str, Any]) -> dict[str, Any]:
    """映射替换的结构化 diff（验证用例断言用）：账不动，口径变。"""
    old_by_target = {a["maps_to"]: a for a in old_pack["industry_accounts"]}
    new_by_target = {a["maps_to"]: a for a in new_pack["industry_accounts"]}
    retargeted = [
        {"code": t, "was": old_by_target[t]["code"], "now": new_by_target[t]["code"]}
        for t in sorted(set(old_by_target) & set(new_by_target))
        if old_by_target[t]["code"] != new_by_target[t]["code"]
    ]
    return {
        "added": sorted(set(new_by_target) - set(old_by_target)),
        "removed": sorted(set(old_by_target) - set(new_by_target)),
        "retargeted": retargeted,
        "rows_changed": old_pack["report_rows"] != new_pack["report_rows"],
        "old_version": old_pack["version"],
        "new_version": new_pack["version"],
    }

"""中国国别包加载（change: add-cn-localization-pack 任务 1.3）。

科目表与税码**不是我们编的**：它们是 `localization/cn/*.json` 里的数据，出处见
`localization/cn/PROVENANCE.md`（Odoo 社区版 `l10n_cn` @20.0，LGPL-3.0）。

这个模块只做三件事，**不做任何会计判断**：

1. 读数据并校验结构（码唯一、父级可解析、类别落在内核封闭五类）；
2. 校验 `manifest.json` 里的 sha256 与条数——数据被改过就加载失败，别静默用；
3. 提供查询：`accounts()` / `account(code)` / `tax_codes()` / `statutory_codes()`。

为什么 sha256 校验在这里而不是 CI 里：国别数据是"抄来的规范"，被随手改一格而没人复核，
报表就会错得很安静。把校验放在每次加载上，改数据就必须同时更新 manifest（这是有意的摩擦）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import TemplateError

SCHEMA = "fd-erp-vertical-templates/localization/cn/v1"
BASE = Path(__file__).resolve().parents[1] / "localization" / "cn"

#: 内核科目的封闭五类（与 `accounts.account_type` 同构）。
KINDS = ("asset", "liability", "equity", "revenue", "expense")

#: 本次接的准则：两套都接（change: add-cn-large-chart-statements）。
#: 映射与报表行次**按准则分表**：同一科目码在两套准则里语义不同
#: （5401 小表是主营业务成本、大表是工程施工；4001 小表是生产成本、大表是实收资本），
#: 共用一份映射会让另一套静默错行。
CHART_SMALL = "accounts-small.json"
CHART_LARGE = "accounts-large.json"
TAX_FILE = "tax-codes.json"
MAPPING_FILES = {CHART_SMALL: "mapping.json", CHART_LARGE: "mapping-large.json"}
STATEMENTS_FILES = {CHART_SMALL: "statements-small.json",
                    CHART_LARGE: "statements-large.json"}
#: 兼容旧引用（默认准则那份）。
MAPPING_FILE = MAPPING_FILES[CHART_SMALL]
STATEMENTS_FILE = STATEMENTS_FILES[CHART_SMALL]

#: 各文件的期望条数（上游实测；变化即需复核 PROVENANCE）。
EXPECTED_COUNTS = {CHART_SMALL: 156, CHART_LARGE: 286, TAX_FILE: 62}


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TemplateError("NOT_FOUND", f"国别包缺文件：{path.name}",
                            detail={"path": str(path)}) from None
    except json.JSONDecodeError as exc:
        raise TemplateError("INVALID_ARGUMENT",
                            f"国别包文件不是合法 JSON：{path.name}（{exc.msg}）",
                            detail={"path": str(path)}) from None


def load_manifest(base: Path | None = None) -> dict[str, Any]:
    root = Path(base or BASE)
    return _read(root / "manifest.json")


def verify(base: Path | None = None) -> dict[str, Any]:
    """校验三份数据：sha256 与条数。返回校验摘要；不一致即抛。"""
    root = Path(base or BASE)
    manifest = load_manifest(root)
    checked: list[dict[str, Any]] = []
    for name, meta in (manifest.get("files") or {}).items():
        path = root / name
        if not path.exists():
            raise TemplateError("NOT_FOUND", f"国别包缺文件：{name}", detail={"file": name})
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected = str(meta.get("sha256") or "")
        if digest != expected:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"国别数据 {name} 与 manifest 记录的 sha256 不一致："
                "数据被改过（或改了数据忘了更新 manifest）——先复核 PROVENANCE 再重算哈希",
                detail={"file": name, "expected": expected, "actual": digest})
        data = json.loads(raw.decode("utf-8"))
        # 科目/税码文件校验条数；映射与报表项目没有"条数"语义（count=null），只校验哈希。
        rows = data.get("accounts")
        if rows is None:
            rows = data.get("codes")
        if rows is not None:
            expected_count = int(meta.get("count") or EXPECTED_COUNTS.get(name) or 0)
            if len(rows) != expected_count:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"国别数据 {name} 条数为 {len(rows)}，manifest 记 {expected_count}："
                    "上游变了或转换出错，需重新核对",
                    detail={"file": name, "actual": len(rows),
                            "expected": expected_count})
            checked.append({"file": name, "sha256": digest, "count": len(rows)})
        else:
            checked.append({"file": name, "sha256": digest})
    return {"schema": manifest.get("schema"), "source": manifest.get("source"),
            "fetchedOn": manifest.get("fetchedOn"), "files": checked}


class CnPack:
    """中国国别包（只读）。默认加载后立即校验。"""

    def __init__(self, base: Path | None = None, *, chart: str = CHART_SMALL,
                 verify_on_load: bool = True):
        self._root = Path(base or BASE)
        if verify_on_load:
            self.verification = verify(self._root)
        else:                                    # pragma: no cover - 仅调试用
            self.verification = {"files": []}
        if chart not in (CHART_SMALL, CHART_LARGE):
            raise TemplateError("INVALID_ARGUMENT",
                                f"未收录的准则：{chart}",
                                detail={"allowed": [CHART_SMALL, CHART_LARGE]})
        data = _read(self._root / chart)
        rows = data.get("accounts") or []
        self.chart = chart
        self.variant = str(data.get("variant") or "")
        self.note = str(data.get("note") or "")
        self._accounts: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = str(row.get("code") or "").strip()
            if not code:
                raise TemplateError("INVALID_ARGUMENT", "国别科目缺少编码")
            if code in self._accounts:
                raise TemplateError("INVALID_ARGUMENT", f"国别科目编码重复：{code}",
                                    detail={"code": code})
            kind = str(row.get("kind") or "")
            if kind not in KINDS:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"国别科目 {code} 的类别 {kind!r} 不在内核五类内",
                    detail={"code": code, "allowed": list(KINDS)})
            self._accounts[code] = row
        missing = sorted({str(r.get("parent")) for r in rows
                          if r.get("parent") and str(r["parent"]) not in self._accounts})
        if missing:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"国别科目的父级无法解析：{'、'.join(missing[:5])}",
                detail={"missingParents": missing})
        self._tax = _read(self._root / TAX_FILE).get("codes") or []
        self._mapping = _read(self._root / MAPPING_FILES[chart])
        self._statements = _read(self._root / STATEMENTS_FILES[chart])
        self._check_mapping()
        self._check_statements()

    def _check_mapping(self) -> None:
        """映射写错比不写更糟：目标科目必须真在包里，否则报表会静默少一行。

        两套准则都做真校验（早先对非小企业直接 return，等于给大表发免检牌）。
        """
        chart = str(self._mapping.get("chart") or "")
        expected = "cn-small-business" if self.chart == CHART_SMALL else "cn-enterprise"
        if chart and chart != expected:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"映射文件声明的准则 {chart} 与所加载的准则 {expected} 不一致",
                detail={"mappingChart": chart, "packChart": self.chart})
        kernel_to_cn = self._mapping.get("kernelToCn") or {}
        dangling = sorted({str(v.get("cn")) for v in kernel_to_cn.values()
                           if not self.account(str(v.get("cn") or ""))})
        if dangling:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"映射指向的准则科目不存在：{'、'.join(dangling[:5])}"
                f"（准则 {expected}）",
                detail={"dangling": dangling, "chart": expected})

    def _check_statements(self) -> None:
        """报表行次的科目与引用都要能解析——否则报表会静默少一行或算成 0。"""
        rules = self._statements
        keys: set[str] = set()
        problems: list[str] = []
        sections = list((rules.get("balanceSheet") or {}).values()) + [
            rules.get("incomeStatement") or []]
        for lines in sections:
            for line in lines:
                keys.add(str(line.get("key")))
        for lines in sections:
            for line in lines:
                key = str(line.get("key"))
                for code in line.get("codes") or []:
                    if not self.account(code):
                        problems.append(f"{key}: 科目 {code} 不在准则表里")
                for ref in line.get("subtotal_of") or []:
                    if str(ref) not in keys:
                        problems.append(f"{key}: 小计引用了未定义的键 {ref}")
                for term in line.get("formula") or []:
                    ref = str(term).lstrip("+-")
                    if ref not in keys:
                        problems.append(f"{key}: 公式引用了未定义的键 {ref}")
        problems.extend(self._check_cash_flow())
        if problems:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"报表行次与准则表不一致（{self.chart}）：{'；'.join(problems[:5])}",
                detail={"problems": problems[:20], "chart": self.chart})

    def _check_cash_flow(self) -> list[str]:
        """现金流量表归位表的校验。

        这张表的每个数都由「对手科目 → 报表行」推出来，写错的形态只有两类：科目码打错
        （该进表的钱静默进 unclassified）与行键打错（行名变 None、金额落进不存在的行）。
        两者都不会让报表报错，只会让某一类净额差一截——所以在加载期逐条查。
        """
        cf = self._statements.get("cashFlow")
        if not isinstance(cf, dict):
            return []
        problems: list[str] = []
        cash_line = str(cf.get("cashLine") or "")
        assets = (self._statements.get("balanceSheet") or {}).get("assets") or []
        if cash_line and not any(str(l.get("key")) == cash_line for l in assets):
            problems.append(f"cashFlow.cashLine 指向资产负债表里不存在的行 {cash_line!r}")
        categories = cf.get("categories") or {}
        lines = cf.get("lines") or {}
        for key, spec in lines.items():
            category = str((spec or {}).get("category") or "")
            if category not in categories:
                problems.append(f"cashFlow.lines.{key}: 类别 {category!r} 不在 categories 里")
            if not str((spec or {}).get("label") or "").strip():
                problems.append(f"cashFlow.lines.{key}: 缺报表行名")
        for code, spec in (cf.get("map") or {}).items():
            if not self.account(code):
                problems.append(f"cashFlow.map.{code}: 科目不在准则表里")
            if not isinstance(spec, dict):
                problems.append(f"cashFlow.map.{code}: 归位值须为对象（line 或 inflow/outflow）")
                continue
            targets = [spec[k] for k in ("line", "inflow", "outflow") if spec.get(k)]
            if not targets:
                problems.append(f"cashFlow.map.{code}: 既没给 line 也没给 inflow/outflow")
            for target in targets:
                if str(target) not in lines:
                    problems.append(f"cashFlow.map.{code}: 归位到未定义的行 {target!r}")
        disposal = cf.get("disposal") or {}
        if disposal:
            if str(disposal.get("line") or "") not in lines:
                problems.append(
                    f"cashFlow.disposal: 行 {disposal.get('line')!r} 未在 lines 里声明")
            for code in disposal.get("roots") or []:
                if not self.account(code):
                    problems.append(f"cashFlow.disposal.roots: 科目 {code} 不在准则表里")
            if not [c for c in (disposal.get("roots") or []) if self.account(c)]:
                problems.append("cashFlow.disposal.roots: 一个可解析的科目都没有——"
                                "整笔归位会永不触发")
        problems.extend(self._check_cash_flow_coverage(cf))
        return problems

    def _check_cash_flow_coverage(self, cf: dict[str, Any]) -> list[str]:
        """映射的每个目标都必须有归宿：现金科目 / 归位表（含父级）/ 显式声明不会带现金。

        **漏登记是静默的**：内核科目映射到准则科目 A，而 A 不在归位表里，那一笔现金
        照样在账上、报表照样平——只是它不进任何一类，`categorizedNet` 少一截，且
        `unclassified` 要点开才看得见。2026-11-14 大表就漏了 2203 预收账款（300 元
        静默落表外），是这条判据把它点出来的。
        """
        known = set(cf.get("map") or {}) | set(cf.get("neverCash") or {})
        roots = [str(c) for c in (((cf.get("cashLine") and
                   next((l.get("codes") for l in
                         ((self._statements.get("balanceSheet") or {}).get("assets") or [])
                         if str(l.get("key")) == str(cf.get("cashLine"))), [])) or []))]
        roots = [str(c) for c in roots]
        problems: list[str] = []
        for code in sorted({str(v.get("cn")) for v in
                            (self._mapping.get("kernelToCn") or {}).values()}):
            if any(code == r or code.startswith(r + ".") for r in roots):
                continue                      # 现金本身不归位
            head = code
            while head and head not in known:
                if "." not in head:
                    head = ""
                    break
                head = head.rsplit(".", 1)[0]
            if not head:
                problems.append(
                    f"cashFlow: 映射目标 {code} 既不是现金科目、也不在归位表里、"
                    f"也没在 neverCash 里声明——那笔现金会静默落表外")
        for code, why in (cf.get("neverCash") or {}).items():
            if not str(why or "").strip():
                problems.append(f"cashFlow.neverCash.{code}: 要写清为什么不带现金")
            if not self.account(code):
                problems.append(f"cashFlow.neverCash.{code}: 科目不在准则表里")
        return problems

    # ---- 查询 ----

    def accounts(self) -> list[dict[str, Any]]:
        return [self._accounts[c] for c in sorted(self._accounts)]

    def account(self, code: Any) -> dict[str, Any] | None:
        return self._accounts.get(str(code or "").strip())

    def children(self, code: Any) -> list[dict[str, Any]]:
        parent = str(code or "").strip()
        return [a for a in self.accounts() if a.get("parent") == parent]

    def under(self, prefix: Any) -> list[dict[str, Any]]:
        """按编码前缀取子树（准则科目是层级码：`2221` / `2221.01` / `2221.01.01`）。"""
        head = str(prefix or "").strip()
        if not head:
            return []
        return [a for a in self.accounts()
                if a["code"] == head or a["code"].startswith(head + ".")]

    def tax_codes(self) -> list[dict[str, Any]]:
        return list(self._tax)

    def tax_rates(self) -> list[str]:
        """本次接的非负税率档（封闭集）；负数档（加计抵减/差额征税）不在内。"""
        rates = sorted({str(t.get("rate")) for t in self._tax
                        if str(t.get("rate") or "").lstrip("-").replace(".", "", 1).isdigit()
                        and not str(t.get("rate")).startswith("-")},
                       key=lambda r: float(r), reverse=True)
        return rates

    def kernel_to_cn(self) -> dict[str, str]:
        """内核记账码 → 准则科目码（一个内核码只映射一个准则码）。"""
        return {str(k): str(v.get("cn")) for k, v in
                (self._mapping.get("kernelToCn") or {}).items()}

    def mapping_notes(self) -> dict[str, str]:
        return {str(k): str(v.get("why") or "") for k, v in
                (self._mapping.get("kernelToCn") or {}).items()}

    def statements(self) -> dict[str, Any]:
        """报表项目定义（《小企业会计准则》会小企 01/02 表）。"""
        return self._statements

    def summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for row in self._accounts.values():
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        return {"chart": self.chart, "variant": self.variant,
                "accounts": len(self._accounts), "byKind": dict(sorted(kinds.items())),
                "taxCodes": len(self._tax), "taxRates": self.tax_rates(),
                "source": self.verification.get("source"),
                "note": self.note}

"""合并报表车道（change: add-consolidation-engine）。

把一组主体当成一个经济实体看：按 `entity` 维度分别取数 → 抵销内部往来 → 出一张
「各主体 → 抵销 → 合并」的工作底稿，并自带平衡断言。

## 为什么抵销差额要单列

内部往来两侧理论上相等，实际常常不等（在途、单边记账、汇率不同）。取**较小者**抵销，
差额作为 `unmatched` 报出来并说明差在哪一侧——抹平会让「集团内部有 200 说不清」永远
看不见，而它往往正是漏记或错记的第一现场。

## 为什么未标主体的分录不摊

摊出去的数字看起来完整，实际是编的。未标主体就单列 `unattributed`，让客户自己决定
是补标还是接受这一行。

## 为什么 `elim:` 分录不算进任何主体

抵销分录是**集团层面**的调整，不是某个主体的账。靠 `source_ref` 前缀 `elim:` 识别，
报告把它单独放进抵销列；这样「主体合计」「抵销」「合并」三列各自可加、可核。

## 为什么外币主体先前直接拒

本版一开始没有折算率数据通路。按 1:1 混入会得到一个看起来精确、实际毫无意义的合并数；
按某个写死的汇率折更是猜。所以 `NOT_WIRED` 并说清缺什么。

（change: add-fx-translation 起：口径 `fx_translation = closing-rate` 时先折算再抵销，折算差额
进权益 CTA；口径 `none` 与 `temporal` 仍按上面拒。）

## 为什么折算必须在抵销之前

内部往来取「双方较小者」。一边 USD 1000、一边 CNY 7000，直接比大小是拿两种币种比数字——
抵销额从根上就是错的，而报表照样能平。所以折算作为 pre-pass：先把每行换成集团本位币，
再走同一条抵销与平衡管线，不出现第二条口径。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .dates import entry_period
from .dates import period_end as _period_end
from .errors import TemplateError
from .eventlog import EventLog
from .policy import PolicyBook
from .translation import TranslationBook

Q2 = Decimal("0.01")
ELIM_PREFIX = "elim:"
#: 程序身份不得登记主体结构（集团事实须人工拍板）。
PROGRAM_ACTORS = ("system", "asset-engine", "billing-engine", "posting-engine",
                  "agent", "consolidation-engine")
#: 未标主体的分录归到这里（与账龄同一约定）。
UNATTRIBUTED = "unattributed"
#: 科目归位：与三表同一张封闭表（14 + 坏账两科目）。
CHART: dict[str, tuple[str, str]] = {
    "1000": ("货币资金", "asset"),
    "1100": ("应收账款", "asset"),
    "1231": ("坏账准备", "asset"),
    "1200": ("存货", "asset"),
    "1300": ("在途结算款", "asset"),
    "1401": ("生产成本（在制品）", "asset"),
    "1601": ("固定资产原值", "asset"),
    "1602": ("累计折旧", "asset"),
    "2000": ("应付账款", "liability"),
    "2100": ("预收及暂收款", "liability"),
    "3000": ("业主投入资本", "equity"),
    "3100": ("留存收益", "equity"),
    "4000": ("营业收入", "revenue"),
    "5000": ("营业成本", "expense"),
    "5100": ("平台及支付费用", "expense"),
    "5200": ("其他经营费用", "expense"),
    "6602": ("信用减值损失", "expense"),
    "6603": ("汇兑损益", "expense"),
}
#: 内部往来科目对（应收 ↔ 应付）——抵销的默认对象。
INTERCOMPANY_PAIRS = (("1100", "2000"),)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


class ConsolidationBook:
    """合并工作底稿（分主体取数 + 内部往来抵销 + 平衡断言）。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any,
                 policy: PolicyBook, *, base_currency: str = "CNY",
                 fx: Any = None):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"consolidation-{tenant_id}.jsonl")
        self._kernel = kernel
        self._policy = policy
        self._base = base_currency
        #: 折算车道与调汇车道共用同一张汇率登记表（`fx` 可缺省：单一币种集团用不到）。
        self._translation = (TranslationBook(tenant_id, fx, base_currency=base_currency)
                             if fx is not None else None)

    # ---- 主体登记 ----

    def _events(self) -> list[dict[str, Any]]:
        return list(self._log.events())

    def entities(self) -> list[dict[str, Any]]:
        """当前有效主体清单（同一标识取最后一次登记）。"""
        latest: dict[str, dict[str, Any]] = {}
        for rec in self._events():
            if rec.get("kind") == "entity":
                latest[str(rec["entityId"])] = rec
        return [latest[k] for k in sorted(latest)]

    def register_entity(self, entity_id: str, name: str, *, ownership: Any = "1.0",
                        functional_currency: str = "CNY",
                        parent: str | None = None,
                        registered_by: str = "owner") -> dict[str, Any]:
        """登记合并主体（人工 only；同标识幂等、改属性走追加式修订）。"""
        actor = str(registered_by or "").strip()
        if not actor or actor in PROGRAM_ACTORS:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"主体结构是集团事实，只能人工登记：{actor or '（空）'} 不得自设",
                detail={"registered_by": actor, "program_actors": list(PROGRAM_ACTORS)})
        key = str(entity_id or "").strip()
        if not key:
            raise TemplateError("INVALID_ARGUMENT", "主体标识不能为空")
        if key == UNATTRIBUTED:
            raise TemplateError("INVALID_ARGUMENT",
                                f"`{UNATTRIBUTED}` 是未标注桶的保留名，不能作为主体标识")
        title = str(name or "").strip()
        if not title:
            raise TemplateError("INVALID_ARGUMENT", "主体名称不能为空")
        try:
            ratio = Decimal(str(ownership))
        except (InvalidOperation, ValueError):
            raise TemplateError("INVALID_ARGUMENT",
                                f"持股比例不是合法数字：{ownership!r}") from None
        if not (Decimal(0) < ratio <= Decimal(1)):
            raise TemplateError("INVALID_ARGUMENT",
                                f"持股比例须在 (0, 1]：{ratio}",
                                detail={"allowed": "(0, 1]"})
        cur = str(functional_currency or "").strip().upper()
        if not cur:
            raise TemplateError("INVALID_ARGUMENT", "记账本位币不能为空")

        existing = next((e for e in self.entities() if e["entityId"] == key), None)
        if existing is not None:
            same = (existing["name"] == title
                    and _dec(existing["ownership"]) == ratio
                    and existing["functionalCurrency"] == cur
                    and existing.get("parent") == (str(parent).strip() if parent else None))
            if same:
                return {**existing, "duplicate": True}
        rec = {"kind": "entity", "entityId": key, "name": title,
               "ownership": str(ratio), "functionalCurrency": cur,
               "parent": str(parent).strip() if parent else None,
               "version": (existing or {}).get("version", 0) + 1,
               "registered_by": actor, "recorded_at": _now()}
        self._log.append(rec)
        return {**rec, "duplicate": False,
                "caliber_note": "主体登记为追加式修订；旧版本保留可回放"}

    # ---- 取数 ----

    def _entries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            out = self._kernel.query_entries(offset=offset, limit=200) or {}
            page = [r for r in (out.get("entries") or out.get("items")
                                or out.get("data") or []) if isinstance(r, dict)]
            if not page:
                break
            rows.extend(page)
            offset += len(page)
            if len(page) < 200:
                break
        return rows

    @staticmethod
    def _split_source(row: dict[str, Any]) -> tuple[str, str]:
        """(主体, 列)。`elim:` 前缀的分录归抵销列，不进任何主体。"""
        src = str(row.get("source_ref") or "")
        if src.startswith(ELIM_PREFIX):
            return (ELIM_PREFIX.rstrip(":"), "elimination")
        who = str(((row.get("dimensions") or {}).get("entity")) or "").strip()
        return (who or UNATTRIBUTED, "entity")

    @staticmethod
    def _sums(rows: list[dict[str, Any]]) -> dict[str, dict[str, Decimal]]:
        """科目 → {debit, credit, net}（net = 借 − 贷）。"""
        out: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            code = str(row.get("account_code") or "")
            bucket = out.setdefault(code, {"debit": Decimal(0),
                                           "credit": Decimal(0),
                                           "net": Decimal(0)})
            amount = _dec(row.get("amount_base") or row.get("amount"))
            signed = amount if row.get("direction") == "debit" else -amount
            bucket["debit" if signed > 0 else "credit"] += abs(amount)
            bucket["net"] += signed
        return out

    @staticmethod
    def _statement_lines(sums: dict[str, dict[str, Decimal]]) -> dict[str, Any]:
        assets = liabilities = equity = revenue = expense = Decimal(0)
        unclassified: list[str] = []
        for code in sorted(sums):
            net = sums[code]["net"]
            _line, kind = CHART.get(code, (None, None))
            if kind is None:
                unclassified.append(code)
                continue
            if kind == "asset":
                assets += net                    # 抵减资产（1231/1602）贷方余额天然为负
            elif kind == "liability":
                liabilities += -net
            elif kind == "equity":
                equity += -net
            elif kind == "revenue":
                revenue += -net
            else:
                expense += net
        return {"assets": assets, "liabilities": liabilities, "equity": equity,
                "revenue": revenue, "expense": expense,
                "retained": revenue - expense, "unclassified": unclassified}

    # ---- 折算 pre-pass（change: add-fx-translation） ----

    def _translated_rows(self, period: str, *, cfg: dict[str, Any],
                         registered: dict[str, dict[str, Any]]
                         ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """把期间内（含以前）分录折成集团本位币。

        返回已折算的分录与折算摘要（每主体差额 + 差额合计 + 平均汇率退回清单）。
        `elim:` 分录与未标主体分录不折（它们本来就记在集团本位币）。
        """
        rows = [r for r in self._entries()
                if entry_period(r) in ("", "unknown") or entry_period(r) <= period]
        mode = str(cfg["values"].get("fx_translation"))
        by_entity: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            who, col = self._split_source(row)
            if col == "entity":
                by_entity.setdefault(who, []).append(row)

        out: list[dict[str, Any]] = [r for r in rows if self._split_source(r)[1] != "entity"]
        per_entity: dict[str, dict[str, Any]] = {}
        fallbacks: list[dict[str, Any]] = []
        total = Decimal(0)

        for who, group in by_entity.items():
            ent = registered.get(who)
            func = (str(ent["functionalCurrency"]).strip().upper()
                    if ent else self._base)
            if func == self._base:
                if ent is None and any(
                        str(r.get("currency") or self._base).upper() != self._base
                        for r in group):
                    raise TemplateError(
                        "NOT_WIRED",
                        f"主体 {who} 未登记，但账上有非本位币分录：不知道它的记账本位币，"
                        "无法判断该不该折算——先登记该主体（含记账本位币）",
                        detail={"entityId": who, "baseCurrency": self._base,
                                "missing": "主体登记（functional_currency）"})
                out.extend(group)
                continue
            if mode == "none":
                raise TemplateError(
                    "NOT_WIRED",
                    f"主体 {who} 记账本位币 {func} ≠ 集团本位币 {self._base}："
                    f"口径 fx_translation=none 无折算通路，不按 1:1 混入",
                    detail={"entityId": who, "baseCurrency": self._base,
                            "functionalCurrency": func,
                            "missing": "折算率通路（fx_translation=none）",
                            "fix": "把口径 fx_translation 置为 closing-rate 并登记该期间汇率"})
            if mode != "closing-rate":
                raise TemplateError(
                    "NOT_WIRED",
                    f"折算口径 {mode} 本版未实现：时态法要逐笔历史汇率（我们没有逐日汇率）",
                    detail={"fx_translation": mode, "implemented": ["closing-rate", "none"]})
            if self._translation is None:
                raise TemplateError(
                    "NOT_WIRED",
                    f"合并车道没有接汇率登记表：外币主体 {who} 无法折算",
                    detail={"entityId": who, "missing": "FxRevaluation 未注入"})
            got = self._translation.translate(who, period, group,
                                              functional_currency=func)
            out.extend(got["rows"])
            per_entity[who] = got
            total += _dec(got["totals"]["translationDifference"])
            fallbacks.extend(got["averageFallback"])

        return out, {"mode": mode, "difference": total,
                     "entities": per_entity, "averageFallback": fallbacks}

    # ---- 工作底稿 ----

    def report(self, period: Any) -> dict[str, Any]:
        """合并工作底稿：各主体 → 抵销 → 合并（自带平衡断言）。"""
        key = _check_period(period)
        cfg = self._policy.current()
        model = str(cfg["values"].get("entity_model"))
        scope = str(cfg["values"].get("elimination_scope"))
        if model != "single-tenant-multi-entity":
            raise TemplateError(
                "NOT_WIRED",
                f"主体模型 {model} 本版未实现：一主体一租户需要跨租户只读通道",
                detail={"entity_model": model,
                        "implemented": ["single-tenant-multi-entity"]})
        if scope != "intercompany-ar-ap":
            raise TemplateError(
                "NOT_WIRED",
                f"抵销范围 {scope} 本版未实现：含存货未实现利润要内部毛利数据、"
                "含权益要完整股权链",
                detail={"elimination_scope": scope,
                        "implemented": ["intercompany-ar-ap"]})

        registered = {e["entityId"]: e for e in self.entities()}
        rows, translation = self._translated_rows(key, cfg=cfg, registered=registered)
        by_group: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            who, _col = self._split_source(row)
            by_group.setdefault(who, []).append(row)

        # 主体列：登记过的主体 + 出现过的主体（含未标注）都列出来，不静默丢行
        names = sorted(set(registered) | {k for k in by_group if k != "elim"})
        entity_cols: list[dict[str, Any]] = []
        for who in names:
            sums = ConsolidationBook._sums(by_group.get(who, []))
            st = self._statement_lines(sums)
            cta = (translation["entities"].get(who, {})
                   .get("totals", {}).get("translationDifference"))
            entity_cols.append({
                "entity": who, "registered": who in registered,
                "ownership": registered.get(who, {}).get("ownership"),
                "assets": _fmt(st["assets"]), "liabilities": _fmt(st["liabilities"]),
                "equity": _fmt(st["equity"]),
                "revenue": _fmt(st["revenue"]), "expense": _fmt(st["expense"]),
                # 折算过的列自平要把折算差额算进去，否则外币主体每期都被判「不平」
                "translationDifference": cta,
                "balanced": (st["assets"] == st["liabilities"] + st["equity"]
                             + st["retained"] + _dec(cta or 0))
                            and not st["unclassified"],
            })

        # 抵销：已落账的 elim: 分录 + 计算的内部往来抵销
        elim_sums = ConsolidationBook._sums(by_group.get("elim", []))
        elim_st = self._statement_lines(elim_sums)
        already = self._posted_elimination(by_group)
        computed, unmatched = self._intercompany(by_group, registered, already=already)

        columns = [_dec(c["assets"]) for c in entity_cols]
        combined_assets = sum(columns, Decimal(0)) + elim_st["assets"] - computed["assets"]
        combined_liab = sum((_dec(c["liabilities"]) for c in entity_cols), Decimal(0)) \
            + elim_st["liabilities"] - computed["liabilities"]
        combined_equity = sum((_dec(c["equity"]) for c in entity_cols), Decimal(0)) \
            + elim_st["equity"]
        combined_revenue = sum((_dec(c["revenue"]) for c in entity_cols), Decimal(0)) \
            + elim_st["revenue"]
        combined_expense = sum((_dec(c["expense"]) for c in entity_cols), Decimal(0)) \
            + elim_st["expense"]
        retained = combined_revenue - combined_expense
        cta = translation["difference"]
        # 折算差额进合并权益（列示为独立一行）；不塞进未结转损益
        rhs = combined_liab + combined_equity + cta + retained
        balanced = combined_assets == rhs

        out = {
            "period": key,
            "baseCurrency": self._base,
            "entityModel": model,
            "eliminationScope": scope,
            "fxTranslation": translation["mode"],
            "entities": entity_cols,
            "translation": {
                "difference": _fmt(cta),
                "byEntity": [{"entity": who, "functionalCurrency": got["functionalCurrency"],
                              "closingRate": got["rates"]["closing"],
                              "difference": got["totals"]["translationDifference"],
                              "balanced": got["balanced"]}
                             for who, got in sorted(translation["entities"].items())],
                "averageFallback": translation["averageFallback"],
            },
            "elimination": {
                "computed": {"assets": _fmt(computed["assets"]),
                             "liabilities": _fmt(computed["liabilities"])},
                "posted": {"assets": _fmt(elim_st["assets"]),
                           "liabilities": _fmt(elim_st["liabilities"])},
                "unmatched": unmatched,
            },
            "combined": {
                "assets": _fmt(combined_assets),
                "liabilities": _fmt(combined_liab),
                "equity": _fmt(combined_equity),
                "translationDifference": _fmt(cta),
                "revenue": _fmt(combined_revenue),
                "expense": _fmt(combined_expense),
                "retained": _fmt(retained),
                "liabilitiesPlusEquity": _fmt(rhs),
            },
            "balanced": balanced,
            "caliber_note": "合并 = 各主体合计 + 抵销列 − 计算出的内部往来抵销；"
                            "未标主体的分录单列 unattributed，不摊给任何主体；"
                            "外币主体先折算再抵销，折算差额进权益且未按持股拆分归母/少数股东；"
                            "内部往来按科目（1100↔2000）识别——同一科目里若混有对外往来，"
                            "会一并计入抵销，差额在 unmatched 里点名",
        }
        if not balanced:
            out["error"] = (f"合并后不平：资产 {_fmt(combined_assets)} ≠ "
                            f"负债+权益+未结转损益 {_fmt(rhs)}"
                            f"（差 {_fmt(combined_assets - rhs)}）——先查各主体是否自平")
        if unmatched:
            out["warning"] = ("内部往来两侧不等，已按较小者抵销并单列差额："
                              + "；".join(f"{u['pair']} 差 {u['amount']}（{u['side']}）"
                                         for u in unmatched))
        return out

    @staticmethod
    def _posted_elimination(by_group: dict[str, list[dict[str, Any]]]) -> Decimal:
        """已落账抵销额：`elim:` 分录对第一个内部往来应收科目的贷方净额。

        抵销分录被归到抵销列（不算进任何主体），所以主体列显示的仍是**毛额**内部往来。
        计算「还需要抵多少」时必须把这部分扣掉，否则第二次合并会把同一笔再抵一次——
        资产越并越小，而报表照样能平（负债也一起小），这种错最难看出来。
        """
        ar_code = INTERCOMPANY_PAIRS[0][0]
        sums = ConsolidationBook._sums(by_group.get("elim", []))
        return max(-sums.get(ar_code, {}).get("net", Decimal(0)), Decimal(0))

    @staticmethod
    def _intercompany(by_group: dict[str, list[dict[str, Any]]],
                      registered: dict[str, dict[str, Any]],
                      *, already: Decimal | None = None
                      ) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
        """按内部往来科目对计算**尚需**抵销额（双方较小者 − 已落账），差额进 unmatched。"""
        computed = {"assets": Decimal(0), "liabilities": Decimal(0)}
        unmatched: list[dict[str, Any]] = []
        for ar_code, ap_code in INTERCOMPANY_PAIRS:
            ar_total = Decimal(0)
            ap_total = Decimal(0)
            for who, rows in by_group.items():
                if who == "elim":
                    continue
                sums = ConsolidationBook._sums(rows)
                ar_total += max(sums.get(ar_code, {}).get("net", Decimal(0)), Decimal(0))
                ap_total += max(-sums.get(ap_code, {}).get("net", Decimal(0)), Decimal(0))
            gross = min(ar_total, ap_total)
            amount = gross - (already or Decimal(0))
            amount = amount if amount > 0 else Decimal(0)
            computed["assets"] += amount          # 抵减应收（资产）
            computed["liabilities"] += amount     # 抵减应付（负债）
            diff = ar_total - ap_total
            if diff != 0:
                unmatched.append({
                    "pair": f"{ar_code}↔{ap_code}",
                    "amount": _fmt(abs(diff)),
                    "side": "应收侧多" if diff > 0 else "应付侧多",
                    "receivable": _fmt(ar_total), "payable": _fmt(ap_total),
                    "grossEliminable": _fmt(gross),
                    "posted": _fmt(already or Decimal(0)),
                })
        return computed, unmatched

    # ---- 抵销入账 ----

    def propose_eliminations(self, period: Any, *,
                             submitted_by: str = "consolidation-engine") -> dict[str, Any]:
        """把计算的内部往来抵销落成一笔待确认 proposal（业务日期 = 期间末日）。"""
        key = _check_period(period)
        # 与 report 同源：外币主体先折算再算抵销额，否则两条路径会给出两个抵销数
        registered = {e["entityId"]: e for e in self.entities()}
        rows, _translation = self._translated_rows(
            key, cfg=self._policy.current(), registered=registered)
        by_group: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            who, _col = self._split_source(row)
            by_group.setdefault(who, []).append(row)
        computed, unmatched = self._intercompany(
            by_group, {}, already=self._posted_elimination(by_group))
        amount = computed["assets"]
        if amount <= 0:
            return {"period": key, "noop": True, "amount": "0.00",
                    "unmatched": unmatched,
                    "caliber_note": "没有可抵销的内部往来，不生成 proposal"}
        ar_code, ap_code = INTERCOMPANY_PAIRS[0]
        stable_id = f"{ELIM_PREFIX}{key}"
        proposal = self._kernel.submit_proposal(
            stable_id,
            [{"account_code": ap_code, "direction": "debit", "amount": _fmt(amount),
              "currency": self._base},
             {"account_code": ar_code, "direction": "credit", "amount": _fmt(amount),
              "currency": self._base}],
            source_ref=f"{ELIM_PREFIX}{key}", kind="journal",
            lane_title=f"{key} 内部往来抵销", submitted_by=submitted_by,
            booked_on=_period_end(key))
        rec = {"kind": "elimination", "period": key, "amount": _fmt(amount),
               "stableId": stable_id, "unmatched": unmatched,
               "proposal": proposal, "recorded_at": _now()}
        self._log.append(rec)
        return {"period": key, "noop": False, "amount": _fmt(amount),
                "stableId": stable_id, "proposal": proposal,
                "bookedOn": _period_end(key), "unmatched": unmatched,
                "duplicate": bool(proposal and proposal.get("duplicate")),
                "caliber_note": "抵销分录 source_ref 以 elim: 开头，报告据此归入抵销列，"
                                "不计入任何主体"}


__all__ = ["ConsolidationBook", "INTERCOMPANY_PAIRS", "ELIM_PREFIX"]

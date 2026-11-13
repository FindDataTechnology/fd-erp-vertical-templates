"""应收减值（坏账准备）车道（change: add-bad-debt-provision）。

**不新增表**：分档费率登记与每期计提结果都落在模板层事件档（append-only JSONL），
账务事实仍只在内核——计提生成**待确认的 proposal**（借 `6602` / 贷 `1231`），过门后才落账。

## 为什么计提额是「增量」而不是「全额」

准备科目是累计余额：这一期该有的准备 = Σ(各账龄桶金额 × 对应费率)。若每次都把全额记一遍，
三期之后准备会是实际需要的三倍，而报表看起来仍然「有准备」。所以落账金额永远是
`应计提 − 账上已有准备`；为负就记反向（冲回）。这样重跑幂等、跨期也只影响各自期间的差额。

## 为什么费率只能人工登记

分档与费率是**会计估计**，不是计算得出的值。程序自设费率等于让系统替管理层做判断，
而这个判断要附在报表附注里对审计解释。程序车道提交一律拒。

## 为什么未实现的方法要拒而不是近似

`specific-identification` 要逐户评估与证据材料，`direct-writeoff` 要核销审批链——两者都缺
数据与流程。用账龄费率假装算出「个别认定」会让报表带着一个看似精确的数字，而它不是那个方法
的结果。宁可 `NOT_WIRED` 并说清缺什么。

## 与账龄/关账/报表的关系

- 计提基数是**账龄表**（同一套 FIFO 核销与维度归属，未标客户单列并计入计提）；
- 业务日期取期间末日：计提属于该期间的损益，且期间已关账时会被数据库拒绝（正确——先计提再关账）；
- `1231` 在报表上是抵减资产，`6602` 进期间费用与未结转损益（资产负债表平衡断言继续成立）。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import TemplateError
from .eventlog import EventLog
from .policy import PolicyBook

Q2 = Decimal("0.01")
#: 费率精度：千分之几（0.005）在两位小数下会被四舍五入成 0.01 —— 计提直接翻倍。
#: 费率单独用 6 位小数，金额仍走 Q2。
Q6 = Decimal("0.000001")
#: 准备科目（抵减应收）与对侧信用减值损失（费用）。
ALLOWANCE_ACCOUNT = "1231"
IMPAIRMENT_ACCOUNT = "6602"
#: 计提基数科目：应收账款。
AR_ACCOUNT = "1100"
#: 程序身份不得设费率（会计估计须人工拍板）。
PROGRAM_ACTORS = ("system", "asset-engine", "billing-engine", "posting-engine",
                  "bad-debt-engine", "agent")
#: 本版实现的方法（其余如实 NOT_WIRED）。
IMPLEMENTED_METHODS = ("aging-rate",)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _fmt_rate(value: Decimal) -> str:
    return str(value.quantize(Q6, rounding=ROUND_HALF_UP))


def _dec(value: Any, what: str) -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不是合法数字：{value!r}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT",
                            f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


def _period_end(period: str) -> str:
    year, month = int(period[:4]), int(period[5:])
    if month == 12:
        return date(year, 12, 31).isoformat()
    # 次月 1 日的前一天 = 本月最后一天（不硬编码 31/30/28）。
    return date.fromordinal(date(year, month + 1, 1).toordinal() - 1).isoformat()


class BadDebtLane:
    """按账龄分档计提坏账准备（增量入账、口径可配、未实现方法如实拒）。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any,
                 policy: PolicyBook, *, aging: Any = None):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"bad-debt-{tenant_id}.jsonl")
        self._kernel = kernel
        self._policy = policy
        #: 账龄引擎（应收侧）；缺省按需构造，测试可注入替身。
        self._aging = aging

    # ---- 口径 ----

    def _method(self) -> str:
        cfg = self._policy.current()
        return str(cfg["values"].get("bad_debt_method") or "aging-rate")

    def _aging_engine(self) -> Any:
        if self._aging is None:
            from .aging import ReceivableAging
            self._aging = ReceivableAging(self._tenant, self._kernel)
        return self._aging

    # ---- 分档费率（人工登记、追加式修订）----

    def _events(self) -> list[dict[str, Any]]:
        return list(self._log.events())

    def _current_schedule(self) -> list[dict[str, Any]]:
        latest: list[dict[str, Any]] = []
        for rec in self._events():
            if rec.get("kind") == "schedule":
                latest = rec.get("bands") or []
        return latest

    def set_schedule(self, bands: list[dict[str, Any]], *,
                     registered_by: str = "owner") -> dict[str, Any]:
        """登记分档费率（人工 only）：每档 `{maxDays, rate}`，最后一档须覆盖无限期。

        `maxDays=None` 表示「以上所有」（无上界）。边界必须严格递增，费率 ∈ [0,1]。
        """
        actor = str(registered_by or "").strip()
        if not actor or actor in PROGRAM_ACTORS:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"坏账费率是会计估计，只能人工拍板：{actor or '（空）'} 不得自设",
                detail={"registered_by": actor, "program_actors": list(PROGRAM_ACTORS)})
        if not isinstance(bands, list) or not bands:
            raise TemplateError("INVALID_ARGUMENT", "分档费率不能为空")

        cleaned: list[dict[str, Any]] = []
        prev = None
        for idx, band in enumerate(bands, start=1):
            if not isinstance(band, dict):
                raise TemplateError("INVALID_ARGUMENT", f"第 {idx} 档须为对象")
            raw_max = band.get("maxDays")
            if raw_max in (None, "") and idx != len(bands):
                raise TemplateError("INVALID_ARGUMENT",
                                    f"第 {idx} 档缺 maxDays（只有最后一档可无上界）")
            edge = None
            if raw_max not in (None, ""):
                try:
                    edge = int(raw_max)
                except (TypeError, ValueError):
                    raise TemplateError("INVALID_ARGUMENT",
                                        f"第 {idx} 档 maxDays 须为整数：{raw_max!r}") from None
                if edge <= 0:
                    raise TemplateError("INVALID_ARGUMENT",
                                        f"第 {idx} 档 maxDays 须为正：{edge}")
                if prev is not None and edge <= prev:
                    raise TemplateError(
                        "INVALID_ARGUMENT",
                        f"第 {idx} 档边界未递增：{edge} ≤ 上一档 {prev}",
                        detail={"band": idx, "maxDays": edge, "previous": prev})
                prev = edge
            rate = _dec(band.get("rate"), f"第 {idx} 档费率")
            if rate < 0 or rate > 1:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"第 {idx} 档费率须在 [0,1]：{rate}")
            cleaned.append({"maxDays": edge, "rate": _fmt_rate(rate)})

        if cleaned[-1]["maxDays"] is not None:
            raise TemplateError("INVALID_ARGUMENT",
                                "最后一档必须覆盖无限期（maxDays 留空）——"
                                "否则超出最后一档的账龄没有费率，会静默漏提")
        rec = {"kind": "schedule", "bands": cleaned, "version": len(
            [r for r in self._events() if r.get("kind") == "schedule"]) + 1,
            "registered_by": actor, "recorded_at": _now()}
        self._log.append(rec)
        return {"bands": cleaned, "version": rec["version"],
                "registeredBy": actor,
                "caliber_note": "分档费率是会计估计，只能人工登记；修订为追加式，旧版本保留可回放"}

    def schedule(self) -> dict[str, Any]:
        bands = self._current_schedule()
        return {"bands": bands,
                "registered": bool(bands),
                "caliber_note": "未登记分档费率时计提会被拒（不猜默认费率）"}

    # ---- 计提 ----

    def _allowance_balance(self) -> Decimal:
        """账上已有的坏账准备（`1231` 贷方净额，正数=已计提）。"""
        out = self._kernel.query_balance(account_code=ALLOWANCE_ACCOUNT) or {}
        rows = [r for r in (out.get("data") or out.get("items") or [])
                if isinstance(r, dict)]
        total = Decimal(0)
        for row in rows:
            # compute_balance 的 balance 是「借 − 贷」；准备在贷方，取反得已计提额。
            total += -_dec(row.get("balance") or 0, "准备余额")
        return total

    @staticmethod
    def _rate_for(days: int, bands: list[dict[str, Any]]) -> Decimal:
        for band in bands:
            edge = band.get("maxDays")
            if edge is None or days <= int(edge):
                return _dec(band["rate"], "费率")
        return Decimal(0)

    def provision(self, period: Any, *, as_of: Any = None,
                  submitted_by: str = "bad-debt-engine") -> dict[str, Any]:
        """按账龄分档计提（增量）：出一张待确认 proposal，`booked_on` = 期间末日。"""
        key = _check_period(period)
        method = self._method()
        if method not in IMPLEMENTED_METHODS:
            raise TemplateError(
                "NOT_WIRED",
                f"坏账计提方法 {method} 本版未实现：个别认定要逐户评估与证据材料，"
                "直接核销要核销审批链；不用账龄费率假装算出来",
                detail={"method": method, "implemented": sorted(IMPLEMENTED_METHODS),
                        "missing": "逐户评估数据与证据材料 / 核销审批链"})

        bands = self._current_schedule()
        if not bands:
            raise TemplateError("INVALID_ARGUMENT",
                                "未登记分档费率：先人工登记（set_schedule），不猜默认费率")

        cutoff = str(as_of or "").strip() or _period_end(key)
        aging = self._aging_engine().report(cutoff)
        labels = aging["bucketLabels"]
        target = Decimal(0)
        detail_rows: list[dict[str, Any]] = []
        for row in aging["rows"]:
            who = row["dimensionValue"]
            subtotal = Decimal(0)
            per_bucket: list[dict[str, Any]] = []
            for bucket in row["buckets"]:
                amount = _dec(bucket["amount"], "桶金额")
                if amount <= 0:
                    continue
                days = self._bucket_days(bucket["label"], bands)
                rate = self._rate_for(days, bands)
                contrib = (amount * rate).quantize(Q2, rounding=ROUND_HALF_UP)
                subtotal += contrib
                per_bucket.append({"label": bucket["label"], "amount": bucket["amount"],
                                   "rate": _fmt_rate(rate), "provision": _fmt(contrib)})
            target += subtotal
            detail_rows.append({"dimensionValue": who,
                                "outstanding": row["outstanding"],
                                "provision": _fmt(subtotal),
                                "buckets": per_bucket})
        target = target.quantize(Q2, rounding=ROUND_HALF_UP)

        existing = self._allowance_balance()
        delta = (target - existing).quantize(Q2, rounding=ROUND_HALF_UP)

        base = {"period": key, "asOf": cutoff, "method": method,
                "bucketLabels": labels, "target": _fmt(target),
                "existing": _fmt(existing), "delta": _fmt(delta), "rows": detail_rows}
        if delta == 0:
            rec = {**base, "kind": "provision", "result": "noop",
                   "recorded_at": _now()}
            self._log.append(rec)
            return {**base, "noop": True, "proposal": None,
                    "caliber_note": "应计提额与账上准备一致，不生成 proposal"}

        # 同一期间已有提交：先分清三种情形，别把「还没确认」也报成跨期。
        prior = self._open_submissions(key)
        if prior:
            last = prior[-1]
            if _dec(last.get("target") or 0, "上次计提额") == target:
                return {**base, "noop": True, "duplicate": True,
                        "proposal": last.get("proposal"),
                        "stableId": last.get("stableId"),
                        "caliber_note": "同期间同金额已提交过，返回首次结果（幂等）"}
            confirmed = self._proposal_status(str(last.get("stableId") or "")) == "confirmed"
            if confirmed:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"跨期调整不允许：{key} 的计提已确认 {last.get('target')}，"
                    "本期差额应在开放期间调整（先在内核冲正再重提）",
                    detail={"period": key, "priorTarget": last.get("target"),
                            "currentTarget": _fmt(target), "confirmed": True})
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"{key} 的计提已提交待确认且金额不同（{last.get('target')} → {_fmt(target)}）："
                "先作废（void）再用新键重提，不要直接覆盖",
                detail={"period": key, "priorTarget": last.get("target"),
                        "currentTarget": _fmt(target), "confirmed": False,
                        "fix": "void(period, reason=...) 后重跑 provision"})

        if delta > 0:
            lines = [
                {"account_code": IMPAIRMENT_ACCOUNT, "direction": "debit",
                 "amount": _fmt(delta), "currency": "CNY"},
                {"account_code": ALLOWANCE_ACCOUNT, "direction": "credit",
                 "amount": _fmt(delta), "currency": "CNY"},
            ]
        else:
            # 准备多了就冲回（反向）：借 1231 / 贷 6602。
            lines = [
                {"account_code": ALLOWANCE_ACCOUNT, "direction": "debit",
                 "amount": _fmt(-delta), "currency": "CNY"},
                {"account_code": IMPAIRMENT_ACCOUNT, "direction": "credit",
                 "amount": _fmt(-delta), "currency": "CNY"},
            ]

        attempt = len([r for r in self._events()
                       if r.get("kind") == "void" and r.get("period") == key])
        # 内核同 stable_id 永远返回首次结果（哪怕那次被退回），作废后重提必须换键。
        stable_id = f"bad-debt:{key}" if attempt == 0 else f"bad-debt:{key}#{attempt + 1}"
        proposal = self._kernel.submit_proposal(
            stable_id, lines, source_ref=f"bad-debt:{key}", kind="journal",
            lane_title=f"{key} 应收减值计提", submitted_by=submitted_by,
            booked_on=_period_end(key))
        rec = {**base, "kind": "provision", "result": "submitted",
               "stableId": stable_id, "proposal": proposal,
               "bookedOn": _period_end(key), "recorded_at": _now()}
        self._log.append(rec)
        return {**base, "noop": False, "proposal": proposal,
                "stableId": stable_id, "bookedOn": _period_end(key),
                "direction": "increase" if delta > 0 else "release",
                "caliber_note": "计提只生成待确认 proposal（借 6602 / 贷 1231）；"
                                "过门后由内核落账，报表按 1100 − 1231 取净额"}

    @staticmethod
    def _bucket_days(label: str, bands: list[dict[str, Any]]) -> int:
        """把桶标签还原成「该桶的代表天数」，用于匹配费率档。

        标签形如 `0-30天` / `31-60天` / `91天以上`：取上界作为代表天数（与费率档的
        `maxDays` 同口径）；`N天以上` 取 `N`（必然落在最后一档无上界）。
        """
        text = str(label or "")
        m = re.match(r"^\d+-(\d+)天$", text)
        if m:
            return int(m.group(1))
        m = re.match(r"^(\d+)天以上$", text)
        if m:
            return int(m.group(1))
        raise TemplateError("INVALID_ARGUMENT", f"无法解析账龄桶标签：{label!r}")

    def _open_submissions(self, period: str) -> list[dict[str, Any]]:
        """该期间尚未作废的提交。

        作废是**独立事件**（append-only，不改旧记录），所以「还开着」= 提交记录的 stableId
        不在作废集合里。只看提交记录自己的标记会漏掉作废，结果重提永远撞「已提交」。
        """
        events = self._events()
        voided = {r.get("stableId") for r in events if r.get("kind") == "void"
                  and r.get("period") == period}
        return [r for r in events
                if r.get("kind") == "provision" and r.get("period") == period
                and r.get("result") == "submitted"
                and r.get("stableId") not in voided]

    def _proposal_status(self, stable_id: str) -> str | None:
        """查提案状态（用于区分「待确认可作废」与「已确认=跨期」）。拿不到就当未知。"""
        if not stable_id:
            return None
        try:
            out = self._kernel.list_proposals() or {}
        except Exception:  # noqa: BLE001 查询失败不影响计提主流程
            return None
        rows = out.get("proposals") or out.get("items") or out.get("data") or []
        for row in rows:
            if isinstance(row, dict) and row.get("stable_id") == stable_id:
                return str(row.get("status") or "") or None
        return None

    def void(self, period: Any, *, reason: str, voided_by: str = "owner") -> dict[str, Any]:
        """作废某期尚未确认的计提（提错或口径变了）；已确认的须先在内核冲正。"""
        key = _check_period(period)
        note = str(reason or "").strip()
        if not note:
            raise TemplateError("INVALID_ARGUMENT", "作废须给出理由（reason）")
        prior = self._open_submissions(key)
        if not prior:
            raise TemplateError("NOT_FOUND", f"{key} 没有待作废的计提提交")
        last = prior[-1]
        if self._proposal_status(str(last.get("stableId") or "")) == "confirmed":
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"{key} 的计提已过确认门落账：先在内核冲正，不要作废台账",
                detail={"period": key, "stableId": last.get("stableId")})
        rec = {"kind": "void", "period": key, "stableId": last.get("stableId"),
               "reason": note, "voided_by": str(voided_by or "owner"),
               "recorded_at": _now()}
        self._log.append(rec)
        return {"period": key, "voided": True, "reason": note,
                "stableId": last.get("stableId"),
                "caliber_note": "作废只改台账事实；重提要换稳定键（内核按 stable_id 幂等）"}

    # ---- 查询 ----

    def status(self, *, as_of: Any = None) -> dict[str, Any]:
        """准备余额、覆盖率、分档表与口径回显。"""
        cutoff = str(as_of or "").strip() or date.today().isoformat()
        aging = self._aging_engine().report(cutoff)
        outstanding = _dec(aging["totalOutstanding"], "应收合计")
        allowance = self._allowance_balance()
        coverage = (allowance / outstanding).quantize(Decimal("0.0001"),
                                                      rounding=ROUND_HALF_UP) \
            if outstanding > 0 else Decimal(0)
        return {
            "asOf": cutoff,
            "method": self._method(),
            "outstanding": _fmt(outstanding),
            "allowance": _fmt(allowance),
            "netReceivable": _fmt(outstanding - allowance),
            "coverage": str(coverage),
            "bands": self._current_schedule(),
            "consistent": aging.get("consistent"),
            "caliber_note": "净额 = 应收 − 准备；覆盖率 = 准备 ÷ 应收（分母为 0 时记 0）",
        }


__all__ = ["BadDebtLane", "ALLOWANCE_ACCOUNT", "IMPAIRMENT_ACCOUNT"]

"""期末外币调汇（汇兑损益）（change: add-fx-revaluation）。

一笔 USD 1000 的应收按 7.00 记了 7000 本位币，期末汇率 7.20 就该记 7200——差 200 是
**汇兑损益**，不是「账错了」。不调汇的后果是资产与损益同时失真，而且两条都「看着正常」。

两条纪律：

1. **期末汇率是外部事实，人工登记且必须带出处**（同坏账费率：程序不得自设）。没有出处的
   汇率不入账；缺汇率就拒并点名币种，**绝不回落到该科目最近一笔分录的历史汇率**——历史
   汇率调汇等于没调。
2. **只调货币性项目**。存货、固定资产、累计折旧、坏账准备按历史成本或估计计量，不因汇率
   变动改变可收回金额；把它们一起调，是把准则里的分类当参数了。

## 判据是差额，不是状态

调汇分录落账后，账上本位币已经等于「外币 × 期末汇率」，重算差额自然为零，所以再跑就是
`noop`。用「提案已存在」当挡板反而危险：汇率被修订时它会把新差额一起挡掉。同族教训见
合并报表的重复抵销——已入账的调整必须被下一次计算扣掉，而不是靠状态标记记住。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .dates import entry_period, period_end
from .errors import TemplateError
from .eventlog import EventLog
from .kernel_rows import read_entries, read_proposals
from .statements import CHART, FX_REVAL_PREFIX

Q2 = Decimal("0.01")
FX_ACCOUNT = "6603"
#: 与报表同源：间接法靠这个前缀把调汇分录从营运资本变动里排除。
FX_SOURCE_PREFIX = FX_REVAL_PREFIX

#: 外币货币性项目：现金、应收、在途结算款、应付、预收暂收。
#: 存货（1200）/固定资产（1601）/累计折旧（1602）/坏账准备（1231）是**非货币性**或估计项，
#: 不按期末汇率重述。
MONETARY_ACCOUNTS: tuple[str, ...] = ("1000", "1100", "1300", "2000", "2100")

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: 汇率两类口径（change: add-fx-translation）：期末用于存量与调汇，期间平均用于损益折算。
RATE_CLOSING = "closing"
RATE_AVERAGE = "average"
RATE_KINDS = (RATE_CLOSING, RATE_AVERAGE)


def _check_kind(value: Any) -> str:
    kind = str(value or "").strip().lower() or RATE_CLOSING
    if kind not in RATE_KINDS:
        raise TemplateError("INVALID_ARGUMENT",
                            f"汇率口径须为 {'/'.join(RATE_KINDS)}：{value!r}",
                            detail={"allowed": list(RATE_KINDS)})
    return kind


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


class FxRevaluation:
    """期末调汇：登记期末汇率 → 逐币种逐科目算差额 → 出待确认 proposal。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any, *,
                 base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"fx-rates-{tenant_id}.jsonl")
        self._kernel = kernel
        self._base = base_currency

    # ---- 期末汇率（人工登记，追加式） ----

    def register_rates(self, period: Any, rates: dict[str, Any], *,
                       source: Any, kind: str = RATE_CLOSING,
                       registered_by: str = "cfo") -> dict[str, Any]:
        """登记某期间的汇率；`source` 必须有——无出处的汇率不入账。

        `kind` 两类：`closing`（期末，调汇与存量折算用）/ `average`（期间平均，损益折算用）。
        两者同表追加式、各自独立修订；不传即期末（既有调用点与历史数据不变）。
        """
        key = _check_period(period)
        rate_kind = _check_kind(kind)
        note = str(source or "").strip()
        if not note:
            raise TemplateError(
                "INVALID_ARGUMENT",
                "汇率必须带出处（source）：汇率是外部事实，无出处就无法复核",
                detail={"period": key, "kind": rate_kind})
        if not isinstance(rates, dict) or not rates:
            raise TemplateError("INVALID_ARGUMENT", "汇率不能为空：{'USD': '7.15'}")
        cleaned: dict[str, str] = {}
        for raw_cur, raw_rate in rates.items():
            cur = str(raw_cur or "").strip().upper()
            if not cur or len(cur) != 3 or not cur.isalpha():
                raise TemplateError("INVALID_ARGUMENT",
                                    f"币种须为三位字母代码：{raw_cur!r}")
            if cur == self._base:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"{cur} 是记账本位币，不能登记对本位币的汇率")
            rate = _dec(raw_rate)
            if rate <= 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"{cur} 汇率须大于零：{raw_rate!r}")
            cleaned[cur] = str(rate)
        rec = {"kind": "rates", "period": key, "rates": cleaned,
               "source": note, "registered_by": str(registered_by),
               "recorded_at": datetime.now(timezone.utc).isoformat()}
        if rate_kind != RATE_CLOSING:
            # 缺省即期末：历史记录不带该键，读侧一律按 closing 理解，故不必回填。
            rec["rateKind"] = rate_kind
        self._log.append(rec)
        return {"period": key, "kind": rate_kind, "registered": cleaned,
                "source": note, "rates": self.rates(key, kind=rate_kind)["rates"],
                "caliber_note": "追加式修订：同期间同币种同口径以后一次为准，历史留痕"}

    def rates(self, period: Any, *, kind: str = RATE_CLOSING) -> dict[str, Any]:
        """该期间某类口径生效的汇率（最后一次登记为准）与修订次数。

        `average` 缺失时返回空集，**不拿期末汇率冒充**：退回与否由折算方声明
        （见 `translation.py` 的 `averageFallback`）。
        """
        key = _check_period(period)
        rate_kind = _check_kind(kind)
        effective: dict[str, dict[str, Any]] = {}
        revisions = 0
        for rec in self._log.events():
            if rec.get("kind") != "rates" or rec.get("period") != key:
                continue
            if str(rec.get("rateKind") or RATE_CLOSING) != rate_kind:
                continue
            revisions += 1
            for cur, rate in (rec.get("rates") or {}).items():
                effective[str(cur)] = {"rate": str(rate),
                                       "source": rec.get("source"),
                                       "registeredBy": rec.get("registered_by"),
                                       "registeredAt": rec.get("recorded_at")}
        return {"period": key, "kind": rate_kind, "rates": effective,
                "revisions": revisions}

    def rate_of(self, period: Any, currency: str, *,
                kind: str = RATE_CLOSING) -> dict[str, Any] | None:
        """单币种取率（折算车道的取数口）；无登记即 `None`，由调用方决定拒还是退回。"""
        cur = str(currency or "").strip().upper()
        return self.rates(period, kind=kind)["rates"].get(cur)

    # ---- 取数与计算 ----

    def _positions(self, period: str) -> dict[str, dict[str, Any]]:
        """科目 → {totalBase, baseLeg, components{币种: [外币净额, 本位币净额]}}。

        重估比较的是**科目总本位币余额**与「各币种外币净额按期末汇率重述后之和」。
        调汇分录本身记的是本位币（差额不是一笔外币金额），所以它只能被科目总余额看到——
        这正是「落账后重跑差额为零」能成立的原因。只按外币分录算、忽略本位币调整分录，
        就会每次重算都算出同一个差额，永远调不完。
        """
        out: dict[str, dict[str, Any]] = {}
        for entry in read_entries(self._kernel):
            if entry_period(entry) > period:
                continue
            code = str(entry.get("account_code") or "").strip()
            if code not in MONETARY_ACCOUNTS:
                continue
            sign = 1 if entry.get("direction") == "debit" else -1
            base = sign * _dec(entry.get("amount_base") or entry.get("amount"))
            slot = out.setdefault(code, {"totalBase": Decimal(0),
                                         "baseLeg": Decimal(0),
                                         "components": {}})
            slot["totalBase"] += base
            currency = str(entry.get("currency") or self._base).upper()
            if currency == self._base:
                # 本位币的**业务**分录贡献「应按本位币计价的部分」；已落账的调汇分录
                # 不计——它的作用已经体现在 totalBase 里，再加一次就成了永久差额。
                if not str(entry.get("source_ref") or "").startswith(FX_SOURCE_PREFIX):
                    slot["baseLeg"] += base
                continue
            foreign = sign * _dec(entry.get("amount"))
            comp = slot["components"].setdefault(currency, [Decimal(0), Decimal(0)])
            comp[0] += foreign
            comp[1] += base
        return out

    def _rows(self, period: str,
              rates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """逐币种逐科目：外币净额 / 账上本位币 / 期末汇率 / 重估后 / 差额（差额仅作展示）。"""
        rows: list[dict[str, Any]] = []
        for code, pos in sorted(self._positions(period).items()):
            for currency, (foreign, base) in sorted(pos["components"].items()):
                if foreign == 0:
                    continue
                rate = _dec((rates.get(currency) or {}).get("rate"))
                restated = (foreign * rate).quantize(Q2, rounding=ROUND_HALF_UP) \
                    if rate > 0 else None
                rows.append({
                    "currency": currency, "accountCode": code,
                    "account": CHART.get(code, (code, None))[0] or code,
                    "foreignBalance": _fmt(foreign), "bookedBase": _fmt(base),
                    "rate": _fmt(rate) if rate > 0 else None,
                    "restatedBase": _fmt(restated) if restated is not None else None,
                    # 该币种**原始分录**的重述差异（不含已落账的调汇）：只作追溯展示。
                    # 真正的调整额按科目合计算，见 adjustments——调汇落账后它是零，
                    # 而这一列仍会显示原始差异，那是追溯，不是待调额。
                    "originalDifference": _fmt(restated - base)
                    if restated is not None else None,
                })
        return rows

    def _adjustments(self, period: str,
                     rates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """按科目算调整额：各币种重述额 + 本位币分录 − 科目总余额。"""
        out: list[dict[str, Any]] = []
        for code, pos in sorted(self._positions(period).items()):
            required = pos["baseLeg"]
            for currency, (foreign, _base) in pos["components"].items():
                rate = _dec((rates.get(currency) or {}).get("rate"))
                if rate > 0 and foreign != 0:
                    required += (foreign * rate).quantize(Q2, rounding=ROUND_HALF_UP)
            difference = (required - pos["totalBase"]).quantize(Q2, rounding=ROUND_HALF_UP)
            out.append({
                "accountCode": code,
                "account": CHART.get(code, (code, None))[0] or code,
                "totalBase": _fmt(pos["totalBase"]),
                "restatedBase": _fmt(required),
                "difference": _fmt(difference),
            })
        return out

    def preview(self, period: Any) -> dict[str, Any]:
        """逐币种逐科目：外币净额 / 账上本位币 / 期末汇率 / 重估后 / 差额。只读。"""
        key = _check_period(period)
        rates = self.rates(key)["rates"]
        rows = self._rows(key, rates)
        adjustments = self._adjustments(key, rates)
        missing = sorted({r["currency"] for r in rows if r["rate"] is None})
        out: dict[str, Any] = {
            "period": key, "rows": rows, "adjustments": adjustments, "rates": rates,
            "missingRates": missing,
            "totalDifference": _fmt(sum((_dec(r["difference"]) for r in adjustments),
                                        Decimal(0))),
            "status": self.status(key),
            "caliber_note": "只含外币货币性项目（现金/应收/在途结算款/应付/预收暂收）；"
                            "存货与固定资产按历史成本，不重述。真正的待调额看 "
                            "adjustments[].difference（按科目合计），rows 里的 "
                            "originalDifference 只是原始分录的重述追溯",
        }
        if missing:
            out["note"] = (f"币种 {', '.join(missing)} 有余额但未登记期末汇率——"
                           f"不猜、不回落历史汇率")
        return out

    def revalue(self, period: Any, *, submitted_by: str = "fx-revaluation") -> dict[str, Any]:
        """出调汇提案：科目 ↔ 6603 成对，借贷必平；差额为零即 noop。"""
        key = _check_period(period)
        rates = self.rates(key)["rates"]
        rows = self._rows(key, rates)
        adjustments = self._adjustments(key, rates)
        missing = sorted({r["currency"] for r in rows if r["rate"] is None})
        if missing:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"币种 {', '.join(missing)} 有外币余额但未登记期末汇率："
                f"不猜汇率，也不回落到历史汇率（那样调汇等于没调）",
                detail={"period": key, "missing": missing,
                        "fix": "register_rates(period, {币种: 汇率}, source=出处)"})

        lines: list[dict[str, Any]] = []
        detail: list[dict[str, Any]] = []
        for adj in adjustments:
            diff = _dec(adj["difference"])
            if diff == 0:
                continue
            side = "debit" if diff > 0 else "credit"
            other = "credit" if diff > 0 else "debit"
            lines.append({"account_code": adj["accountCode"], "direction": side,
                          "amount": _fmt(abs(diff)), "currency": self._base})
            lines.append({"account_code": FX_ACCOUNT, "direction": other,
                          "amount": _fmt(abs(diff)), "currency": self._base})
            detail.append({"accountCode": adj["accountCode"],
                           "totalBase": adj["totalBase"],
                           "restatedBase": adj["restatedBase"],
                           "difference": adj["difference"],
                           "amount": _fmt(abs(diff))})

        if not lines:
            return {"period": key, "noop": True, "proposal": None, "lines": [],
                    "rows": rows, "adjustments": adjustments,
                    "status": self.status(key),
                    "caliber_note": "期末差额为零（无外币余额，或已按期末汇率调过）：不生成提案"}

        if any(str(p.get("status") or "") == "pending"
               for p in self._attempts(key)):
            prior = self._attempts(key)[-1]
            return {"period": key, "noop": True, "duplicate": True, "pending": True,
                    "proposal": prior, "status": self.status(key),
                    "caliber_note": "本期已有待确认调汇提案：先过门或作废，不重复提交"}

        attempts = len(self._attempts(key))
        stable_id = f"{FX_SOURCE_PREFIX}{key}" if attempts == 0 \
            else f"{FX_SOURCE_PREFIX}{key}#{attempts + 1}"
        proposal = self._kernel.submit_proposal(
            stable_id, lines, source_ref=f"{FX_SOURCE_PREFIX}{key}", kind="journal",
            lane_title=f"{key} 期末外币调汇", submitted_by=submitted_by,
            booked_on=period_end(key))
        total = sum((_dec(d["amount"]) for d in detail), Decimal(0))
        return {"period": key, "noop": False, "proposal": proposal,
                "stableId": stable_id, "bookedOn": period_end(key),
                "lines": lines, "rows": rows, "adjustments": detail,
                "totalDifference": _fmt(total),
                "status": self.status(key),
                "caliber_note": "调汇只生成待确认 proposal（科目 ↔ 6603 成对）；"
                                "过门后由内核落账，再跑差额自然为零"}

    # ---- 状态 ----

    def _attempts(self, period: str) -> list[dict[str, Any]]:
        head = f"{FX_SOURCE_PREFIX}{period}"
        rows = []
        for row in read_proposals(self._kernel):
            key = str(row.get("stable_id") or "")
            if key == head or key.startswith(head + "#"):
                rows.append(row)
        return rows

    def status(self, period: Any) -> dict[str, Any]:
        key = _check_period(period)
        rows = self._attempts(key)
        posted = [r for r in rows if str(r.get("status") or "") == "confirmed"]
        pending = [r for r in rows if str(r.get("status") or "") == "pending"]
        state = "posted" if posted else "pending" if pending else \
            "rejected" if rows else "not_revalued"
        return {"period": key, "state": state, "attempts": len(rows),
                "ratesRegistered": bool(self.rates(key)["rates"]),
                "caliber_note": "状态只看提案；是否还需调汇看 preview 的差额"}


__all__ = ["FX_ACCOUNT", "FX_SOURCE_PREFIX", "FxRevaluation", "MONETARY_ACCOUNTS"]

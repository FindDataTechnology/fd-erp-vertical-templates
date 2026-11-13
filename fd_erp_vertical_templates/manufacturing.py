"""生产工单（change: add-manufacturing-orders）。

有生产的客户，账上必须能回答两件事：这批工单吃进去多少料、还差多少；以及在制品上压着多少钱。
本车道做 BOM 展开 → 工单 → 领料进在制品（`1401`）→ 完工转成品（`1200`）这一段**账务闭环**。

## 明确不做的部分

工艺路线、产能排程、MRP 运算——它们需要车间数据与 APS，且与账务无关。**多层 BOM 也不做**：
递归展开与层级校验是另一套东西，本版只支持单层，并且拒绝而不是假装展开。

## 三条不可让的纪律

1. **单位成本必须由人给**。成本凭据来自采购/成本核算；程序自设一个「大概的单价」，
   毛利就从此不可信，而且看不出来。缺成本就拒，不补零。
2. **只出待确认 proposal**。领料与完工都走确认门，不代任何人过门。
3. **在制品要能和账对上**。台账算出来的在制品，必须等于账上 `1401` 里挂了本工单维度的那部分；
   对不上就把差额报出来，不拿台账数盖住账本数。

## 为什么在制品要独立科目

领料直接记进 `1200 存货`（存货凭空变多）或 `5000 营业成本`（未完工就确认成本），
两种都会让资产负债表与毛利失真。`1401 生产成本` 借=归集、贷=转出，余额就是期末在制品。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .dates import entry_period, period_end
from .errors import TemplateError
from .eventlog import EventLog
from .kernel_rows import read_entries, read_proposals
from .statements import CHART

Q2 = Decimal("0.01")
QTY = Decimal("0.000001")
WIP_ACCOUNT = "1401"
INVENTORY_ACCOUNT = "1200"
ORDER_DIMENSION = "production_order"
SCHEMA = "fd-erp-vertical-templates/manufacturing/v1"

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROGRAM_ACTORS = {"system", "manufacturing-engine", "engine", "agent", "cron"}


def _dec(value: Any, what: str = "金额") -> Decimal:
    if value in (None, ""):
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError("INVALID_ARGUMENT",
                            f"{what}不是合法数字：{value!r}") from exc


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q2, rounding=ROUND_HALF_UP))


def _fmt_qty(value: Decimal) -> str:
    return str(value.quantize(QTY, rounding=ROUND_HALF_UP))


def _check_period(value: Any) -> str:
    key = str(value or "").strip()
    if not _PERIOD_RE.match(key):
        raise TemplateError("INVALID_ARGUMENT", f"期间须为 YYYY-MM：{value!r}",
                            detail={"example": "2026-10"})
    return key


def _check_day(value: Any, what: str = "业务日期") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise TemplateError("INVALID_ARGUMENT", f"{what}不能为空")
    if _DATE_RE.match(raw):
        return raw
    if _PERIOD_RE.match(raw):
        return period_end(raw)
    raise TemplateError("INVALID_ARGUMENT",
                        f"{what}须为 YYYY-MM-DD 或 YYYY-MM：{value!r}")


class ManufacturingLane:
    """BOM 展开 → 工单 → 领料进在制品 → 完工转成品。"""

    def __init__(self, tenant_id: str, data_dir: str | Path, kernel: Any, *,
                 base_currency: str = "CNY", today: Any = None):
        self._tenant = tenant_id
        self._log = EventLog(Path(data_dir) / f"manufacturing-{tenant_id}.jsonl")
        self._kernel = kernel
        self._base = base_currency
        self._today = today

    # ---- 取数 ----

    def _events(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in self._log.events() if isinstance(r, dict)]
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def _today_str(self) -> str:
        if self._today is not None:
            value = self._today() if callable(self._today) else self._today
            return str(value)
        return date.today().isoformat()

    # ---- BOM ----

    def register_bom(self, product_sku: Any, output_qty: Any,
                     components: Any, *, effective_from: Any = None,
                     registered_by: str = "owner") -> dict[str, Any]:
        """登记单层 BOM（人工拍板、追加式修订）。"""
        actor = str(registered_by or "").strip()
        if not actor or actor in PROGRAM_ACTORS:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"BOM 须人工登记：{actor or '（空）'} 不得自设物料清单",
                detail={"registered_by": actor, "program_actors": sorted(PROGRAM_ACTORS)})
        product = str(product_sku or "").strip()
        if not product:
            raise TemplateError("INVALID_ARGUMENT", "产品 SKU 不能为空")
        out_qty = _dec(output_qty, "产出数量")
        if out_qty <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"产出数量须大于零：{output_qty!r}")
        if not isinstance(components, list) or not components:
            raise TemplateError("INVALID_ARGUMENT", "组件清单不能为空")
        seen: set[str] = set()
        cleaned: list[dict[str, Any]] = []
        for raw in components:
            if not isinstance(raw, dict):
                raise TemplateError("INVALID_ARGUMENT", f"组件行格式非法：{raw!r}")
            sku = str(raw.get("sku") or raw.get("component") or "").strip()
            if not sku:
                raise TemplateError("INVALID_ARGUMENT", "组件行缺少 SKU")
            if sku == product:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"组件 {sku} 就是产品本身：单层 BOM 不允许自引用（多层请拆成子件）",
                    detail={"product": product, "sku": sku})
            if sku in seen:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"组件 {sku} 出现了两次：同一组件只留一行用量",
                    detail={"sku": sku})
            seen.add(sku)
            qty = _dec(raw.get("qty") or raw.get("quantity"), f"组件 {sku} 用量")
            if qty <= 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"组件 {sku} 用量须大于零：{raw.get('qty')!r}")
            cleaned.append({"sku": sku, "qty": _fmt_qty(qty),
                            "unit": str(raw.get("unit") or "").strip() or None})
        # 多层 BOM 拒绝，而不是「接受但只算一层」：上层工单会把自制子件当成外购件计价，
        # 账上一点错都看不出来，等对成本时才发现少了一整层的在制品。
        self._refuse_multilevel(product, seen)
        day = (_check_day(effective_from, "生效日期") if effective_from not in (None, "")
               else self._today_str())
        version = len(self._events("bom")) + 1
        rec = {"kind": "bom", "product": product, "outputQty": _fmt_qty(out_qty),
               "components": cleaned, "effectiveFrom": day,
               "registeredBy": actor, "version": version,
               "recordedAt": self._today_str()}
        self._log.append(rec)
        return {"product": product, "outputQty": _fmt_qty(out_qty),
                "components": cleaned, "effectiveFrom": day,
                "version": version,
                "caliber_note": "单层 BOM；多层请拆成子件并在各层各自登记（本版不递归展开）"}

    def _refuse_multilevel(self, product: str, components: set[str]) -> None:
        """单层 BOM：任何会形成层级的形状都拒，并指出改法。

        两个方向都要堵——只堵一个方向，另一种登记顺序就能绕过去：
        ①组件自己已经有 BOM（本次登记把它的成本算成外购件）；
        ②本产品已经被别人当组件用（本次登记让那个上层变成多层）。
        """
        bom_rows = self._events("bom")
        made: set[str] = {str(r.get("product")) for r in bom_rows}
        nested = sorted(sku for sku in components if sku in made)
        if nested:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"组件 {'、'.join(nested)} 自身有 BOM：多层 BOM 本版不支持，"
                "请把这一层拆成独立工单，或把该组件按外购件处理",
                detail={"components_with_bom": nested, "bom_depth": "single-level"})
        used_as_component = sorted(
            {c.get("sku") for r in bom_rows for c in (r.get("components") or [])}
            & {product})
        if used_as_component:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"{product} 已作为组件出现在别的 BOM 里：再登记它的 BOM 就成了多层，"
                "本版不支持（请把上层拆单，或把它下面这层改成外购件）",
                detail={"product": product, "bom_depth": "single-level"})

    def bom(self, product_sku: Any, *, as_of: Any = None) -> dict[str, Any] | None:
        """生效版本（同产品最后一次登记，且生效日期不晚于 as_of）。"""
        product = str(product_sku or "").strip()
        day = (_check_day(as_of, "查询日期") if as_of not in (None, "")
               else self._today_str())
        rows = [r for r in self._events("bom")
                if r.get("product") == product
                and str(r.get("effectiveFrom") or "") <= day]
        return rows[-1] if rows else None

    # ---- 工单 ----

    def order(self, product_sku: Any, qty: Any, *, ref: Any,
              due_on: Any = None, created_by: str = "ops") -> dict[str, Any]:
        """按 BOM 展开需求量；`ref` 幂等。"""
        tag = str(ref or "").strip()
        if not tag:
            raise TemplateError("INVALID_ARGUMENT", "工单须给 ref（幂等键）")
        prior = next((r for r in self._events("order") if r.get("ref") == tag), None)
        if prior is not None:
            return {"ref": tag, "duplicate": True, "order": prior,
                    "caliber_note": "同 ref 幂等：返回首次结果，不重复建单"}
        product = str(product_sku or "").strip()
        if not product:
            raise TemplateError("INVALID_ARGUMENT", "产品 SKU 不能为空")
        want = _dec(qty, "工单数量")
        if want <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"工单数量须大于零：{qty!r}")
        bom = self.bom(product)
        if bom is None:
            raise TemplateError(
                "NOT_FOUND",
                f"产品 {product} 没有生效的 BOM：先 register_bom",
                detail={"product": product})
        requirements = [{"sku": c["sku"],
                         "qty": _fmt_qty(_dec(c["qty"], "组件用量") * want),
                         "unit": c.get("unit")} for c in bom["components"]]
        rec = {"kind": "order", "ref": tag, "product": product,
               "qty": _fmt_qty(want), "dueOn": (str(due_on) if due_on else None),
               "requirements": requirements, "bomVersion": bom["version"],
               "createdBy": str(created_by or "ops")}
        self._log.append(rec)
        return {"ref": tag, "duplicate": False, "order": rec,
                "requirements": requirements,
                "caliber_note": "需求量 = 组件用量 × 工单数量（单层展开）"}

    def _order(self, ref: Any) -> dict[str, Any]:
        tag = str(ref or "").strip()
        found = next((r for r in self._events("order") if r.get("ref") == tag), None)
        if found is None:
            raise TemplateError("NOT_FOUND", f"工单不存在：{tag!r}",
                                detail={"ref": tag})
        return found

    # ---- 领料 ----

    def _issued_totals(self, ref: str) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for rec in self._events("issue"):
            if rec.get("ref") != ref:
                continue
            for line in rec.get("lines") or []:
                sku = line["sku"]
                totals[sku] = totals.get(sku, Decimal(0)) + _dec(line["qty"], "领料数量")
        return totals

    def issue(self, ref: Any, lines: Any, *, issued_on: Any = None,
              submitted_by: str = "manufacturing") -> dict[str, Any]:
        """领料进在制品：借 1401 / 贷 1200。单位成本必须由人给。"""
        order = self._order(ref)
        tag = order["ref"]
        if not isinstance(lines, list) or not lines:
            raise TemplateError("INVALID_ARGUMENT", "领料明细不能为空")
        allowed = {r["sku"] for r in order["requirements"]}
        cleaned: list[dict[str, Any]] = []
        total = Decimal(0)
        for raw in lines:
            if not isinstance(raw, dict):
                raise TemplateError("INVALID_ARGUMENT", f"领料行格式非法：{raw!r}")
            sku = str(raw.get("sku") or raw.get("component") or "").strip()
            if sku not in allowed:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"组件 {sku} 不在工单 {tag} 的 BOM 里",
                    detail={"sku": sku, "allowed": sorted(allowed)})
            qty = _dec(raw.get("qty") or raw.get("quantity"), f"组件 {sku} 领料数量")
            if qty <= 0:
                raise TemplateError("INVALID_ARGUMENT",
                                    f"组件 {sku} 领料数量须大于零")
            unit_cost = _dec(raw.get("unit_cost") or raw.get("unitCost"),
                             f"组件 {sku} 单位成本")
            if unit_cost <= 0:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"组件 {sku} 的单位成本须由成本凭据给出（不得自设）",
                    detail={"sku": sku, "unit_cost": raw.get("unit_cost")})
            amount = (qty * unit_cost).quantize(Q2, rounding=ROUND_HALF_UP)
            total += amount
            cleaned.append({"sku": sku, "qty": _fmt_qty(qty),
                            "unitCost": _fmt(unit_cost), "amount": _fmt(amount)})

        sequential = len(self._events("issue"))
        stable_id = f"mfg-issue:{tag}:{sequential + 1}"
        day = _check_day(issued_on, "领料日期") if issued_on not in (None, "") \
            else self._today_str()
        proposal = self._kernel.submit_proposal(
            stable_id,
            [{"account_code": WIP_ACCOUNT, "direction": "debit",
              "amount": _fmt(total), "currency": self._base},
             {"account_code": INVENTORY_ACCOUNT, "direction": "credit",
              "amount": _fmt(total), "currency": self._base}],
            source_ref=f"mfg-issue:{tag}", kind="journal",
            lane_title=f"{tag} 生产领料", submitted_by=submitted_by,
            dimensions={ORDER_DIMENSION: tag}, booked_on=day)

        rec = {"kind": "issue", "ref": tag, "lines": cleaned,
               "total": _fmt(total), "issuedOn": day, "stableId": stable_id}
        # 先落台账再算超领：否则本次领料自己还没进累计，超领永远检测不到
        self._log.append(rec)

        # 超领：允许但必须点名（静默吞掉会让需求量与实物长期背离）
        issued = self._issued_totals(tag)
        over: list[dict[str, Any]] = []
        for req in order["requirements"]:
            need = _dec(req["qty"], "需求量")
            got = issued.get(req["sku"], Decimal(0))
            if got > need:
                over.append({"sku": req["sku"], "required": _fmt_qty(need),
                             "issued": _fmt_qty(got),
                             "over": _fmt_qty(got - need)})
        out: dict[str, Any] = {
            "ref": tag, "stableId": stable_id, "bookedOn": day,
            "lines": cleaned, "total": _fmt(total), "proposal": proposal,
            "caliber_note": "领料只生成待确认 proposal（借 1401 / 贷 1200）；"
                            "单位成本由成本凭据给出，程序不自设",
        }
        if over:
            out["overIssued"] = over
            out["note"] = "超领已记录并点名：需求量不会被自动改写"
        return out

    # ---- 完工 ----

    def _accumulated(self, ref: str) -> Decimal:
        total = Decimal(0)
        for rec in self._events("issue"):
            if rec.get("ref") == ref:
                total += _dec(rec.get("total"), "领料合计")
        return total

    def _completed(self, ref: str) -> Decimal:
        total = Decimal(0)
        for rec in self._events("complete"):
            if rec.get("ref") == ref:
                total += _dec(rec.get("qty"), "完工数量")
        return total

    def complete(self, ref: Any, qty: Any, *, completed_on: Any = None,
                 submitted_by: str = "manufacturing") -> dict[str, Any]:
        """完工转成品：借 1200 / 贷 1401。"""
        order = self._order(ref)
        tag = order["ref"]
        done_now = _dec(qty, "完工数量")
        if done_now <= 0:
            raise TemplateError("INVALID_ARGUMENT", f"完工数量须大于零：{qty!r}")
        planned = _dec(order["qty"], "计划数量")
        done = self._completed(tag)
        if done + done_now > planned:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"完工数量超出计划：计划 {_fmt_qty(planned)}、已完工 {_fmt_qty(done)}、"
                f"本次 {_fmt_qty(done_now)}（剩 {_fmt_qty(planned - done)}）",
                detail={"planned": _fmt_qty(planned), "completed": _fmt_qty(done),
                        "remaining": _fmt_qty(planned - done)})
        accumulated = self._accumulated(tag)
        if accumulated <= 0:
            raise TemplateError(
                "INVALID_ARGUMENT",
                f"工单 {tag} 还没有归集成本：先领料再完工（0 成本入库看着正常）",
                detail={"ref": tag})
        unit_cost = accumulated / planned
        amount = (done_now * unit_cost).quantize(Q2, rounding=ROUND_HALF_UP)

        sequential = len(self._events("complete"))
        stable_id = f"mfg-complete:{tag}:{sequential + 1}"
        day = (_check_day(completed_on, "完工日期") if completed_on not in (None, "")
               else self._today_str())
        proposal = self._kernel.submit_proposal(
            stable_id,
            [{"account_code": INVENTORY_ACCOUNT, "direction": "debit",
              "amount": _fmt(amount), "currency": self._base},
             {"account_code": WIP_ACCOUNT, "direction": "credit",
              "amount": _fmt(amount), "currency": self._base}],
            source_ref=f"mfg-complete:{tag}", kind="journal",
            lane_title=f"{tag} 完工入库", submitted_by=submitted_by,
            dimensions={ORDER_DIMENSION: tag}, booked_on=day)
        rec = {"kind": "complete", "ref": tag, "qty": _fmt_qty(done_now),
               "unitCost": _fmt(unit_cost), "total": _fmt(amount),
               "completedOn": day, "stableId": stable_id}
        self._log.append(rec)
        return {"ref": tag, "stableId": stable_id, "bookedOn": day,
                "qty": _fmt_qty(done_now), "unitCost": _fmt(unit_cost),
                "total": _fmt(amount), "proposal": proposal,
                "caliber_note": "单位成本 = 已归集在制品 ÷ 计划数量；完工只出待确认 proposal"}

    # ---- 状态与对账 ----

    def status(self, ref: Any) -> dict[str, Any]:
        """需求/已领/待领、归集成本、完工进度、在制品——并与账上 1401 对账。"""
        order = self._order(ref)
        tag = order["ref"]
        issued = self._issued_totals(tag)
        requirements = []
        for req in order["requirements"]:
            need = _dec(req["qty"], "需求量")
            got = issued.get(req["sku"], Decimal(0))
            requirements.append({
                "sku": req["sku"], "required": _fmt_qty(need),
                "issued": _fmt_qty(got),
                "remaining": _fmt_qty(max(need - got, Decimal(0))),
                # 超出部分单独给：superfluous > 0 就是超领，别让 remaining 归零掩盖它
                "overIssued": _fmt_qty(max(got - need, Decimal(0))),
            })
        accumulated = self._accumulated(tag)
        completed_qty = self._completed(tag)
        completed_cost = sum((_dec(r.get("total"), "完工成本")
                              for r in self._events("complete")
                              if r.get("ref") == tag), Decimal(0))
        wip = accumulated - completed_cost
        on_ledger = self._wip_on_ledger(tag)
        return {
            "ref": tag, "product": order["product"], "qty": order["qty"],
            "requirements": requirements,
            "issuedCost": _fmt(accumulated), "completedQty": _fmt_qty(completed_qty),
            "completedPercent": (_fmt_qty(completed_qty / _dec(order["qty"], "计划数量")
                                          * 100) if _dec(order["qty"], "计划数量") else "0.000000"),
            "completedCost": _fmt(completed_cost),
            "wipBalance": _fmt(wip), "wipOnLedger": _fmt(on_ledger),
            "difference": _fmt(wip - on_ledger),
            "consistent": wip == on_ledger,
            "caliber_note": "在制品余额 = 已归集领料成本 − 已完工转出成本；"
                            "wipOnLedger 是账上 1401 挂了本工单维度的净额（落地后才会动）",
        }

    def _wip_on_ledger(self, ref: str) -> Decimal:
        """账上该工单的在制品：1401 且 dimensions.production_order == ref。"""
        total = Decimal(0)
        for entry in read_entries(self._kernel):
            if str(entry.get("account_code") or "").strip() != WIP_ACCOUNT:
                continue
            dims = entry.get("dimensions") or {}
            if str(dims.get(ORDER_DIMENSION) or "").strip() != ref:
                continue
            amount = _dec(entry.get("amount_base") or entry.get("amount"), "分录金额")
            total += amount if entry.get("direction") == "debit" else -amount
        return total

    def orders(self) -> list[dict[str, Any]]:
        """全部工单（含台账状态）。"""
        return [self.status(r["ref"]) for r in self._events("order")]


__all__ = ["INVENTORY_ACCOUNT", "ManufacturingLane", "ORDER_DIMENSION", "SCHEMA",
           "WIP_ACCOUNT"]

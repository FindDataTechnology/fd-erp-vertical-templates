"""金蝶结算对账腿（add-supply-chain-vertical 任务 8.2 收尾 / change「金蝶凭证
推送 + 日终双边对账」）。

形态裁定：本模块面向的是**连接器网关契约**（push + 查双面），不是只读连接器
—— connector_bindings「kingdee-cloud」的 direction = push-via-connector +
read-back：凭证推送经萬星连接器执行到锚点实例（金蝶云星空），模板层只面向
网关契约留痕与对账，零直连外部系统、零凭据接触（BYOK 归连接器载体）。
因此网关接口不落 connectors/（那里是 read 面滩头，边界审计口径一致）。

对账驱动（settlement）：
- period_vouchers：从账本收取凭证面（经 KernelClient.query_entries 按账期/
  来源过滤，provenance 逐行引用），按事务聚合为凭证推送清单；不平凭证
  fail-closed 拒收（不推送）；
- push_vouchers → 锚点实例回执（外部凭证号/状态）；同凭证 stable_id 幂等
  （重推不重不漏）；
- 日终双边对账：本地凭证 vs 锚点实例回执，复用 three_way 对齐结构
  （四类差异 + 双侧引用：missing_in_right=锚点侧缺笔 / extra_in_right=
  锚点侧多出（本地缺笔）/ amount_mismatch=金额不符 / status_mismatch=
  状态不符）；
- 对账结论经既有 writeback 白名单车道回写核对记录：差异逐条更正提案
  （1300/4000 白名单科目）或拍板升级（状态不符，金额一致无更正分录）；
  纯推送对账零差异 → 回写确认记录（自平核对分录：借贷同科目等额，若经
  确认门落账净额为零、不改任何余额——纯留痕核对确认）。

客户中性：注释与输出只写「锚点实例」（行业垂直模板不绑定具名客户）。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from ..errors import TemplateError
from ..kernel_client import KernelClient
from .crossborder import WRITEBACK_ALLOWED_ACCOUNTS
from .three_way import align_pair
from .writeback import escalate_diff, writeback_recon_conclusion

#: 结算对账对齐双侧命名（沿 three_way 惯例：missing_in_right / extra_in_right /
#: amount_mismatch / status_mismatch）
LEFT_SIDE = "ledger-vouchers"
RIGHT_SIDE = "kingdee-receipts"


def _dec(value: Any, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what} 非法：{value!r}") from exc


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


# ---------------------------------------------------------------------------
# 连接器网关契约（push + 查双面；实现经萬星连接器，模板层零直连）
# ---------------------------------------------------------------------------

class KingdeeGateway(Protocol):
    """锚点实例凭证网关契约（不是只读连接器）：推送凭证 + 查回执/往来。

    生产实现 = 萬星连接器 kingdee provider（push + query action，BYOK 凭证
    不出库）；接口语义与连接器 provider 的 push/query action 对齐。
    """

    def push_vouchers(self, vouchers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """推送凭证面到锚点实例，返回回执（外部凭证号/状态；同 stable_id 幂等）。"""
        ...

    def query_vouchers(self, period: str) -> list[dict[str, Any]]:
        """查锚点实例侧凭证回执/往来记录（账期过滤；标准化流水形状）。"""
        ...

    def describe(self) -> dict[str, Any]:
        """载体描述（外部系统/载体/是否外部写面——如实标注）。"""
        ...


class DemoKingdeeGateway:
    """合成锚点实例网关（测试/demo，非生产厂商）：内存存回执，零凭据。

    可注入对账差异场景（供日终双边对账差异测试）：
    - drop_stable_ids：推送已受理但回执缺失（缺笔 → missing_in_right）；
    - amount_overrides：回执金额与本地不符（→ amount_mismatch）；
    - status_overrides：回执状态与本地不符（→ status_mismatch）；
    - extra_receipts：锚点侧存在本地从未推送的往来记录（→ extra_in_right）。

    幂等：按凭证 stable_id 去重——同凭证重推返回首次回执（duplicate=True），
    回执库不重不漏。全部状态在内存，零真实外部写入。
    """

    def __init__(self, *, external_system: str = "锚点实例 ERP",
                 drop_stable_ids: set[str] | None = None,
                 amount_overrides: dict[str, str] | None = None,
                 status_overrides: dict[str, str] | None = None,
                 extra_receipts: list[dict[str, Any]] | None = None):
        self._external = external_system
        self._drop = set(drop_stable_ids or ())
        self._amount_overrides = dict(amount_overrides or {})
        self._status_overrides = dict(status_overrides or {})
        self._receipts: dict[str, dict[str, Any]] = {}
        self._push_trace: list[dict[str, Any]] = []
        for extra in extra_receipts or []:
            sid = str(extra.get("stable_id") or "").strip()
            if not sid:
                raise TemplateError("INVALID_ARGUMENT", "注入回执缺 stable_id")
            self._receipts[sid] = {
                "stable_id": sid,
                "receipt_no": str(extra.get("receipt_no")
                                  or self._receipt_no(sid)),
                "amount": str(extra.get("amount") or "0"),
                "currency": str(extra.get("currency") or "CNY"),
                "status": str(extra.get("status") or "posted"),
                "occurred_at": str(extra.get("occurred_at") or ""),
                "voucher_no": extra.get("voucher_no"),
            }

    @staticmethod
    def _receipt_no(stable_id: str) -> str:
        return f"KD-{hashlib.sha1(stable_id.encode()).hexdigest()[:10].upper()}"

    def push_vouchers(self, vouchers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """推送凭证面：回执按 stable_id 幂等（重推返回首次结果，不重不漏）。"""
        receipts: list[dict[str, Any]] = []
        for v in vouchers:
            sid = str(v.get("stable_id") or "").strip()
            if not sid:
                raise TemplateError("INVALID_ARGUMENT", "推送凭证缺 stable_id")
            self._push_trace.append({"stable_id": sid,
                                     "amount": str(v.get("amount"))})
            if sid in self._receipts:
                receipts.append({**self._receipts[sid], "duplicate": True})
                continue
            if sid in self._drop:
                # 回执缺失注入（缺笔场景）：推送受理但锚点侧无凭证可查
                receipts.append({"stable_id": sid, "accepted": False,
                                 "status": "receipt-lost", "duplicate": False,
                                 "note": "回执缺失（注入场景）：锚点侧无对应凭证"})
                continue
            amount = self._amount_overrides.get(sid, str(v.get("amount") or "0"))
            status = self._status_overrides.get(sid, "posted")
            rec = {
                "stable_id": sid,
                "receipt_no": self._receipt_no(sid),
                "amount": amount,
                "currency": str(v.get("currency") or "CNY"),
                "status": status,
                "occurred_at": str(v.get("occurred_at") or ""),
                "voucher_no": v.get("voucher_no"),
            }
            self._receipts[sid] = rec
            receipts.append({**rec, "duplicate": False})
        return receipts

    def query_vouchers(self, period: str) -> list[dict[str, Any]]:
        """查锚点实例侧回执/往来（three_way 标准流水形状；link=凭证 stable_id）。"""
        out: list[dict[str, Any]] = []
        for sid, rec in self._receipts.items():
            if period and not str(rec.get("occurred_at") or "").startswith(period):
                continue
            out.append({
                "stable_id": sid,
                "amount": str(rec.get("amount") or "0"),
                "currency": str(rec.get("currency") or "CNY"),
                "status": str(rec.get("status") or "posted"),
                "occurred_at": str(rec.get("occurred_at") or ""),
                "source_ref": f"kingdee:{rec.get('receipt_no')}",
                "raw": {"link": sid, "receipt_no": rec.get("receipt_no"),
                        "voucher_no": rec.get("voucher_no"),
                        "external_system": self._external},
            })
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "kingdee-gateway", "external_system": self._external,
                "carrier": "open-connector（萬星连接器）",
                "direction": "push-via-connector + read-back",
                "writes_external": True,
                "pushed_trace_count": len(self._push_trace),
                "note": "连接器网关契约（push+查双面）：实际外部写入经萬星连接器"
                        "执行，模板层零直连；本实现为合成 demo（内存回执，零凭据）"}


# ---------------------------------------------------------------------------
# 结算对账驱动（凭证面收取 → 推送 → 日终双边对账 → 白名单车道回写）
# ---------------------------------------------------------------------------

class SettlementRecon:
    """金蝶结算对账驱动：账本凭证面 ↔ 锚点实例回执（单租户实例，互不混账）。"""

    def __init__(self, tenant_id: str, kernel: KernelClient, gateway: KingdeeGateway,
                 *, base_currency: str = "CNY"):
        self._tenant = tenant_id
        self._kernel = kernel
        self._gateway = gateway
        self._base = base_currency

    # ---- 1) 凭证面收取（按账期/来源过滤，provenance 逐行引用）----

    def period_vouchers(self, period: str, *,
                        source_prefix: str | None = None) -> list[dict[str, Any]]:
        """从账本收取账期内凭证面，聚合为推送清单（kv:<事务号> 稳定键）。

        - 经 KernelClient.query_entries 收分录面（模板层零直连账本）；
        - 账期过滤 = posted_at 前缀匹配 period（支持日/月口径）；来源过滤 =
          source_ref 前缀匹配（可选）；
        - 按事务聚合，凭证金额 = 本位币借方合计（借贷合计必须相等，不平
          fail-closed 拒收不推送）；
        - provenance 逐行引用（行号/科目/方向/金额/source_ref）随 raw 携带，
          供差异双侧引用与核对岗解释。
        """
        if not str(period or "").strip():
            raise TemplateError("INVALID_ARGUMENT", "账期 period 不能为空")
        result = self._kernel.query_entries(tenant_id=self._tenant)
        rows = list(result.get("entries") or [])
        by_txn: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            posted = str(row.get("posted_at") or "")
            if not posted.startswith(period):
                continue
            if source_prefix and not str(row.get("source_ref") or "") \
                    .startswith(source_prefix):
                continue
            by_txn.setdefault(str(row.get("txn_id")), []).append(row)

        vouchers: list[dict[str, Any]] = []
        for txn_id, lines in sorted(by_txn.items()):
            debit_total = sum(
                (_dec(l.get("amount_base") if l.get("amount_base") is not None
                      else l.get("amount"), "分录金额") for l in lines
                 if str(l.get("direction")).lower() == "debit"), Decimal(0))
            credit_total = sum(
                (_dec(l.get("amount_base") if l.get("amount_base") is not None
                      else l.get("amount"), "分录金额") for l in lines
                 if str(l.get("direction")).lower() == "credit"), Decimal(0))
            if debit_total != credit_total:
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"凭证不平（借 {_fmt(debit_total)} ≠ 贷 {_fmt(credit_total)}），"
                    f"fail-closed 拒收不推送：{txn_id}")
            first = lines[0]
            vouchers.append({
                "stable_id": f"kv:{txn_id}",
                "voucher_no": txn_id,
                "amount": _fmt(debit_total),
                "currency": self._base,
                "status": "posted",
                "occurred_at": str(first.get("posted_at") or ""),
                "source_ref": f"ledger:{txn_id}",
                "raw": {
                    "link": f"kv:{txn_id}",
                    "period": period,
                    "entry_count": len(lines),
                    "lines": [{
                        "line_no": l.get("line_no"),
                        "account_code": l.get("account_code"),
                        "direction": l.get("direction"),
                        "amount": str(l.get("amount")),
                        "amount_base": str(l.get("amount_base")),
                        "source_ref": l.get("source_ref"),
                    } for l in lines],
                },
            })
        return vouchers

    # ---- 2) 推送（幂等）----

    def push_period(self, period: str, *,
                    source_prefix: str | None = None) -> dict[str, Any]:
        """推送账期凭证面到锚点实例（网关按 stable_id 幂等，重推不重不漏）。"""
        vouchers = self.period_vouchers(period, source_prefix=source_prefix)
        receipts = self._gateway.push_vouchers(vouchers)
        duplicates = sum(1 for r in receipts if r.get("duplicate"))
        return {"period": period, "voucher_count": len(vouchers),
                "receipt_count": len(receipts), "duplicate_count": duplicates,
                "vouchers": vouchers, "receipts": receipts,
                "gateway": self._gateway.describe()}

    # ---- 3) 日终双边对账（复用 three_way 四类差异）----

    def reconcile_period(self, period: str, *,
                         source_prefix: str | None = None,
                         amount_tolerance: str = "0") -> dict[str, Any]:
        """本地凭证 vs 锚点实例回执逐笔对齐（四类差异 + 双侧引用 + 解释提示）。"""
        vouchers = self.period_vouchers(period, source_prefix=source_prefix)
        receipts = self._gateway.query_vouchers(period)
        aligned = align_pair(vouchers, receipts, left_side=LEFT_SIDE,
                             right_side=RIGHT_SIDE, amount_tolerance=amount_tolerance)
        aligned["period"] = period
        aligned["gateway"] = self._gateway.describe()
        aligned["caliber_note"] = (
            "结算对账双边口径：本地凭证面（kv:<事务号>，本位币借方合计）vs 锚点实例"
            "回执（连接器 query 面）；四类差异 + 双侧引用；对账零写外部系统，"
            "差异处理经白名单确认车道（proposal 待确认），模板层零直连锚点实例")
        return aligned

    # ---- 4) 差异 → 核对建议分录（白名单科目；状态不符无金额更正）----

    def propose_correction_lines(self, diff: dict[str, Any]) -> list[dict[str, Any]]:
        """差异 → 更正建议分录（1300/4000 白名单；只出建议面，不提交）。

        - missing_in_right（锚点侧缺笔）→ 借 1300 / 贷 4000（按左方金额）；
        - extra_in_right（锚点侧多出）→ 借 4000 / 贷 1300（按右方金额）；
        - amount_mismatch → 按差额借 1300 / 贷 4000；
        - status_mismatch → 金额一致无更正分录，走拍板升级（INVALID_ARGUMENT）。
        """
        kind = diff.get("kind")
        if kind == "amount_mismatch" and diff.get("left_amount") \
                and diff.get("right_amount"):
            amount = _fmt(abs(_dec(diff["left_amount"], "左方金额")
                              - _dec(diff["right_amount"], "右方金额")))
        else:
            amount = str(diff.get("left_amount") or diff.get("right_amount") or "")
        if kind == "status_mismatch":
            raise TemplateError(
                "INVALID_ARGUMENT",
                "状态不符差异金额一致，无更正分录可出（走拍板升级，"
                "由核对岗引用锚点实例状态证据定性）")
        if not amount or _dec(amount, "差异金额") <= 0:
            raise TemplateError("INVALID_ARGUMENT",
                                "差异缺金额或金额非正，无法出更正建议分录",
                                detail={"diff_kind": kind})
        if kind == "extra_in_right":
            lines = [{"account_code": "4000", "direction": "debit",
                      "amount": amount, "currency": self._base},
                     {"account_code": "1300", "direction": "credit",
                      "amount": amount, "currency": self._base}]
        else:  # missing_in_right / amount_mismatch → 差额挂应收更正
            lines = [{"account_code": "1300", "direction": "debit",
                      "amount": amount, "currency": self._base},
                     {"account_code": "4000", "direction": "credit",
                      "amount": amount, "currency": self._base}]
        for line in lines:
            if line["account_code"] not in WRITEBACK_ALLOWED_ACCOUNTS:
                raise TemplateError(
                    "BOUNDARY_VIOLATION",
                    f"回写科目 {line['account_code']} 不在白名单（资金类分录禁写）",
                    detail={"account_code": line["account_code"]})
        return lines

    # ---- 5) 日结关闭：推送 + 对账 + 白名单车道回写 ----

    def run_settlement_close(self, period: str, *, tenant_id: str,
                             fleet: str = "sc-settlement-fleet",
                             post: str = "reconciler", title: str = "核对岗",
                             source_prefix: str | None = None,
                             amount_tolerance: str = "0") -> dict[str, Any]:
        """结算日结：推送（幂等）→ 日终双边对账 → 差异逐条回写/升级 →
        零差异回写确认记录。

        - 差异（金额类）→ 更正提案（writeback 白名单车道，pending 待确认门）；
        - status_mismatch → 拍板升级单（核对岗无法定性 → owner 席位）；
        - 零差异 → 确认记录回写（自平核对分录：借贷同科目等额，若经确认门
          落账净额为零、不改任何余额——纯核对确认留痕）。
        """
        push = self.push_period(period, source_prefix=source_prefix)
        recon = self.reconcile_period(period, source_prefix=source_prefix,
                                      amount_tolerance=amount_tolerance)
        writebacks: list[dict[str, Any]] = []
        escalations: list[dict[str, Any]] = []
        for d in recon["diffs"]:
            if d["kind"] == "status_mismatch":
                escalations.append(escalate_diff(d, fleet=fleet))
                continue
            lines = self.propose_correction_lines(d)
            writebacks.append(writeback_recon_conclusion(
                self._kernel, tenant_id=tenant_id, fleet=fleet, post=post,
                title=title, diff=d, proposal_lines=lines,
                decision_note=f"结算对账差异处理（账期 {period}）："
                              f"{d.get('explanation_hint') or ''}".strip(),
                proposed_by="settlement-recon"))
        if not recon["diffs"]:
            total = _dec(recon["totals"]["left_amount"], "对账合计")
            if total > 0:
                marker_lines = [
                    {"account_code": "1300", "direction": "debit",
                     "amount": _fmt(total), "currency": self._base},
                    {"account_code": "1300", "direction": "credit",
                     "amount": _fmt(total), "currency": self._base},
                ]
                confirmed_diff = {
                    "kind": "settlement_confirmed",
                    "left_ref": f"period:{period}",
                    "right_ref": self._gateway.describe()["external_system"],
                    "explanation_hint": "双边零差异：核对确认记录（自平分录，"
                                        "若经确认门落账净额为零、不改任何余额）",
                }
                writebacks.append(writeback_recon_conclusion(
                    self._kernel, tenant_id=tenant_id, fleet=fleet, post=post,
                    title=title, diff=confirmed_diff, proposal_lines=marker_lines,
                    decision_note=f"结算对账零差异确认（账期 {period}）："
                                  f"{recon['matched_count']} 张凭证双边一致；"
                                  "自平核对记录，确认落账净额为零",
                    proposed_by="settlement-recon"))
        return {"period": period, "push": {
                    "voucher_count": push["voucher_count"],
                    "duplicate_count": push["duplicate_count"],
                    "gateway": push["gateway"]},
                "recon": recon, "writeback_proposals": writebacks,
                "escalations": escalations,
                "close_note": "结算日结：推送幂等 + 四类差异 + 白名单车道回写；"
                              "模板层对锚点实例只有网关契约面（实际写入经连接器）"}

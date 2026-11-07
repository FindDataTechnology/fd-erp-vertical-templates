"""对账结论回写 —— 内核白名单车道（erp-vertical-crossborder「对账舰队」/
restaurant「门店日结舰队」spec 场景：处理结论经内核白名单车道回写核对记录）。

车道纪律（erp-ledger-kernel「agent 只写 proposal 的确认车道」）：
- 拍板结论回写 = submit_proposal（proposal 待确认），不直接落账；
- 落账由拍板席（owner）经 confirm_proposal 确认门完成 —— 本模块不调用它；
- 机器车道禁写资金类分录：回写科目白名单（WRITEBACK_ALLOWED_ACCOUNTS）
  之外（含 1000 现金/权益类）一律拒绝；
- 资金执行类请求（分账/代收代付）在模板层直接拒绝（shares.assert_no_execution）。
"""
from __future__ import annotations

import hashlib
from typing import Any

from ..errors import TemplateError
from ..kernel_client import KernelClient
from .crossborder import WRITEBACK_ALLOWED_ACCOUNTS

#: 资金执行类术语（出现在核对结论文本即拒绝 —— 二清红线模板层落死）
FUND_EXECUTION_TERMS = ("分账执行", "代收代付", "资金池", "划款执行", "自动打款")


def writeback_recon_conclusion(kernel: KernelClient, *, tenant_id: str,
                               fleet: str, post: str, title: str,
                               diff: dict[str, Any],
                               proposal_lines: list[dict[str, Any]],
                               decision_note: str,
                               proposed_by: str = "recon-fleet") -> dict[str, Any]:
    """把已拍板的差异核对结论以 proposal 形态回写（pending，待确认门落账）。

    stable_id 幂等（同差异重推只落一次提案）；lane_* 携带 fleet identity
    turn 车道语义（柏讯 propose/confirm 契约对齐）。
    """
    text = f"{decision_note} {diff.get('explanation_hint') or ''}"
    for term in FUND_EXECUTION_TERMS:
        if term in text:
            raise TemplateError(
                "SHARE_EXECUTION_DENIED",
                f"核对结论含资金执行语义「{term}」——模板禁写资金执行面（二清红线）")
    for line in proposal_lines:
        code = str(line.get("account_code"))
        if code not in WRITEBACK_ALLOWED_ACCOUNTS:
            raise TemplateError(
                "BOUNDARY_VIOLATION",
                f"回写科目 {code} 不在白名单（资金类分录禁写）",
                detail={"allowed": sorted(WRITEBACK_ALLOWED_ACCOUNTS)})
        if code in ("1000", "2100", "3000", "3100"):
            raise TemplateError(
                "BOUNDARY_VIOLATION",
                f"科目 {code} 属资金/权益类，机器车道禁写")
    stable_id = hashlib.sha1(
        f"{tenant_id}|recon-writeback|{diff.get('kind')}|"
        f"{diff.get('left_ref')}|{diff.get('right_ref')}".encode()).hexdigest()
    return kernel.submit_proposal(
        stable_id=stable_id,
        lines=proposal_lines,
        source_ref=f"recon:{diff.get('kind')}:{diff.get('left_ref')}"
                   f"->:{diff.get('right_ref')}",
        kind="journal",
        lane_fleet=fleet, lane_post=post, lane_title=title,
        lane_target=f"diff:{diff.get('kind')}",
        tenant_id=tenant_id,
        submitted_by=proposed_by,
    )


def escalate_diff(diff: dict[str, Any], *, fleet: str, threshold: str = "high",
                  ) -> dict[str, Any]:
    """差异升级拍板单（owner 席位）。返回拍板单结构（车队 persona 消费）。"""
    severity = "high" if diff.get("kind") in ("amount_mismatch", "missing_in_right") \
        else "medium"
    return {
        "escalation_id": hashlib.sha1(
            f"{fleet}|{diff.get('kind')}|{diff.get('left_ref')}|"
            f"{diff.get('right_ref')}".encode()).hexdigest()[:16],
        "severity": severity, "threshold": threshold,
        "diff": diff, "fleet": fleet,
        "seat": "owner-拍板席",
        "note": "核对岗无法定性 → escalate 到 owner 席位拍板；"
                "拍板结论经 writeback_recon_conclusion 回写（proposal 待确认）",
    }

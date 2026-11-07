"""三方流逐笔对齐（erp-ledger-kernel 对账滩头之上的模板层驱动）。

四类差异（与内核 recon 语义一致）：缺失 / 多余 / 金额不符 / 状态不符，
每条差异含双侧原始引用（source_ref + raw 快照）与金额/币种/日期摘要
（供差异呈现折算与核对建议分录消费）。逐店出账互不混账。

对齐策略（三步）：
1. 业务关联键优先（raw.link / raw.order_id —— 平台结算单引用订单号）。
2. 无关联键时按「同日 + 同币种 + 金额相等（容差内）」贪心配对。
3. 残差二次配对：同日同币种的未配记录两两配对进入 _judge ——
   金额差即 amount_mismatch（不是 missing+extra 双假差异）；
   日/币种都对不上的才落 missing/extra。
重推去重：同侧同 stable_id 视为同一条记录的重推，只参与一次（计数入
duplicates_dropped）。
库存/在途类是状态对齐（非金额）：按关联键核对存在性与状态，不做金额配对。

物流/合规解释：本层 MUST NOT 重造物流/合规判定 —— 涉及在途/签收/供给的
差异只打 needs_explanation 提示槽，由舰队核对岗引用对应舰观测证据解释
（spec「物流与合规信息联动」）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import TemplateError

DIFF_KINDS = ("missing_in_right", "extra_in_right", "amount_mismatch", "status_mismatch")


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"金额非法：{value!r}") from exc


def align_pair(left: list[dict[str, Any]], right: list[dict[str, Any]], *,
               left_side: str, right_side: str,
               amount_tolerance: str = "0") -> dict[str, Any]:
    """双侧流逐笔对齐。left/right 为标准化流水 dict 列表（见连接器 normalize）。

    返回：{matched: [...], diffs: [四类差异...], totals: {...},
          duplicates_dropped: n}。
    每条差异：{kind, left_ref, right_ref, left_raw, right_raw,
              left_amount, right_amount, left_currency, right_currency,
              explanation_hint}——双侧原始引用 + 原始记录快照 + 金额摘要。
    """
    tol = _dec(amount_tolerance)

    # ---- 重推去重：同侧同 stable_id 只参与一次 ----
    left, left_dropped = _dedup(left)
    right, right_dropped = _dedup(right)
    dropped = left_dropped + right_dropped

    matched: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    used_right: set[int] = set()

    # ---- 1) 业务关联键优先 ----
    linked_right: set[int] = set()
    link_miss_left: list[dict[str, Any]] = []
    free_left: list[dict[str, Any]] = []
    for l in left:
        link = _link_of(l)
        if not link:
            free_left.append(l)
            continue
        hit_idx = next((i for i, r in enumerate(right)
                        if i not in used_right and i not in linked_right
                        and _link_of(r) == link), None)
        if hit_idx is None:
            link_miss_left.append(l)
            continue
        linked_right.add(hit_idx)
        _judge(l, right[hit_idx], matched, diffs, tol, left_side, right_side)

    # ---- 2) 同日+同币种+金额（容差内）贪心配对 ----
    residual_left: list[dict[str, Any]] = []
    for l in free_left:
        day, cur = _day_of(l), str(l.get("currency"))
        cand_idx = next((i for i, r in enumerate(right)
                         if i not in used_right and i not in linked_right
                         and _day_of(r) == day and str(r.get("currency")) == cur
                         and abs(_dec(r.get("amount")) - _dec(l.get("amount"))) <= tol),
                        None)
        if cand_idx is None:
            residual_left.append(l)
        else:
            used_right.add(cand_idx)
            _judge(l, right[cand_idx], matched, diffs, tol, left_side, right_side)

    # ---- 3) 残差二次配对（同日同币种，金额不等 → amount_mismatch）----
    residual_right = [i for i, r in enumerate(right)
                      if i not in used_right and i not in linked_right]
    for l in residual_left:
        day, cur = _day_of(l), str(l.get("currency"))
        cand_idx = next((i for i in residual_right
                         if _day_of(right[i]) == day
                         and str(right[i].get("currency")) == cur), None)
        if cand_idx is not None:
            residual_right.remove(cand_idx)
            used_right.add(cand_idx)
            _judge(l, right[cand_idx], matched, diffs, tol, left_side, right_side)
        else:
            diffs.append(_missing_diff(l, left_side, right_side))
    for l in link_miss_left:  # 有关联键但右侧未找到 → 缺笔
        diffs.append(_missing_diff(l, left_side, right_side))
    for i in residual_right:
        r = right[i]
        diffs.append({
            "kind": "extra_in_right", "left_ref": None,
            "right_ref": r.get("source_ref"), "left_raw": None,
            "right_raw": r.get("raw"),
            "left_amount": None, "right_amount": str(r.get("amount")),
            "left_currency": None, "right_currency": str(r.get("currency")),
            "explanation_hint": f"{left_side} 侧无对应流水（右侧多出）",
        })

    return {
        "left_side": left_side, "right_side": right_side,
        "matched_count": len(matched),
        "matched": matched, "diffs": diffs,
        "duplicates_dropped": dropped,
        "totals": {
            "left_count": len(left), "right_count": len(right),
            "left_amount": str(sum((_dec(x.get("amount")) for x in left), Decimal(0))),
            "right_amount": str(sum((_dec(x.get("amount")) for x in right), Decimal(0))),
            "diff_count": len(diffs),
            "by_kind": {k: sum(1 for d in diffs if d["kind"] == k) for k in DIFF_KINDS},
        },
    }


def _link_of(rec: dict[str, Any]) -> str:
    raw = rec.get("raw") or {}
    return str(raw.get("link") or raw.get("order_id") or "")


def _day_of(rec: dict[str, Any]) -> str:
    return str(rec.get("occurred_at") or "")[:10]


def _dedup(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """同 stable_id 重推去重（保首条，计数）。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    dropped = 0
    for r in records:
        sid = str(r.get("stable_id") or "")
        if sid and sid in seen:
            dropped += 1
            continue
        if sid:
            seen.add(sid)
        out.append(r)
    return out, dropped


def _missing_diff(l: dict[str, Any], left_side: str, right_side: str) -> dict[str, Any]:
    return {
        "kind": "missing_in_right", "left_ref": l.get("source_ref"),
        "right_ref": None, "left_raw": l.get("raw"), "right_raw": None,
        "left_amount": str(l.get("amount")), "right_amount": None,
        "left_currency": str(l.get("currency")), "right_currency": None,
        "left_day": _day_of(l), "right_day": None,
        "explanation_hint": f"{right_side} 侧未见对应流水",
    }


def _judge(l: dict[str, Any], r: dict[str, Any], matched: list[dict[str, Any]],
           diffs: list[dict[str, Any]], tol: Decimal,
           left_side: str, right_side: str) -> None:
    base = {
        "left_ref": l.get("source_ref"), "right_ref": r.get("source_ref"),
        "left_raw": l.get("raw"), "right_raw": r.get("raw"),
        "left_amount": str(l.get("amount")), "right_amount": str(r.get("amount")),
        "left_currency": str(l.get("currency")),
        "right_currency": str(r.get("currency")),
        "left_day": _day_of(l), "right_day": _day_of(r),
    }
    if str(l.get("currency")) != str(r.get("currency")):
        diffs.append({**base, "kind": "amount_mismatch",
                      "explanation_hint": f"币种不一致 {l.get('currency')} vs "
                                          f"{r.get('currency')}"})
        return
    delta = abs(_dec(r.get("amount")) - _dec(l.get("amount")))
    if delta > tol:
        diffs.append({**base, "kind": "amount_mismatch",
                      "explanation_hint": f"金额差 {delta}（左侧 {l.get('amount')}"
                                          f" vs 右侧 {r.get('amount')}）"})
        return
    if str(l.get("status")) != str(r.get("status")):
        diffs.append({**base, "kind": "status_mismatch",
                      "explanation_hint": f"状态不一致（左 {l.get('status')} / "
                                          f"右 {r.get('status')}）"})
        return
    matched.append({"left_ref": l.get("source_ref"),
                    "right_ref": r.get("source_ref"),
                    "amount": str(l.get("amount")), "currency": l.get("currency")})


def align_status(left: list[dict[str, Any]], right: list[dict[str, Any]], *,
                 left_side: str, right_side: str) -> dict[str, Any]:
    """状态对齐（库存/在途等非金额第三腿）：按关联键核对存在性。

    差异 explanation_hint 打 needs_explanation 槽（logistics/compliance），
    由舰队核对岗引用对应舰观测证据解释 —— 本层不判定物流因果。
    """
    right_keys: dict[str, dict[str, Any]] = {}
    for r in right:
        key = _link_of(r) or str(r.get("stable_id"))
        right_keys[key] = r
    matched: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for l in left:
        key = _link_of(l) or str(l.get("stable_id"))
        hit = right_keys.get(key)
        if hit is None:
            diffs.append({
                "kind": "missing_in_right", "left_ref": l.get("source_ref"),
                "right_ref": None, "left_raw": l.get("raw"), "right_raw": None,
                "left_amount": str(l.get("amount")), "right_amount": None,
                "left_currency": str(l.get("currency")), "right_currency": None,
                "explanation_hint":
                    f"{right_side} 侧未见对应记录；needs_explanation=logistics "
                    "(引用物流舰观测证据判在途/签收，不在对账层猜测)",
            })
            continue
        seen.add(key)
        if str(l.get("status")) != str(hit.get("status")):
            diffs.append({
                "kind": "status_mismatch", "left_ref": l.get("source_ref"),
                "right_ref": hit.get("source_ref"), "left_raw": l.get("raw"),
                "right_raw": hit.get("raw"),
                "left_amount": str(l.get("amount")),
                "right_amount": str(hit.get("amount")),
                "left_currency": str(l.get("currency")),
                "right_currency": str(hit.get("currency")),
                "explanation_hint":
                    f"状态不一致（左 {l.get('status')} / 右 {hit.get('status')}）；"
                    "needs_explanation=logistics",
            })
        else:
            matched.append({"left_ref": l.get("source_ref"),
                            "right_ref": hit.get("source_ref"), "key": key})
    for key, r in right_keys.items():
        if key not in seen:
            diffs.append({
                "kind": "extra_in_right", "left_ref": None,
                "right_ref": r.get("source_ref"), "left_raw": None,
                "right_raw": r.get("raw"),
                "left_amount": None, "right_amount": str(r.get("amount")),
                "left_currency": None, "right_currency": str(r.get("currency")),
                "explanation_hint": f"{left_side} 侧无对应记录（右侧多出）",
            })
    return {"left_side": left_side, "right_side": right_side,
            "matched_count": len(matched), "matched": matched, "diffs": diffs,
            "totals": {"left_count": len(left), "right_count": len(right),
                       "diff_count": len(diffs),
                       "by_kind": {k: sum(1 for d in diffs if d["kind"] == k)
                                   for k in DIFF_KINDS}}}

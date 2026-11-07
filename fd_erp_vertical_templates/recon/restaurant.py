"""餐饮门店对账与日结驱动（erp-vertical-restaurant-chain 任务 3.4 + 3.5）。

三方流：门店 POS 日流水 ↔ 收款渠道结算流 ↔ 外卖平台结算流，逐店出账。
日结（design D5 滩头）：每日出门店营业日报三段 —— 流水 / 收款 / 差异。
对账 MUST NOT 写任何渠道/平台侧数据（连接器只读）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..connectors.payments import PaymentChannelAdapter
from ..connectors.pos import PosVendorAdapter
from ..errors import TemplateError
from .three_way import align_pair


class StoreDailyClose:
    """单店单日三方对账 + 日结日报。多门店 = 每店一个实例（互不混账）。"""

    def __init__(self, store_id: str, *, pos_adapter: PosVendorAdapter,
                 channel_adapters: dict[str, PaymentChannelAdapter],
                 delivery_adapter: PaymentChannelAdapter | None = None):
        self._store = store_id
        self._pos = pos_adapter
        self._channels = channel_adapters
        self._delivery = delivery_adapter

    def daily_close(self, day: str) -> dict[str, Any]:
        """日结：拉三流 → 逐笔对齐 → 日报三段（流水/收款/差异）。"""
        pos_flows = self._pos.read_day_flows(self._store, day)

        channels: dict[str, list[dict[str, Any]]] = {}
        for name, adapter in self._channels.items():
            channels[name] = adapter.read_settlements(self._store, day)

        delivery: list[dict[str, Any]] = []
        if self._delivery is not None:
            delivery = self._delivery.read_settlements(self._store, day)

        # 流水段：POS 流水按渠道归类（raw.channel 或按结算关联）
        flows_section = self._flows_section(pos_flows)
        # 收款段：各渠道结算总额 + 手续费
        receipts_section = self._receipts_section(channels, delivery)
        # 差异段：POS ↔ 每渠道逐笔对齐 + 外卖结算对齐
        diffs_section = self._diffs_section(pos_flows, channels, delivery, day)

        return {
            "store_id": self._store, "day": day,
            "report": {"flows": flows_section, "receipts": receipts_section,
                       "diffs": diffs_section},
            "sections_note": "日报三段：流水/收款/差异（design D5）",
        }

    # ---- 日报三段 ----

    def _flows_section(self, pos_flows: list[dict[str, Any]]) -> dict[str, Any]:
        total = sum((Decimal(str(f.get("amount") or "0")) for f in pos_flows),
                    Decimal(0))
        by_channel: dict[str, Decimal] = {}
        for f in pos_flows:
            ch = str((f.get("raw") or {}).get("channel") or "unclassified")
            by_channel[ch] = by_channel.get(ch, Decimal(0)) + Decimal(str(f.get("amount") or "0"))
        return {"pos_flow_count": len(pos_flows),
                "pos_total": str(total),
                "by_channel": {k: str(v) for k, v in sorted(by_channel.items())}}

    def _receipts_section(self, channels: dict[str, list[dict[str, Any]]],
                          delivery: list[dict[str, Any]]) -> dict[str, Any]:
        rows: dict[str, Any] = {}
        for name, records in channels.items():
            settle = sum((Decimal(str(r.get("amount") or "0")) for r in records),
                         Decimal(0))
            fees = sum((Decimal(str((r.get("raw") or {}).get("fee") or "0"))
                        for r in records), Decimal(0))
            rows[name] = {"settlement_total": str(settle), "fee_total": str(fees),
                          "record_count": len(records)}
        if self._delivery is not None:
            d_settle = sum((Decimal(str(r.get("amount") or "0")) for r in delivery),
                           Decimal(0))
            d_fees = sum((Decimal(str((r.get("raw") or {}).get("fee") or "0"))
                          for r in delivery), Decimal(0))
            rows["delivery"] = {"settlement_total": str(d_settle),
                                "fee_total": str(d_fees),  # 外卖佣金（平台费率解释）
                                "record_count": len(delivery)}
        return rows

    def _diffs_section(self, pos_flows: list[dict[str, Any]],
                       channels: dict[str, list[dict[str, Any]]],
                       delivery: list[dict[str, Any]], day: str) -> dict[str, Any]:
        diffs: list[dict[str, Any]] = []
        matched_total = 0
        # POS（按渠道归类）↔ 该渠道结算，逐笔对齐
        pos_channels = {str((f.get("raw") or {}).get("channel") or "unclassified")
                        for f in pos_flows}
        for name, records in channels.items():
            side_flows = [f for f in pos_flows
                          if str((f.get("raw") or {}).get("channel") or "") == name]
            result = align_pair(side_flows, records, left_side=f"pos:{name}",
                                right_side=f"settle:{name}")
            matched_total += result["matched_count"]
            for d in result["diffs"]:
                d["pair"] = f"pos↔{name}"
                diffs.append(d)
        # 未适配渠道如实报出（POS 有流水但渠道无适配器 = 不能静默消失）
        for ch in sorted(pos_channels - set(channels)):
            if ch == "unclassified":
                continue
            side_flows = [f for f in pos_flows
                          if str((f.get("raw") or {}).get("channel") or "") == ch]
            diffs.append({
                "kind": "missing_in_right", "left_ref": side_flows[0]["source_ref"],
                "right_ref": None, "left_raw": side_flows[0].get("raw"),
                "right_raw": None,
                "left_amount": str(side_flows[0].get("amount")),
                "right_amount": None,
                "left_currency": str(side_flows[0].get("currency")),
                "right_currency": None,
                "explanation_hint": f"渠道 {ch} 无已配置适配器（NOT_WIRED 如实报出，"
                                    "不以人工补录冒充自动接入）",
                "pair": f"pos↔{ch}(unadapted)",
            })
        # 佣金差异：外卖结算费 vs 平台费率（费率带出处方可解释）
        if self._delivery is not None and delivery:
            commission_diffs = self._commission_diffs(delivery)
            diffs.extend(commission_diffs)
        by_kind: dict[str, int] = {}
        for d in diffs:
            by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
        return {"diff_count": len(diffs), "by_kind": by_kind, "items": diffs}

    def _commission_diffs(self, delivery: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """外卖佣金核对：佣金率缺出处 → 如实 unexplained（不臆测）。"""
        out: list[dict[str, Any]] = []
        for r in delivery:
            fee = (r.get("raw") or {}).get("fee")
            order_amount = (r.get("raw") or {}).get("order_amount")
            if fee is None or order_amount in (None, "0", 0):
                continue
            out.append({
                "kind": "amount_mismatch",
                "left_ref": r.get("source_ref"), "right_ref": None,
                "left_raw": r.get("raw"), "right_raw": None,
                "explanation_hint":
                    f"外卖结算费 {fee} / 订单额 {order_amount}：佣金差异须按平台费率"
                    "出处解释（费率未配置出处 → unexplained，须核对岗引用平台费率证据）",
                "pair": "delivery-commission",
            })
        return out

    # ---- 差异 → 核对建议分录（与跨境同白名单科目纪律）----

    def propose_correction_lines(self, diff: dict[str, Any]) -> list[dict[str, Any]]:
        from .crossborder import WRITEBACK_ALLOWED_ACCOUNTS, _diff_amount
        amount = _diff_amount(diff)
        if not amount or Decimal(str(amount)) <= 0:
            raise TemplateError("INVALID_ARGUMENT",
                                "差异缺金额或金额非正，无法出核对建议分录",
                                detail={"diff_kind": diff.get("kind")})
        if diff.get("kind") == "missing_in_right":
            lines = [{"account_code": "1300", "direction": "debit", "amount": amount,
                      "currency": "CNY"},
                     {"account_code": "4000", "direction": "credit", "amount": amount,
                      "currency": "CNY"}]
        elif diff.get("kind") == "extra_in_right":
            lines = [{"account_code": "4000", "direction": "debit", "amount": amount,
                      "currency": "CNY"},
                     {"account_code": "1300", "direction": "credit", "amount": amount,
                      "currency": "CNY"}]
        else:
            lines = [{"account_code": "1300", "direction": "debit", "amount": amount,
                      "currency": "CNY"},
                     {"account_code": "4000", "direction": "credit", "amount": amount,
                      "currency": "CNY"}]
        for line in lines:
            if line["account_code"] not in WRITEBACK_ALLOWED_ACCOUNTS:
                raise TemplateError(
                    "BOUNDARY_VIOLATION",
                    f"回写科目 {line['account_code']} 不在白名单（资金类分录禁写）")
        return lines

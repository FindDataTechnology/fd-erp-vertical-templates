"""库存台账账实一致（add-supply-chain-vertical 任务 5.x / spec「仓配与交付编排」）。

- 只读 WMS 快照适配器：接口 + DemoWmsAdapter 合成实现（read-only）。
  真实 WMS provider 按萬星连接器 SDK 新增（open-connector-mt 立项），
  本仓只消费快照契约。
- 日终账实对账：账本库存科目（分录侧，source_ref 携带商品/仓维度）vs
  WMS 快照 → 差异清单，复用 three_way 差异结构（缺失/多余/数量不符/
  状态不符 + 双侧引用）。
- 编排层零写 WMS：本模块对 WMS 只有 read 面（boundary 审计口径一致）。

账侧位置来源约定：库存分录 proposal 的 source_ref 采用
「inv:<product_code>:<warehouse>」结构化键（模板回写/账务面提交时写入），
本模块按该键聚合借贷数量方向，得账面库位数量；无法解析的分录如实列入
unparsed_entries（不静默吞掉）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import TemplateError
from .kernel_client import KernelClient
from .recon.three_way import align_pair

#: 账面 source_ref 结构化键前缀（库存维度分录）
INV_REF_PREFIX = "inv:"
#: 计数流单位（库存对账按数量，不按金额）
QTY_CURRENCY = "EA"


class WmsSnapshotAdapter(ABC):
    """WMS 库存快照适配器接口：只读（编排层零写 WMS）。

    read_inventory_snapshot 返回标准化库位快照：
    {product_code, warehouse, qty, status, occurred_at, source_ref, raw}
    """

    @abstractmethod
    def read_inventory_snapshot(self, warehouse: str | None = None
                                ) -> list[dict[str, Any]]:
        """读库存快照（全仓或指定仓）。只读。"""
        ...

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        ...


class DemoWmsAdapter(WmsSnapshotAdapter):
    """合成 WMS 适配器（测试/demo 用，非生产厂商）：注入快照数据，只读。"""

    def __init__(self, positions: list[dict[str, Any]]):
        self._positions = [dict(p) for p in positions]

    def read_inventory_snapshot(self, warehouse: str | None = None
                                ) -> list[dict[str, Any]]:
        out = []
        for p in self._positions:
            if warehouse and str(p.get("warehouse")) != warehouse:
                continue
            out.append({
                "product_code": str(p.get("product_code")),
                "warehouse": str(p.get("warehouse")),
                "qty": str(p.get("qty") or "0"),
                "status": str(p.get("status") or "available"),
                "occurred_at": str(p.get("occurred_at") or ""),
                "source_ref": str(p.get("source_ref")
                                  or f"wms:{p.get('warehouse')}:{p.get('product_code')}"),
                "raw": dict(p.get("raw") or {"product_code": p.get("product_code"),
                                             "warehouse": p.get("warehouse")}),
            })
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "wms", "vendor": "demo-synthetic",
                "writes_external": False,
                "note": "合成 WMS（测试/demo），只读快照面；"
                        "真实 WMS provider 归萬星连接器（open-connector-mt）"}


def _qty(value: Any, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise TemplateError("INVALID_ARGUMENT", f"{what} 非法：{value!r}") from exc


def _inv_key_of(source_ref: str) -> tuple[str, str] | None:
    """解析账面 source_ref 的库存维度键 inv:<product_code>:<warehouse>。"""
    text = str(source_ref or "")
    if not text.startswith(INV_REF_PREFIX):
        return None
    parts = text[len(INV_REF_PREFIX):].split(":")
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def book_positions_from_entries(entries: list[dict[str, Any]]
                                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """账面库位数量：按 inv: 键聚合分录借贷（debit 入库 +、credit 出库 −）。

    返回（位置记录, 不可解析分录）：位置记录为 three_way 标准流水形状
    （raw.link = product|warehouse 供对齐关联键复用）。
    状态口径：账本分录只有 posted 态，库位状态统一按 available 口径比对 ——
    WMS 侧冻结/锁定/在途等非 available 库位状态即状态不符（进差异清单驱动处理）。
    """
    net: dict[tuple[str, str], dict[str, Any]] = {}
    unparsed: list[dict[str, Any]] = []
    for row in entries:
        key = _inv_key_of(row.get("source_ref"))
        if key is None:
            unparsed.append({"source_ref": row.get("source_ref"),
                             "txn_id": row.get("txn_id"),
                             "note": "分录无库存维度键（inv:<product>:<warehouse>），"
                                     "不入账实对账，如实列示"})
            continue
        signed = _qty(row.get("amount"), "分录数量")
        if str(row.get("direction") or "").lower() == "credit":
            signed = -signed
        elif str(row.get("direction") or "").lower() != "debit":
            unparsed.append({"source_ref": row.get("source_ref"),
                             "note": f"direction 非法：{row.get('direction')!r}"})
            continue
        bucket = net.setdefault(key, {"qty": Decimal(0), "refs": [],
                                      "status": "available", "occurred_at": ""})
        bucket["qty"] += signed
        bucket["refs"].append(f"{row.get('txn_id')}#{row.get('line_no')}")
        bucket["occurred_at"] = max(bucket["occurred_at"],
                                    str(row.get("posted_at") or ""))
    positions = []
    for (code, wh), b in sorted(net.items()):
        positions.append({
            "stable_id": f"book:{code}:{wh}",
            "amount": format(b["qty"].normalize(), "f"),
            "currency": QTY_CURRENCY,
            "status": b["status"],
            "occurred_at": b["occurred_at"],
            "source_ref": f"{INV_REF_PREFIX}{code}:{wh}",
            "raw": {"link": f"{code}|{wh}", "product_code": code,
                    "warehouse": wh, "entry_refs": b["refs"]},
        })
    return positions, unparsed


def _snapshot_records(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """WMS 快照 → three_way 标准流水形状（计数流，link = product|warehouse）。"""
    recs = []
    for p in positions:
        code, wh = str(p.get("product_code")), str(p.get("warehouse"))
        recs.append({
            "stable_id": f"wms:{code}:{wh}",
            "amount": str(p.get("qty") or "0"),
            "currency": QTY_CURRENCY,
            "status": str(p.get("status") or "available"),
            "occurred_at": str(p.get("occurred_at") or ""),
            "source_ref": str(p.get("source_ref")
                              or f"wms:{wh}:{code}"),
            "raw": {"link": f"{code}|{wh}", **(p.get("raw") or {})},
        })
    return recs


def recon_book_vs_wms(kernel: KernelClient, wms: WmsSnapshotAdapter, *,
                      tenant_id: str, account_code: str = "1200",
                      warehouse: str | None = None,
                      amount_tolerance: str = "0") -> dict[str, Any]:
    """日终账实对账：账本库存科目分录（聚合库位数量）vs WMS 快照。

    差异复用 three_way 四类结构（missing_in_right=账有实无 / extra_in_right=
    实有账无 / amount_mismatch=数量不符 / status_mismatch=状态不符），
    每条差异双侧引用（账面 txn#line 列表 / WMS source_ref）。
    只读：query_entries + 快照 read，编排层零写 WMS、零写账本。
    """
    result = kernel.query_entries(account_code=account_code, tenant_id=tenant_id)
    entries = list(result.get("entries") or [])
    book, unparsed = book_positions_from_entries(entries)
    snapshot = _snapshot_records(wms.read_inventory_snapshot(warehouse))
    aligned = align_pair(book, snapshot, left_side="ledger-book",
                         right_side="wms-snapshot",
                         amount_tolerance=amount_tolerance)
    aligned["account_code"] = account_code
    aligned["unparsed_entries"] = unparsed
    aligned["caliber_note"] = (
        "账实对账按数量（EA）：账面 = 库存科目分录按 inv:<product>:<warehouse> "
        "键聚合借贷；差异四类 + 双侧引用；编排层零写 WMS（差异处理经确认车道）")
    return aligned

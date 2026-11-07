"""POS BYOK 适配器框架（erp-vertical-restaurant-chain 任务 3.2 / design D4）。

框架一个、厂商适配器逐个插拔：
- 首个适配厂商由首个真实客户的实际厂商决定（潜在客户厂商组合未定，
  不预设名单）——框架内置仅 synthetic（测试用）与 export-file（导出文件
  替代通道）。
- 授权逐店开通：每店凭商家授权（ERP_BYOK_POS_<厂商>_<店> 环境变量），
  凭证零落库。
- 未适配厂商如实报出（NOT_WIRED + 替代路径说明），MUST NOT 用人工补录
  冒充已适配厂商的自动接入。
"""
from __future__ import annotations

import csv
import hashlib
import io
from abc import ABC, abstractmethod
from typing import Any

from ..errors import TemplateError

#: 已注册厂商（插拔注册表）。真实厂商适配器随首单客户逐个上线。
VENDOR_REGISTRY: dict[str, type["PosVendorAdapter"]] = {}
#: 未适配厂商的如实替代路径口径
EXPORT_FILE_FALLBACK_NOTE = (
    "该 POS 厂商暂无适配器：可用替代路径为「门店日流水导出文件通道」"
    "（商户从厂商后台导出 CSV，经导出通道接入对账）；未适配前不提供自动接入，"
    "不以人工补录冒充。")


def register_vendor(cls: type["PosVendorAdapter"]) -> type["PosVendorAdapter"]:
    VENDOR_REGISTRY[cls.vendor] = cls
    return cls


def _stable_id(store_id: str, vendor: str, raw_id: str) -> str:
    return hashlib.sha1(f"{store_id}|pos:{vendor}|{raw_id}".encode()).hexdigest()


class PosVendorAdapter(ABC):
    """厂商适配器接口：只有 read 面（boundary 审计 connectors-readonly）。

    read_day_flows 返回标准化流水（stable_id/amount/currency/status/
    occurred_at/source_ref/raw），raw 保留厂商原始字段供差异引用。
    """

    vendor: str = "abstract"
    credential_env: str = ""

    @abstractmethod
    def read_day_flows(self, store_id: str, day: str) -> list[dict[str, Any]]:
        """读某店某日 POS 流水（逐笔）。只读；凭证缺失/厂商未适配如实报。"""
        ...

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        ...


class SyntheticPosAdapter(PosVendorAdapter):
    """synthetic 厂商：测试店合成数据（e2e 2.6/5.2 用），非生产厂商。"""

    vendor = "synthetic"
    credential_env = "ERP_BYOK_POS_SYNTHETIC"

    def __init__(self, flows: list[dict[str, Any]] | None = None):
        self._flows = flows or []

    def read_day_flows(self, store_id: str, day: str) -> list[dict[str, Any]]:
        out = []
        for f in self._flows:
            if f.get("day") != day:
                continue
            out.append({
                "stable_id": _stable_id(store_id, self.vendor, f.get("id")),
                "amount": str(f.get("amount") or "0"),
                "currency": str(f.get("currency") or "CNY"),
                "status": str(f.get("status") or "posted"),
                "occurred_at": f.get("occurred_at") or f"{day}T00:00:00",
                "source_ref": f"pos:{self.vendor}:{store_id}:{f.get('id')}",
                "raw": dict(f),
            })
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "pos", "vendor": self.vendor, "writes_external": False,
                "note": "synthetic 厂商（测试/e2e），非生产厂商"}


@register_vendor
class ExportFilePosAdapter(PosVendorAdapter):
    """导出文件替代通道：未适配厂商的如实路径（商户后台导出 CSV）。"""

    vendor = "export-file"
    credential_env = ""

    def __init__(self, csv_text: str):
        self._csv_text = csv_text

    def read_day_flows(self, store_id: str, day: str) -> list[dict[str, Any]]:
        rows = list(csv.DictReader(io.StringIO(self._csv_text)))
        out = []
        for r in rows:
            if str(r.get("day") or "") != day:
                continue
            if not r.get("id"):
                raise TemplateError(
                    "INVALID_ARGUMENT",
                    f"导出文件第 {rows.index(r) + 1} 行缺流水 id（导出通道要求"
                    "幂等键，否则对账差异不可解释）")
            out.append({
                "stable_id": _stable_id(store_id, self.vendor, r["id"]),
                "amount": str(r.get("amount") or "0"),
                "currency": str(r.get("currency") or "CNY"),
                "status": str(r.get("status") or "posted"),
                "occurred_at": r.get("occurred_at") or f"{day}T00:00:00",
                "source_ref": f"pos:export-file:{store_id}:{r['id']}",
                "raw": dict(r),
            })
        return out

    def describe(self) -> dict[str, Any]:
        return {"adapter": "pos", "vendor": self.vendor, "writes_external": False,
                "note": "导出文件替代通道（未适配厂商的如实路径）"}


def get_pos_adapter(vendor: str, *, store_id: str,
                    synthetic_flows: list[dict[str, Any]] | None = None,
                    export_csv: str | None = None) -> PosVendorAdapter:
    """按厂商取适配器（逐店授权校验 + 未适配如实报出）。

    - synthetic：e2e 专用（凭证 ERP_BYOK_POS_SYNTHETIC 须注入，验证 BYOK 面）。
    - export-file：替代通道（无凭证；文件本身是商户导出物）。
    - 其它厂商：进 VENDOR_REGISTRY 才可用；未注册 = 如实 NOT_WIRED。
    """
    import os
    if vendor == "synthetic":
        if not os.environ.get("ERP_BYOK_POS_SYNTHETIC"):
            raise TemplateError(
                "NOT_WIRED",
                f"POS 授权未开通：ERP_BYOK_POS_SYNTHETIC（store={store_id}）")
        return SyntheticPosAdapter(synthetic_flows)
    if vendor == "export-file":
        if not export_csv:
            raise TemplateError("INVALID_ARGUMENT",
                                "导出文件通道须提供导出 CSV 文本")
        return ExportFilePosAdapter(export_csv)
    cls = VENDOR_REGISTRY.get(vendor)
    if cls is None:
        raise TemplateError(
            "NOT_WIRED",
            f"POS 厂商 {vendor} 尚无适配器。{EXPORT_FILE_FALLBACK_NOTE}",
            detail={"vendor": vendor, "store_id": store_id,
                    "registered_vendors": sorted(VENDOR_REGISTRY)})
    token_env = f"ERP_BYOK_POS_{vendor.upper()}_{store_id.upper().replace('-', '_')}"
    if not os.environ.get(token_env):
        raise TemplateError(
            "NOT_WIRED",
            f"该店 POS 授权未开通：{token_env}（逐店开通，凭证零落库）",
            detail={"vendor": vendor, "store_id": store_id})
    return cls(store_id)  # type: ignore[call-arg]


def vendor_support_surface() -> dict[str, Any]:
    """如实厂商支持面（供客户问答/文档直接引用）。"""
    return {
        "registered_vendors": sorted(VENDOR_REGISTRY),
        "production_vendors": [],  # 首单客户驱动，当前为零（如实）
        "fallback": EXPORT_FILE_FALLBACK_NOTE,
    }

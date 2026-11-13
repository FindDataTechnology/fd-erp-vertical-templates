"""中国国别包加载（change: add-cn-localization-pack 任务 1.4）。

判据（对应 spec `erp-cn-localization` 第一条）：

1. 条数与上游一致（小企业 156 / 大企业 286 / 税码 62），类别全落在内核封闭五类；
2. 数据被改过（sha256 不符）即加载失败——不许静默用被改过的准则数据；
3. 父级悬空即失败（层级码断了，报表按前缀聚合会静默漏子树）；
4. 条数与 manifest 不符即失败（上游改了或转换出错）；
5. 税率档是封闭集 13/9/6/5/3/0，负数档（加计抵减/差额征税）不在内。

零 DB、零网络：只读仓内数据包。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.localization import (BASE, CHART_LARGE, CHART_SMALL,
                                                    CnPack, verify)

SMALL, LARGE, TAX = 156, 286, 62


def _copy_pack(tmp_path: Path) -> Path:
    dst = tmp_path / "cn"
    shutil.copytree(BASE, dst)
    return dst


def _resign(root: Path, name: str, *, count: int | None = None) -> None:
    """改完数据后重算 sha（模拟「改了数据也改了 manifest」）。"""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    meta = manifest["files"][name]
    meta["sha256"] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    if count is not None:
        meta["count"] = count
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                        encoding="utf-8")


# ---- 1) 条数与类别 ----

def test_pack_counts_match_upstream():
    small, large = CnPack(chart=CHART_SMALL), CnPack(chart=CHART_LARGE)
    assert len(small.accounts()) == SMALL
    assert len(large.accounts()) == LARGE
    assert len(small.tax_codes()) == TAX
    assert small.summary()["byKind"] == {"asset": 59, "equity": 5, "expense": 39,
                                         "liability": 45, "revenue": 8}
    assert {a["kind"] for a in large.accounts()} <= {
        "asset", "liability", "equity", "revenue", "expense"}


def test_tax_rates_are_the_closed_positive_set():
    assert CnPack().tax_rates() == ["13", "9", "6", "5", "3", "0"]


def test_vat_subtree_has_the_codes_we_book_against():
    """科目码必须来自数据，不来自记忆：2221.01.05 是出口退税，转出未交增值税是 2221.01.07。"""
    pack = CnPack()
    tree = {a["code"]: a["name"] for a in pack.under("2221")}
    assert tree["2221.01.01"].endswith("销项税额）")
    assert tree["2221.01.02"].endswith("进项税额）")
    assert tree["2221.01.07"].endswith("转出未交增值税）")
    assert tree["2221.01.05"].endswith("出口退税）")
    assert tree["2221.02"].endswith("未交增值税")
    assert pack.account("1122")["name"] == "应收账款"
    assert pack.account("2202")["name"] == "应付账款"


def test_hierarchy_resolves_through_upstream_ids():
    pack = CnPack()
    assert [a["code"] for a in pack.children("1002")] == ["1002.01"]
    assert pack.account("2221.01")["parent"] == "2221"
    assert [a["code"] for a in pack.children("2221.01")][:2] == [
        "2221.01.01", "2221.01.02"]


def test_verify_reports_every_file():
    out = verify()
    assert {f["file"] for f in out["files"]} == {
        CHART_SMALL, CHART_LARGE, "tax-codes.json",
        "mapping.json", "statements-small.json"}
    assert out["source"].startswith("odoo/addons/l10n_cn")


# ---- 2) 数据被改过就拒 ----

def test_a_tampered_data_file_is_refused(tmp_path):
    root = _copy_pack(tmp_path)
    data = json.loads((root / CHART_SMALL).read_text(encoding="utf-8"))
    data["accounts"][0]["name"] = "改过的名字"                 # 不改 manifest
    (root / CHART_SMALL).write_text(json.dumps(data, ensure_ascii=False),
                                    encoding="utf-8")
    with pytest.raises(TemplateError) as ei:
        CnPack(base=root)
    assert "sha256" in ei.value.message
    assert ei.value.detail["file"] == CHART_SMALL


# ---- 3) 父级悬空即拒 ----

def test_a_dangling_parent_is_refused(tmp_path):
    root = _copy_pack(tmp_path)
    data = json.loads((root / CHART_SMALL).read_text(encoding="utf-8"))
    data["accounts"][0]["parent"] = "9999"
    (root / CHART_SMALL).write_text(json.dumps(data, ensure_ascii=False),
                                    encoding="utf-8")
    _resign(root, CHART_SMALL, count=len(data["accounts"]))
    with pytest.raises(TemplateError) as ei:
        CnPack(base=root)
    assert ei.value.detail["missingParents"] == ["9999"]


# ---- 4) 条数不符即拒 ----

def test_a_count_mismatch_is_refused(tmp_path):
    root = _copy_pack(tmp_path)
    _resign(root, "tax-codes.json", count=999)
    with pytest.raises(TemplateError) as ei:
        CnPack(base=root)
    assert "条数为" in ei.value.message
    assert ei.value.detail["expected"] == 999


# ---- 5) 未知准则即拒 ----

def test_unknown_chart_is_refused():
    with pytest.raises(TemplateError) as ei:
        CnPack(chart="accounts-us.json")
    assert ei.value.detail["allowed"] == [CHART_SMALL, CHART_LARGE]

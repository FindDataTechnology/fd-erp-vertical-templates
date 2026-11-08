"""科目映射 pack 契约（任务 1.2 / 5.1）：加载、呈现层重算、替换映射不改账。"""
from __future__ import annotations

import copy
import os
import json
from pathlib import Path

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.mapping import (
    UNIVERSAL_ACCOUNT_CODES,
    apply_mapping,
    load_mapping_pack,
    replace_mapping,
)

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates"


def test_universal_codes_match_kernel_migration():
    """模板层通用科目集与内核迁移 0002 预设逐字对齐（不得漂移）。"""
    # 与 tests/conftest.py 同一开关：CI 上没有内核兄弟仓，本机可用 env 复现。
    kernel_root = Path(os.environ.get("ERP_LEDGER_KERNEL_ROOT")
                       or REPO.parent / "fd-erp-ledger-mcp")
    migration = kernel_root / "fd_erp_ledger_mcp" / "migrations" / "0002_universal_accounts.sql"
    if not migration.exists():
        # 内核是兄弟仓，CI 不检出它；这条对齐门在没有内核时无从核对，如实跳过。
        pytest.skip(f"内核仓不在位，无法核对科目集对齐（{migration}）")
    sql = migration.read_text(encoding="utf-8")
    seeded = set()
    for line in sql.splitlines():
        s = line.strip()
        if s.startswith("('"):
            seeded.add(s[2:s.find("'", 2)])
    assert seeded == set(UNIVERSAL_ACCOUNT_CODES), "模板与内核科目集漂移"


def test_load_both_template_mapping_packs():
    pack = load_mapping_pack(TEMPLATES / "crossborder" / "account-mapping" / "pack.json")
    assert pack["vertical"] == "crossborder"
    rest = load_mapping_pack(TEMPLATES / "restaurant-chain" / "account-mapping" / "pack.json")
    assert rest["vertical"] == "restaurant-chain"
    # 行业科目必须一一对齐内核科目（呈现重算不重复计数）
    for p in (pack, rest):
        targets = [a["maps_to"] for a in p["industry_accounts"]]
        assert len(targets) == len(set(targets))
        assert all(t in UNIVERSAL_ACCOUNT_CODES for t in targets)


def test_invalid_target_rejected():
    with pytest.raises(TemplateError) as ei:
        load_mapping_pack({"schema": "erp-account-mapping/v1", "version": "1.0.0",
                           "industry_accounts": [{"code": "B-1", "name_zh": "行业科目",
                                                  "maps_to": "9999"}],
                           "report_rows": [{"key": "r", "includes": ["B-1"]}]})
    assert ei.value.code == "MAPPING_INVALID"


def test_duplicate_target_rejected():
    with pytest.raises(TemplateError) as ei:
        load_mapping_pack({"schema": "erp-account-mapping/v1", "version": "1.0.0",
                           "industry_accounts": [
                               {"code": "A", "name_zh": "一", "maps_to": "4000"},
                               {"code": "B", "name_zh": "二", "maps_to": "4000"}],
                           "report_rows": [{"key": "r", "includes": ["A", "B"]}]})
    assert "重复" in ei.value.message or "重复计数" in ei.value.message


def test_kind_mismatch_rejected():
    with pytest.raises(TemplateError):
        load_mapping_pack({"schema": "erp-account-mapping/v1", "version": "1.0.0",
                           "industry_accounts": [{"code": "A", "name_zh": "收入科目",
                                                  "maps_to": "5100", "kind": "revenue"}],
                           "report_rows": [{"key": "r", "includes": ["A"]}]})


def test_apply_mapping_presentation_only():
    """内核余额 → 行业报表口径；unmapped 如实列出；输入零改动。"""
    pack = load_mapping_pack(TEMPLATES / "restaurant-chain" / "account-mapping" / "pack.json")
    balances = [
        {"account_code": "4000", "currency": "CNY", "debit_total": "0",
         "credit_total": "1200.5", "balance": "-1200.5", "entry_count": 2},
        {"account_code": "5100", "currency": "CNY", "debit_total": "30.5",
         "credit_total": "0", "balance": "30.5", "entry_count": 1},
        {"account_code": "3000", "currency": "CNY", "debit_total": "0",
         "credit_total": "500", "balance": "-500", "entry_count": 1},  # 未映射
    ]
    snapshot = copy.deepcopy(balances)
    view = apply_mapping(balances, pack)
    assert balances == snapshot, "apply_mapping 必须只读"
    rows = {r["row_key"]: r for r in view["rows"]}
    assert rows["revenue"]["balance"] == pytest.approx(-1200.5)
    assert "R-0001" in rows["revenue"]["industry_accounts"]
    cost = rows["cost"]
    assert cost["balance"] == pytest.approx(30.5)
    assert set(cost["industry_accounts"]) == {"R-0003"}
    assert view["unmapped_balances"][0]["account_code"] == "3000"
    assert "零改动" in view["caliber_note"]


def test_replace_mapping_diff():
    old = load_mapping_pack(TEMPLATES / "restaurant-chain" / "account-mapping" / "pack.json")
    new = copy.deepcopy(old)
    new["version"] = "1.1.0"
    # 行业科目改码（同内核科目）：R-0006 → R-0009（都挂 1100）
    for acc in new["industry_accounts"]:
        if acc["code"] == "R-0006":
            acc["code"] = "R-0009"
    diff = replace_mapping(old, new)
    assert diff["new_version"] == "1.1.0"
    assert diff["retargeted"] == [{"code": "1100", "was": "R-0006", "now": "R-0009"}]
    assert diff["added"] == [] and diff["removed"] == []


def test_replace_mapping_does_not_touch_ledger(kernel, reset_db, seed_fx):
    """映射替换不改账（e2e）：落账 → v1 呈现 → 换 v2 → 分录逐字一致、呈现改变。"""
    from fd_erp_vertical_templates.mapping import load_mapping_pack as _load, apply_mapping as _apply
    seed_fx([{"quote_currency": "USD", "rate": "7.2", "as_of": "2026-11-06",
              "source": "ecb-exr"}])
    pack_v1 = _load(TEMPLATES / "restaurant-chain" / "account-mapping" / "pack.json")
    pack_v2 = copy.deepcopy(pack_v1)
    pack_v2["version"] = "1.1.0"
    for acc in pack_v2["industry_accounts"]:
        if acc["code"] == "R-0006":
            acc["code"] = "R-0009"
    kernel.submit_proposal(
        stable_id="recon-d1", tenant_id=reset_db.tenant_id,
        lines=[{"account_code": "4000", "direction": "credit", "amount": "300",
                "currency": "CNY"},
               {"account_code": "1300", "direction": "debit", "amount": "300",
                "currency": "CNY"}],
        source_ref="e2e:replace-mapping")
    kernel.confirm_proposal(stable_id="recon-d1", tenant_id=reset_db.tenant_id,
                            confirmed_by="owner")
    before_entries = kernel.query_entries(tenant_id=reset_db.tenant_id)["entries"]
    view1 = _apply(kernel.query_balance(tenant_id=reset_db.tenant_id)["data"], pack_v1)
    view2 = _apply(kernel.query_balance(tenant_id=reset_db.tenant_id)["data"], pack_v2)
    after_entries = kernel.query_entries(tenant_id=reset_db.tenant_id)["entries"]
    # 账不动：分录逐字一致
    assert json.dumps(before_entries, sort_keys=True, default=str) == \
        json.dumps(after_entries, sort_keys=True, default=str)
    # 口径变：5100 的余额（佣金）在 v1 计入 cost 行，v2 同样计入 cost 行但科目换挂
    assert view1["mapping_version"] == "1.0.0" and view2["mapping_version"] == "1.1.0"

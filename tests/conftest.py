"""测试夹具：真实 Postgres（DB 约束层不可 mock）+ 内核同进程客户端。

- ERP_LEDGER_DATABASE_URL 指向一次性测试库；migrate 一次，逐用例 TRUNCATE。
- InProcessKernelClient 与内核 tools.py 同形调用（模板运行时生产形态是
  MCP 工具面；测试侧允许 import 内核 —— boundary 审计只扫模板包本体）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# 内核是**兄弟仓**，本仓单独检出时不存在（CI 就是这样）。资产类门（绑定声明、
# 角色词表、面级对位、appliance 副本、清单形状）不需要内核也不需要 Postgres，
# 所以内核导入做成可选的：缺了就让需要它的 fixture skip，而不是整个 conftest 炸掉——
# 否则那几道门在 CI 里永远跑不起来（2026-11-08 首次上 CI 即撞）。
KERNEL_ROOT = Path(os.environ.get("ERP_LEDGER_KERNEL_ROOT")
                   or REPO_ROOT.parent / "fd-erp-ledger-mcp")

import sys
sys.path.insert(0, str(KERNEL_ROOT))

try:
    from fd_erp_ledger_mcp.config import Settings  # noqa: E402
    from fd_erp_ledger_mcp.db import migrate  # noqa: E402
    from fd_erp_ledger_mcp.universal_accounts import seed_universal_accounts  # noqa: E402
    from fd_erp_ledger_mcp.fx import seed_fx_facts  # noqa: E402
    from fd_erp_ledger_mcp.db import connection  # noqa: E402
    from fd_erp_ledger_mcp import ledger as _ledger  # noqa: E402
    from fd_erp_ledger_mcp import proposals as _proposals  # noqa: E402
    KERNEL_AVAILABLE = True
    KERNEL_IMPORT_ERROR: str | None = None
except ModuleNotFoundError as exc:  # 只有"缺内核"走这条；内核内部的 ImportError 照旧炸
    if not str(exc).startswith("No module named 'fd_erp_ledger_mcp"):
        raise
    KERNEL_AVAILABLE = False
    KERNEL_IMPORT_ERROR = f"{exc}（KERNEL_ROOT={KERNEL_ROOT}）"

from fd_erp_vertical_templates.errors import TemplateError  # noqa: E402

TENANT = "tpl-e2e"


@pytest.fixture(scope="session")
def db_settings():
    if not KERNEL_AVAILABLE:
        pytest.skip(f"内核仓不在位，跳过需要 Postgres 内核的用例：{KERNEL_IMPORT_ERROR}")
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if not dsn:
        pytest.skip("ERP_LEDGER_DATABASE_URL 未设置（需真实 Postgres）")
    s = Settings(database_url=dsn, tenant_id=TENANT, base_currency="CNY")
    migrate(s)
    return s


@pytest.fixture()
def reset_db(db_settings):
    conn = psycopg.connect(db_settings.database_url, autocommit=True)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('erp.allow_maintenance','on', true)")
                cur.execute(
                    "TRUNCATE proposals, journal_entries, fx_facts, recon_reports, "
                    "recon_task_items, audit, accounts, "
                    "fee_details, billing_rules, "
                    "credit_registry, credit_reservations RESTART IDENTITY CASCADE")
    finally:
        conn.close()
    return db_settings


class InProcessKernelClient:
    """内核 8 工具的同进程实现（签名同形；模板层只见 KernelClient 协议）。"""

    def __init__(self, settings: Settings, tenant_id: str = TENANT):
        self._s = settings
        self._tenant = tenant_id

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        reversal_of=None, lane_fleet=None, lane_post=None,
                        lane_title=None, lane_target=None, tenant_id=None,
                        submitted_by="agent"):
        with connection(self._s) as conn:
            return _proposals.submit_proposal(
                conn, settings=self._s, tenant_id=self._tenant,
                stable_id=stable_id, lines=lines, source_ref=source_ref,
                kind=kind, reversal_of=reversal_of, lane_fleet=lane_fleet,
                lane_post=lane_post, lane_title=lane_title,
                lane_target=lane_target, submitted_by=submitted_by)

    def list_proposals(self, status=None, tenant_id=None, offset=0, limit=100):
        with connection(self._s) as conn:
            rows = _proposals.list_proposals(conn, self._tenant, status)
            return {"proposals": rows[offset:offset + limit], "rows_returned": len(rows)}

    def confirm_proposal(self, proposal_id=None, stable_id=None, tenant_id=None,
                         confirmed_by="human"):
        with connection(self._s) as conn:
            return _proposals.confirm_proposal(
                conn, settings=self._s, tenant_id=self._tenant,
                proposal_id=proposal_id, stable_id=stable_id,
                confirmed_by=confirmed_by)

    def reject_proposal(self, proposal_id=None, stable_id=None, reason=None,
                        tenant_id=None, rejected_by="human"):
        with connection(self._s) as conn:
            return _proposals.reject_proposal(
                conn, tenant_id=self._tenant, proposal_id=proposal_id,
                stable_id=stable_id, reason=reason, rejected_by=rejected_by)

    def query_balance(self, account_code=None, tenant_id=None):
        with connection(self._s) as conn:
            data = _ledger.compute_balance(conn, self._tenant, account_code,
                                           settings=self._s)
            return {"data": data, "rows_returned": len(data)}

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      offset=0, limit=100):
        with connection(self._s) as conn:
            rows = _ledger.query_entries(conn, self._tenant,
                                         account_code=account_code, txn_id=txn_id)
            return {"entries": rows[offset:offset + limit], "rows_returned": len(rows)}

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        raise NotImplementedError("e2e 走模板三方驱动 + 账务工具；内核对账长任务"
                                  "由内核自身 e2e 覆盖")

    def get_recon_report(self, task_id, tenant_id=None):
        raise NotImplementedError("同上")

    # ---- 信用额度/计费扩展工具（add-supply-chain-vertical）----
    # InProcess fail-closed：内核侧工具由另一 change 落地。若内核模块已存在
    # 则委托同形函数；不存在则如实抛 NOT_WIRED（绝不伪造成功）。

    def _credit_mod(self):
        try:
            from fd_erp_ledger_mcp import credit  # 内核侧信用记账模块（credit.py）
        except ImportError as exc:
            raise TemplateError(
                "NOT_WIRED",
                "内核信用工具未上线（fail-closed，不伪造成功）："
                "fd_erp_ledger_mcp.credit 不存在（add-supply-chain-vertical "
                "内核侧任务交付后此处自动接线）") from exc
        return credit

    def _billing_mod(self):
        try:
            from fd_erp_ledger_mcp import billing  # 内核侧计费引擎模块
        except ImportError as exc:
            raise TemplateError(
                "NOT_WIRED",
                "内核计费工具未上线（fail-closed，不伪造成功）："
                "fd_erp_ledger_mcp.billing 不存在（add-supply-chain-vertical "
                "内核侧任务交付后此处自动接线）") from exc
        return billing

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None):
        mod = self._credit_mod()
        with connection(self._s) as conn:
            return mod.reserve_credit(
                conn, settings=self._s, tenant_id=self._tenant,
                customer_id=customer_id, stable_id=stable_id, amount=amount,
                currency=currency, source_ref=source_ref)

    def release_credit(self, customer_id, stable_id):
        mod = self._credit_mod()
        with connection(self._s) as conn:
            return mod.release_credit(conn, settings=self._s, tenant_id=self._tenant,
                                      customer_id=customer_id, stable_id=stable_id)

    def query_credit(self, customer_id):
        mod = self._credit_mod()
        with connection(self._s) as conn:
            # MCP 工具面 query_credit = credit.credit_position（同形语义）
            return mod.credit_position(conn, settings=self._s,
                                       tenant_id=self._tenant,
                                       customer_id=customer_id)

    def bill_event(self, event):
        mod = self._billing_mod()
        with connection(self._s) as conn:
            return mod.bill_event(conn, settings=self._s, tenant_id=self._tenant,
                                  event=event)

    def list_fees(self, offset=0, limit=100):
        mod = self._billing_mod()
        with connection(self._s) as conn:
            # MCP 工具面 list_fees = billing.list_fees + paginate（同形语义）
            rows = mod.list_fees(conn, self._tenant)
            return {"fees": rows[offset:offset + limit], "rows_returned": len(rows)}

    def get_fee_detail(self, fee_id):
        mod = self._billing_mod()
        with connection(self._s) as conn:
            return mod.get_fee_detail(conn, settings=self._s,
                                      tenant_id=self._tenant, fee_id=fee_id)

    def submit_billing_rule(self, config, source_ref=None):
        mod = self._billing_mod()
        with connection(self._s) as conn:
            return mod.submit_billing_rule(conn, settings=self._s,
                                           tenant_id=self._tenant, config=config,
                                           source_ref=source_ref)

    def approve_billing_rule(self, rule_id, version):
        mod = self._billing_mod()
        with connection(self._s) as conn:
            return mod.approve_billing_rule(conn, settings=self._s,
                                            tenant_id=self._tenant,
                                            rule_id=rule_id, version=version)

    def reverse_fee(self, fee_id, reason):
        mod = self._billing_mod()
        with connection(self._s) as conn:
            return mod.reverse_fee(conn, settings=self._s, tenant_id=self._tenant,
                                   fee_id=fee_id, reason=reason)

    def simulate_billing(self, config, events):
        mod = self._billing_mod()
        with connection(self._s) as conn:
            return mod.simulate_billing(conn, settings=self._s,
                                        tenant_id=self._tenant, config=config,
                                        events=events)


@pytest.fixture()
def kernel(db_settings):
    client = InProcessKernelClient(db_settings)
    with connection(db_settings) as conn:
        seed_universal_accounts(conn, TENANT)
    return client


@pytest.fixture()
def seed_fx(db_settings):
    def _seed(observations: list[dict[str, Any]]) -> dict[str, Any]:
        with connection(db_settings) as conn:
            return seed_fx_facts(conn, observations, settings=db_settings)
    return _seed

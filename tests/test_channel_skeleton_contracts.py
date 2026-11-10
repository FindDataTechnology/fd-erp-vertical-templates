"""三条通道骨架契约的机械审计（A 3.1 / 4.1 的可验证部分）。

3.1/4.1 要求「provider 动作面全为 read、本地 runtime 冒烟动作存在」。在锚点
客户确定前不能真实现 provider（ADR 0002 明令「不为想象的接口写 provider」），
所以这两条真正可交付的验证是：**契约里声明的动作面必须全是只读**，且矩阵对应
格必须如实停在「需一次接入」——不许因为写了契约就当成接通。

这条测试防的正是那种滑坡：先写契约、再顺手把矩阵改成 available。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONNECTOR_DOCS = REPO.parent / "open-connector-mt" / "docs"

#: (契约文件, 矩阵平台键)
CONTRACTS = (
    ("tiktok-shop-skeleton-contract.md", "tiktok_shop"),
    ("merchant-erp-skeleton-contract.md", "merchant_erp"),
    ("taobao-jd-skeleton-contract.md", "taobao"),
)

#: 只读动作白名单：契约里只允许出现这几种（list_/get_ 前缀且不带写语义）。
READ_ACTION = re.compile(r"`((?:list|get)_[a-z_]+)`")
#: 写语义关键词——契约里出现即为越界。
WRITE_VERBS = ("create", "update", "delete", "submit", "push", "adjust",
               "set_", "cancel", "refund_issue", "transfer")


def _contract_path(name: str) -> Path:
    return CONNECTOR_DOCS / name


@pytest.mark.parametrize("doc,platform", CONTRACTS, ids=[c[0] for c in CONTRACTS])
def test_contract_exists_and_declares_read_only_surface(doc: str, platform: str):
    path = _contract_path(doc)
    if not path.is_file():
        pytest.skip(f"{doc} 不在本机（单独检出本仓时正常）")
    text = path.read_text(encoding="utf-8")

    actions = set(READ_ACTION.findall(text))
    assert actions, f"{doc} 没有声明任何 list_/get_ 动作面"
    assert {"list_orders", "list_settlements", "list_inventory"} <= actions, (
        f"{doc} 三腿动作面不全：缺 {sorted({'list_orders','list_settlements','list_inventory'} - actions)}")

    # 只读纪律：任何写语义动作名都不得以动作面形式出现
    offenders = [a for a in READ_ACTION.findall(text)
                 if any(v in a for v in WRITE_VERBS)]
    assert not offenders, f"{doc} 声明了写动作：{offenders}"

    # 契约必须写明「实现挂起」与前置依赖，否则读者会以为已可用
    assert "实现挂起" in text, f"{doc} 未标明实现挂起"
    assert ("锚点客户" in text or "客户" in text), f"{doc} 未写前置依赖"


@pytest.mark.parametrize("doc,platform", CONTRACTS, ids=[c[0] for c in CONTRACTS])
def test_contract_does_not_upgrade_matrix(doc: str, platform: str):
    """写了契约 ≠ 接通：矩阵该平台三腿必须仍是 unmapped，且原因写清是骨架/未实现。"""
    from fd_erp_vertical_templates.capability import (
        STATUS_UNAVAILABLE,
        STATUS_UNMAPPED,
        load_matrix,
    )

    matrix = load_matrix(REPO / "templates" / "crossborder" / "capability-matrix.json")
    for flow in ("orders", "settlements", "inventory"):
        cell = matrix.cells[(platform, flow)]
        assert cell.status in (STATUS_UNMAPPED, STATUS_UNAVAILABLE), (
            f"{platform}.{flow} 在只有契约的情况下被标成 {cell.status}："
            "契约不是证据，升格需要真实读取记录")
        assert cell.reason.strip(), f"{platform}.{flow} 缺原因（对外无法解释）"
        assert ("骨架" in cell.reason or "未实现" in cell.reason
                or "schema" in cell.reason), (
            f"{platform}.{flow} 的原因没写清为何不可得：{cell.reason}")

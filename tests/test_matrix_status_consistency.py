"""两套证据链必须互相一致（能力矩阵 ↔ 连接器对接三态）。

仓里并行着两条链，粒度不同：

- `templates/crossborder/capability-matrix.json`：**平台 × 流**，回答「这条腿能不能读到」；
- `connector-status.json`：**绑定级**三态（catalog / executor_tests / live），
  回答「这个外部系统接通没有」。

两条链各自有门，但**它们之间没有门**。于是可能出现最坏的一种自相矛盾：
对接台账写着 connected，而能力矩阵该平台全是「需一次接入」——客户拿到的两份材料
互相打脸，且没人被拦下。

不变量（两个方向都要查）：
1. 某平台的 connector 在对接台账里是 `connected` → 该平台至少一格 `available`；
2. 某平台的所有格都不可得（无 `available`） → 该 connector 不得是 `connected`。

现在两条链都是「零接通」，所以这两条恒真——因此配一条**负例**证明机制真的会拦，
否则就是空转。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fd_erp_vertical_templates.capability import STATUS_AVAILABLE, load_matrix

REPO = Path(__file__).resolve().parent.parent
MATRIX = REPO / "templates" / "crossborder" / "capability-matrix.json"
STATUS_FILE = REPO / "connector-status.json"


def _status_by_provider() -> dict[str, str]:
    if not STATUS_FILE.is_file():
        pytest.skip("connector-status.json 不在本机（由 build_connector_status.py 派生）")
    body = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for row in body.get("integrations") or []:
        provider = row.get("provider")
        if provider:
            out.setdefault(provider, row.get("state") or "")
    return out


def _check(matrix, statuses: dict[str, str]) -> list[str]:
    """返回冲突清单（空 = 一致）。抽成函数是为了能用假数据做负例。"""
    problems: list[str] = []
    for platform in matrix.platform_keys():
        connector = matrix.platform_meta(platform).get("connector")
        if not connector:
            continue
        available = [f for f in ("orders", "settlements", "inventory")
                     if (platform, f) in matrix.cells
                     and matrix.cells[(platform, f)].status == STATUS_AVAILABLE]
        state = statuses.get(connector)
        if state == "connected" and not available:
            problems.append(
                f"{platform}: 对接台账称 {connector} 已接通，但矩阵三腿全不可得——"
                "两份材料矛盾，先改台账或补真实读取证据")
        if state is not None and state != "connected" and available:
            problems.append(
                f"{platform}: 矩阵有 {len(available)} 腿可得，而对接台账称 {connector} "
                f"未接通（{state}）——升格缺证据或台账滞后")
    return problems


def test_matrix_and_connector_status_agree():
    matrix = load_matrix(MATRIX)
    problems = _check(matrix, _status_by_provider())
    assert not problems, "能力矩阵与连接器对接台账互相矛盾：\n  " + "\n  ".join(problems)


def test_mechanism_catches_connected_but_matrix_empty():
    """负例 1：台账说接通、矩阵全不可得 → 必须拦（当前真实状态是「都没接通」）。"""
    matrix = load_matrix(MATRIX)
    fake = {"shopify_admin": "connected",
            "tiktok_shop": "not_connected", "taobao": "not_connected",
            "jd": "not_connected", "merchant_erp": "not_connected"}
    problems = _check(matrix, fake)
    assert any("shopify" in p and "矛盾" in p for p in problems), problems


def test_mechanism_catches_available_but_status_says_not_connected():
    """负例 2：矩阵有可用腿、台账说没接通 → 也必须拦。"""
    from datetime import date

    body = {
        "schema": "ecommerce-capability-matrix/v1",
        "vertical": "crossborder",
        "platforms": [{"key": "shopify", "connector": "shopify_admin", "flows": {
            "orders": {"status": "available", "channel": "a", "reason": "",
                       "measured_at": "2026-11-09",
                       "evidence": [{"kind": "provider_action", "ref": "a"},
                                    {"kind": "runtime_read", "ref": "b"}]}}}],
    }
    matrix = load_matrix(body, today=date(2026, 11, 10))
    problems = _check(matrix, {"shopify_admin": "not_connected"})
    assert any("台账滞后" in p or "未接通" in p for p in problems), problems


def test_platforms_without_connector_are_skipped():
    """拼多多没有 connector（不立 provider）——不该因为它产生冲突。"""
    matrix = load_matrix(MATRIX)
    assert matrix.platform_meta("pinduoduo").get("connector") is None
    assert not any("pinduoduo" in p for p in _check(matrix, {}))

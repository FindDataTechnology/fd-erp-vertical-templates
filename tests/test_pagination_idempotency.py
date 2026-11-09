"""翻页与幂等（A 任务 2.4）。

单页读取不会报错，它只是少给数据——所以断言必须是「两页都在」而不是「没抛异常」。
幂等断言：同一窗口拉两次，stable_id 集合完全一致（重推是常态）。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_templates.errors import TemplateError
from fd_erp_vertical_templates.runtime_client import ActionRef, RuntimeClient

REF = ActionRef("shopify_admin", "list_orders", "c1")


def _paged(pages: list[list[dict]]):
    """按页返回，模拟 pageInfo.hasNextPage / endCursor。"""
    calls: list[dict] = []

    def transport(method, url, headers, body):
        calls.append(body)
        idx = len(calls) - 1
        has_next = idx < len(pages) - 1
        return 200, {
            "success": True, "message": "OK",
            "data": {
                "orders": pages[idx],
                "pageInfo": {"hasNextPage": has_next,
                             "hasPreviousPage": idx > 0,
                             "startCursor": "s",
                             "endCursor": f"c{idx}"},
            },
            "meta": {},
        }

    return RuntimeClient("http://runtime.test", transport=transport), calls


def test_paginates_until_has_next_page_is_false():
    client, calls = _paged([[{"id": "a", "stable_id": "s-a"}],
                            [{"id": "b", "stable_id": "s-b"}],
                            [{"id": "c", "stable_id": "s-c"}]])
    out = client.call_all(REF, {"since": "2026-11-01"})
    assert [r["id"] for r in out["records"]] == ["a", "b", "c"]
    assert out["pages"] == 3
    # 第二页起带上一页的 endCursor
    assert "after" not in calls[0]
    assert calls[1]["after"] == "c0"
    assert calls[2]["after"] == "c1"


def test_repeated_window_is_idempotent():
    pages = [[{"id": "a", "stable_id": "s-a"}], [{"id": "b", "stable_id": "s-b"}]]
    first, _ = _paged(pages)
    second, _ = _paged(pages)
    a = first.call_all(REF, {"since": "2026-11-01"})["records"]
    b = second.call_all(REF, {"since": "2026-11-01"})["records"]
    assert [r["stable_id"] for r in a] == [r["stable_id"] for r in b]


def test_duplicate_records_within_a_read_are_dropped():
    """平台重推同一笔：一次读取里出现两次也只留一条。"""
    client, _ = _paged([[{"id": "a", "stable_id": "s-a"},
                         {"id": "a", "stable_id": "s-a"}]])
    out = client.call_all(REF, {})
    assert len(out["records"]) == 1


def test_claims_next_page_without_cursor_is_an_error():
    """声称还有下一页却没有 endCursor：按未读完报错，不能当读完。"""
    def transport(method, url, headers, body):
        return 200, {"data": {"orders": [{"id": "a"}],
                              "pageInfo": {"hasNextPage": True,
                                           "hasPreviousPage": False,
                                           "startCursor": "s",
                                           "endCursor": None}}}

    client = RuntimeClient("http://runtime.test", transport=transport)
    with pytest.raises(TemplateError) as ei:
        client.call_all(REF, {})
    assert ei.value.code == "BAD_RESPONSE"
    assert "不能当作读完" in ei.value.message


def test_page_limit_is_an_error_not_a_partial_result():
    def transport(method, url, headers, body):
        return 200, {"data": {"orders": [{"id": "x"}],
                              "pageInfo": {"hasNextPage": True,
                                           "hasPreviousPage": True,
                                           "startCursor": "s",
                                           "endCursor": "c"}}}

    client = RuntimeClient("http://runtime.test", transport=transport)
    with pytest.raises(TemplateError) as ei:
        client.call_all(REF, {}, max_pages=3)
    assert "不返回部分结果" in ei.value.message
    assert ei.value.detail["pages"] == 3


def test_shopify_adapter_uses_pagination():
    """适配器层必须真的走翻页：第二页的记录也要出现在结果里。"""
    from fd_erp_vertical_templates.connectors import shopify

    def transport(method, url, headers, body):
        idx = body.get("after")
        if idx is None:
            return 200, {"data": {"orders": [{"id": "o1", "amount": "10"}],
                                  "pageInfo": {"hasNextPage": True,
                                               "hasPreviousPage": False,
                                               "startCursor": "s", "endCursor": "c1"}}}
        return 200, {"data": {"orders": [{"id": "o2", "amount": "20"}],
                              "pageInfo": {"hasNextPage": False,
                                           "hasPreviousPage": True,
                                           "startCursor": "c1", "endCursor": None}}}

    reader = shopify.ShopifyStoreReader(
        "s", client=RuntimeClient("http://runtime.test", transport=transport))
    rows = reader.read_orders()
    assert [r["raw"]["id"] for r in rows] == ["o1", "o2"]

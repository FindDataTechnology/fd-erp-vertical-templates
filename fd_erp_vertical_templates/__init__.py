"""ERP 行业模板层（add-erp-vertical-templates）。

边界铁律（本包全部代码受其约束，boundary.py 机械化审计）：
1. 零旁路账本：模板层不存任何账务事实（余额/分录/科目余额表），一切账务经
   fd-erp-ledger-mcp 内核 8 工具（submit_proposal / confirm_proposal / ...）。
2. 零直连库表：本包不 import 任何 DB 驱动（psycopg/pymongo/sqlite3 等），
   不出现内核表名（journal_entries/proposals/accounts...）的 SQL。
3. 回写仅经内核白名单车道：对账核对结论只能以 proposal（待确认）形态提交
   （lane_* 车道语义字段），落账必须经确认门；禁写资金类分录。
4. 对外部系统零写入：连接器只有 read 面。

四件套：连接器 pack + 舰队蓝图 + 科目映射 pack + 报表 preset（manifest.py）。
科目映射：呈现层重算，替换映射不改账（mapping.py）。
"""
from .manifest import TEMPLATE_SCHEMA, load_template_manifest
from .mapping import load_mapping_pack, apply_mapping, replace_mapping

__all__ = [
    "TEMPLATE_SCHEMA",
    "load_template_manifest",
    "load_mapping_pack",
    "apply_mapping",
    "replace_mapping",
]

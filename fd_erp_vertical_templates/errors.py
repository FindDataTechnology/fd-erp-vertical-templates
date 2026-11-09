"""模板层错误语义（封闭集合，与内核错误码语义兼容）。

模板层新增码只描述模板自身的问题（行业映射/主数据/适配器范围），
账务语义错误一律透传内核封闭集合。
"""
from __future__ import annotations


class TemplateError(Exception):
    """模板层错误：code 取自下方封闭集合 + message + detail。"""

    CODES = (
        "INVALID_ARGUMENT",      # 参数非法（与内核同名同义）
        "NOT_FOUND",             # 对象不存在
        "NOT_WIRED",             # 未接线/未适配（如实报出，不伪造能力）
        "MAPPING_INVALID",       # 科目映射 pack 非法（目标不在内核通用科目集等）
        "MANIFEST_INVALID",      # 模板四件套清单非法
        "MATRIX_INVALID",        # 能力矩阵非法（schema/状态/证据形状/缺格）
        "RUNTIME_UNREACHABLE",   # 连接器运行时不可达（不得回退直连）
        "ACTION_NOT_FOUND",      # 动作不在目录（目录缺口，不得自建通道）
        "AUTH_FAILED",           # 凭据无效或权限不足（去连接器重授权）
        "RATE_LIMITED",          # 上游配额（可退避重试，归因「配额」非「无数据」）
        "BAD_RESPONSE",          # 动作返回结构不符契约（契约漂移）
        "BOUNDARY_VIOLATION",    # 模板边界审计发现违规
        "MASTER_DATA_STATE",     # 主数据状态非法（如已停业门店再出日结）
        "CREDIT_NOT_REGISTERED",  # 客户未登记信用额度（下单前置运维动作缺失）
        "WORKFLOW_STATE",        # 编排工作流状态非法（如未确认的采购单再推送）
        "SHARE_EXECUTION_DENIED",  # 试图输出资金执行面（红线，模板层直接拒绝）
    )

    def __init__(self, code: str, message: str, detail: dict | None = None):
        if code not in self.CODES:
            raise ValueError(f"TemplateError code 不在封闭集合: {code}")
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}

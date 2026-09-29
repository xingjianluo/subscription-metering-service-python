"""错误码与领域异常：每类拒绝/冲突都有独立 code，便于调用方区分原因。"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # 上报类
    MISSING_FIELD = "missing_field"
    INVALID_FIELD = "invalid_field"
    NEGATIVE_QUANTITY = "usage_negative_quantity"
    USAGE_OUT_OF_RANGE = "usage_out_of_subscription_range"
    # 主数据 / 状态类
    UNKNOWN_PLAN = "unknown_plan"
    UNKNOWN_SUBSCRIPTION = "unknown_subscription"
    UNKNOWN_INVOICE = "unknown_invoice"
    PLAN_CHANGE_OUT_OF_RANGE = "plan_change_out_of_range"
    CANCEL_OUT_OF_RANGE = "cancel_out_of_range"
    PERIOD_NOT_CLOSED = "period_not_closed"
    INVOICE_ALREADY_SETTLED = "invoice_already_settled"
    INVOICE_SETTLE_FAILED = "invoice_settle_failed"
    INVALID_STATE = "invalid_state"
    # 幂等冲突
    EVENT_CONFLICT = "event_id_conflict"


class MeteringError(Exception):
    """业务错误：code 为稳定机器码，message 面向人，details 携带判定上下文。"""

    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{code.value}] {message}")

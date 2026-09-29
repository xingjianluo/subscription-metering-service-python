"""领域错误与可区分的拒绝原因。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """所有可对外区分的失败原因。"""

    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    MISSING_FIELD = "missing_field"
    NEGATIVE_AMOUNT = "negative_amount"
    PLAN_NOT_FOUND = "plan_not_found"
    SUBSCRIPTION_NOT_FOUND = "subscription_not_found"
    SUBSCRIPTION_INACTIVE = "subscription_inactive"
    USAGE_OUTSIDE_PERIOD = "usage_outside_period"
    USAGE_BEFORE_SUBSCRIPTION = "usage_before_subscription"
    USAGE_AFTER_CANCELLATION = "usage_after_cancellation"
    USAGE_AFTER_PERIOD_END = "usage_after_period_end"
    NEGATIVE_QUANTITY = "negative_quantity"
    CONFLICT = "conflict"
    EVENT_PROCESSED_CONFLICT = "event_processed_conflict"
    INVOICE_NOT_FOUND = "invoice_not_found"
    INVOICE_ALREADY_SETTLED = "invoice_already_settled"
    PAYMENT_DECLINED = "payment_declined"
    ALREADY_CANCELLED = "already_cancelled"
    INVALID_PERIOD = "invalid_period"
    PLAN_CHANGE_SAME_PLAN = "plan_change_same_plan"
    CHANGE_TIME_INVALID = "change_time_invalid"
    INVOICE_PERIOD_NOT_CLOSED = "invoice_period_not_closed"


class DomainError(Exception):
    """业务错误：携带稳定错误码与可读原因。"""

    def __init__(self, code: ErrorCode, message: str, *, details: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{code.value}] {message}")

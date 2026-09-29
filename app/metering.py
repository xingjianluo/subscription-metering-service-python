"""用量计量：周期窗口聚合、乱序归位、跨周期隔离。"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.errors import DomainError, ErrorCode
from app.models import Subscription, SubscriptionStatus, UsageRecord, require_field
from app.periods import period_for, period_index_for


@dataclass
class WindowUsage:
    subscription_id: str
    period_index: int
    period_start: datetime
    period_end: datetime
    total_quantity: Decimal
    record_count: int
    records: list[UsageRecord]


def validate_usage(
    record: UsageRecord,
    subscription: Subscription,
    anchored_at: datetime,
) -> int:
    """校验用量上报，返回所属账期序号；不合法抛 DomainError。"""
    require_field(record.subscription_id, "subscription_id")
    require_field(record.metric, "metric")
    if record.occurred_at is None:
        raise DomainError(ErrorCode.MISSING_FIELD, "缺少关键字段: occurred_at",
                          details={"field": "occurred_at"})
    if record.quantity is None:
        raise DomainError(ErrorCode.MISSING_FIELD, "缺少关键字段: quantity",
                          details={"field": "quantity"})
    quantity = Decimal(record.quantity)
    if quantity < 0:
        raise DomainError(
            ErrorCode.NEGATIVE_QUANTITY,
            "用量额度不能为负",
            details={"field": "quantity", "value": str(quantity)},
        )

    when = record.occurred_at
    if when < anchored_at:
        raise DomainError(
            ErrorCode.USAGE_BEFORE_SUBSCRIPTION,
            "用量时间早于订阅起始时间",
            details={"occurred_at": when.isoformat(),
                     "anchored_at": anchored_at.isoformat()},
        )

    if (
        subscription.status == SubscriptionStatus.CANCELLED
        and subscription.cancelled_at is not None
        and when > subscription.cancelled_at
    ):
        raise DomainError(
            ErrorCode.USAGE_AFTER_CANCELLATION,
            "用量时间晚于订阅取消时间",
            details={"occurred_at": when.isoformat(),
                     "cancelled_at": subscription.cancelled_at.isoformat()},
        )

    index = period_index_for(anchored_at, when)
    if index < 0:  # 理论上被前面的早于订阅拦截，双保险
        raise DomainError(
            ErrorCode.USAGE_OUTSIDE_PERIOD,
            "用量不属于任何有效账期",
            details={"occurred_at": when.isoformat()},
        )
    return index


def insert_usage_ordered(records: list[UsageRecord], record: UsageRecord) -> list[UsageRecord]:
    """按 occurred_at 将（可能乱序到达的）用量插入正确位置。

    同一时刻的多条记录都保留——幂等去重在事件台账层完成，
    不同事件即便数量相同也是两次独立使用，应相加。
    """
    keys = [r.occurred_at for r in records]
    pos = bisect_left(keys, record.occurred_at)
    records.insert(pos, record)
    return records


def aggregate_window(
    subscription_id: str,
    period_index: int,
    anchored_at: datetime,
    records: list[UsageRecord],
) -> WindowUsage:
    """聚合单个时间窗口内的用量（记录已按时间排序）。"""
    start, end = period_for(anchored_at, period_index)
    in_window = [r for r in records if start <= r.occurred_at < end]
    total = sum((Decimal(r.quantity) for r in in_window), Decimal("0"))
    return WindowUsage(
        subscription_id=subscription_id,
        period_index=period_index,
        period_start=start,
        period_end=end,
        total_quantity=total,
        record_count=len(in_window),
        records=in_window,
    )

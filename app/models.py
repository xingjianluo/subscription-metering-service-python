"""领域模型：只放类型定义，不含业务逻辑。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class EventKind(str, Enum):
    PLAN_DEFINED = "plan_defined"
    SUBSCRIPTION_CREATED = "subscription_created"
    USAGE_REPORTED = "usage_reported"
    PLAN_CHANGED = "plan_changed"
    SUBSCRIPTION_CANCELED = "subscription_canceled"
    INVOICE_GENERATED = "invoice_generated"
    INVOICE_SETTLED = "invoice_settled"


class EventStatus(str, Enum):
    PENDING = "pending"   # 已入账事件流，效果可能尚未提交（中断恢复点）
    APPLIED = "applied"   # 效果已提交，重放时直接跳过
    REJECTED = "rejected"  # 校验拒绝（拒绝原因保存在 result.error 中），重放时跳过


class InvoiceStatus(str, Enum):
    PENDING = "pending"
    SETTLED = "settled"


class LineType(str, Enum):
    BASE = "base"        # 套餐基础费（可能按比例）
    OVERAGE = "overage"  # 超量费
    CREDIT = "credit"    # 抵扣（如中途取消退还）


@dataclass(frozen=True)
class Plan:
    plan_id: str
    name: str
    base_price: Decimal            # 每周期基础费
    included_quantity: Decimal     # 每周期内含额度
    overage_unit_price: Decimal    # 超量单价
    currency: str = "CNY"


@dataclass(frozen=True)
class PlanPhase:
    """套餐时间线上的一段：从 effective_from 起生效（左闭）。"""
    plan_id: str
    effective_from: datetime


@dataclass
class Subscription:
    subscription_id: str
    customer_id: str
    start_at: datetime
    phases: list[PlanPhase] = field(default_factory=list)
    canceled_at: datetime | None = None  # 取消生效时刻（该时刻起不再计费用量）


@dataclass(frozen=True)
class UsageRecord:
    event_id: str
    subscription_id: str
    ts: datetime
    quantity: Decimal


@dataclass
class EventRecord:
    event_id: str
    kind: EventKind
    payload: dict
    payload_hash: str
    seq: int
    status: EventStatus = EventStatus.PENDING
    result: dict | None = None
    received_at: datetime | None = None
    applied_at: datetime | None = None


@dataclass(frozen=True)
class TimelineSegment:
    """账期内按套餐变更/取消切出的计费小段，区间左闭右开。"""
    plan_id: str
    start: datetime
    end: datetime
    days: Decimal
    total_days: Decimal


@dataclass(frozen=True)
class InvoiceLine:
    line_type: LineType
    plan_id: str | None
    description: str
    amount: Decimal
    basis: dict  # 判定依据：比例、用量、额度等


@dataclass
class Invoice:
    invoice_id: str
    subscription_id: str
    period_index: int
    period_start: datetime
    period_end: datetime
    currency: str
    lines: list[InvoiceLine] = field(default_factory=list)
    total_amount: Decimal = Decimal("0")
    status: InvoiceStatus = InvoiceStatus.PENDING
    settlement_request_id: str | None = None
    settled_at: datetime | None = None


@dataclass(frozen=True)
class LedgerEntry:
    """结算台账：同一 (invoice_id, request_id) 最多一笔成功扣款。"""
    invoice_id: str
    request_id: str
    amount: Decimal
    success: bool
    at: datetime
    recovered: bool = False  # 重放重建（未真实调用支付网关）

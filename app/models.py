"""领域模型：套餐、订阅、用量、账单项。

金额一律使用 Decimal，单位为元；出账时在 billing 层统一四舍五入到分。
"""

from __future__ import annotations

import hashlib
import json as _json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from app.errors import DomainError, ErrorCode


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------- 套餐


@dataclass
class Plan:
    id: str
    name: str
    base_price: Decimal
    included_quantity: Decimal
    overage_unit_price: Decimal
    currency: str = "CNY"

    @classmethod
    def create(
        cls,
        name: str,
        base_price: Decimal,
        included_quantity: Decimal,
        overage_unit_price: Decimal,
        currency: str = "CNY",
        plan_id: str | None = None,
    ) -> "Plan":
        if not name or not str(name).strip():
            raise DomainError(ErrorCode.MISSING_FIELD, "套餐名称不能为空",
                              details={"field": "name"})
        require_non_negative(base_price, "base_price")
        require_non_negative(included_quantity, "included_quantity")
        require_non_negative(overage_unit_price, "overage_unit_price")
        return cls(
            id=plan_id or _new_id("plan"),
            name=name.strip(),
            base_price=Decimal(base_price),
            included_quantity=Decimal(included_quantity),
            overage_unit_price=Decimal(overage_unit_price),
            currency=currency,
        )


# ---------------------------------------------------------------- 订阅


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


@dataclass
class PlanAssignment:
    """套餐归属片段：[effective_from, effective_to)。"""

    plan_id: str
    effective_from: datetime
    effective_to: datetime | None  # None 表示开放区间（延续至今）


@dataclass
class Subscription:
    id: str
    plan_id: str
    status: SubscriptionStatus
    anchored_at: datetime
    cancelled_at: datetime | None
    plan_history: list[PlanAssignment] = field(default_factory=list)

    @classmethod
    def create(cls, plan_id: str, anchored_at: datetime) -> "Subscription":
        require_field(plan_id, "plan_id")
        return cls(
            id=_new_id("sub"),
            plan_id=plan_id,
            status=SubscriptionStatus.ACTIVE,
            anchored_at=anchored_at,
            cancelled_at=None,
            plan_history=[PlanAssignment(plan_id=plan_id,
                                         effective_from=anchored_at,
                                         effective_to=None)],
        )

    def open_assignment(self) -> PlanAssignment:
        return self.plan_history[-1]


# ---------------------------------------------------------------- 用量


@dataclass
class UsageRecord:
    subscription_id: str
    quantity: Decimal
    occurred_at: datetime
    metric: str


# ---------------------------------------------------------------- 账单


class LineItemKind(str, Enum):
    BASE = "base"
    PRORATED_CREDIT = "prorated_credit"
    PRORATED_CHARGE = "prorated_charge"
    OVERAGE = "overage"


@dataclass
class LineItem:
    kind: LineItemKind
    plan_id: str
    description: str
    quantity: Decimal           # 计费量（天数或用量单位）
    unit_price: Decimal
    amount: Decimal             # 有符号金额，抵扣为负
    period_start: datetime
    period_end: datetime

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "plan_id": self.plan_id,
            "description": self.description,
            "quantity": str(self.quantity),
            "unit_price": str(self.unit_price),
            "amount": str(self.amount),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
        }


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    SETTLED = "settled"
    FAILED = "failed"


@dataclass
class Invoice:
    id: str
    subscription_id: str
    period_start: datetime
    period_end: datetime
    line_items: list[LineItem] = field(default_factory=list)
    status: InvoiceStatus = InvoiceStatus.DRAFT
    total_amount: Decimal = Decimal("0")
    currency: str = "CNY"
    created_at: datetime | None = None
    settled_at: datetime | None = None
    payment_txn_id: str | None = None
    content_checksum: str = ""

    def recompute_total(self) -> Decimal:
        total = sum((item.amount for item in self.line_items), Decimal("0"))
        # 金额四舍五入到分（2 位小数）
        self.total_amount = total.quantize(Decimal("0.01"))
        return self.total_amount

    def compute_checksum(self) -> str:
        body = {
            "subscription_id": self.subscription_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "currency": self.currency,
            "items": [item.to_dict() for item in self.line_items],
            "total": str(self.recompute_total()),
        }
        raw = _json.dumps(body, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 事件台账


class EventType(str, Enum):
    PLAN_CREATED = "plan_created"
    SUBSCRIPTION_CREATED = "subscription_created"
    PLAN_CHANGED = "plan_changed"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"
    USAGE_REPORTED = "usage_reported"
    INVOICE_GENERATED = "invoice_generated"
    INVOICE_SETTLED = "invoice_settled"


class EventOutcome(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass
class LedgerEntry:
    event_id: str
    event_type: EventType
    payload_hash: str
    outcome: EventOutcome
    result: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    recorded_at: datetime | None = None


# ---------------------------------------------------------------- 校验


def require_field(value, field_name: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(ErrorCode.MISSING_FIELD, f"缺少关键字段: {field_name}",
                          details={"field": field_name})


def require_non_negative(value: Decimal, field_name: str) -> None:
    if value is None:
        raise DomainError(ErrorCode.MISSING_FIELD, f"缺少关键字段: {field_name}",
                          details={"field": field_name})
    if Decimal(value) < 0:
        raise DomainError(ErrorCode.NEGATIVE_AMOUNT, f"字段不能为负: {field_name}",
                          details={"field": field_name, "value": str(value)})

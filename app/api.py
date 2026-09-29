"""FastAPI 路由：把 HTTP 请求转为带 event_id 的命令。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------- 请求模型


class PlanIn(BaseModel):
    event_id: str
    name: str
    base_price: Decimal
    included_quantity: Decimal
    overage_unit_price: Decimal
    currency: str = "CNY"


class SubscriptionIn(BaseModel):
    event_id: str
    plan_id: str


class PlanChangeIn(BaseModel):
    event_id: str
    new_plan_id: str
    effective_at: datetime | None = None


class CancelIn(BaseModel):
    event_id: str
    cancelled_at: datetime | None = None


class UsageIn(BaseModel):
    event_id: str
    subscription_id: str
    quantity: Decimal
    occurred_at: datetime
    metric: str


class InvoiceIn(BaseModel):
    event_id: str
    subscription_id: str
    period_index: int


class SettleIn(BaseModel):
    event_id: str
    subscription_id: str
    period_index: int
    decline_once: bool = False


def _svc(request: Request):
    return request.app.state.service


def _respond(result) -> JSONResponse:
    # 409 表示幂等/冲突类语义；这里统一 200 + outcome，由调用方区分
    return JSONResponse(status_code=200, content=result.to_dict())


@router.post("/plans")
def create_plan(body: PlanIn, request: Request):
    return _respond(_svc(request).create_plan(
        body.event_id, body.name, body.base_price,
        body.included_quantity, body.overage_unit_price, body.currency))


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, request: Request):
    plan = _svc(request).get_plan(plan_id)
    return {"id": plan.id, "name": plan.name, "base_price": str(plan.base_price),
            "included_quantity": str(plan.included_quantity),
            "overage_unit_price": str(plan.overage_unit_price),
            "currency": plan.currency}


@router.post("/subscriptions")
def create_subscription(body: SubscriptionIn, request: Request):
    return _respond(_svc(request).create_subscription(body.event_id, body.plan_id))


@router.get("/subscriptions/{subscription_id}")
def get_subscription(subscription_id: str, request: Request):
    from app.services import _subscription_to_dict
    return _subscription_to_dict(_svc(request).get_subscription(subscription_id))


@router.post("/subscriptions/{subscription_id}/change-plan")
def change_plan(subscription_id: str, body: PlanChangeIn, request: Request):
    return _respond(_svc(request).change_plan(
        body.event_id, subscription_id, body.new_plan_id, body.effective_at))


@router.post("/subscriptions/{subscription_id}/cancel")
def cancel(subscription_id: str, body: CancelIn, request: Request):
    return _respond(_svc(request).cancel_subscription(
        body.event_id, subscription_id, body.cancelled_at))


@router.post("/usage")
def report_usage(body: UsageIn, request: Request):
    return _respond(_svc(request).report_usage(
        body.event_id, body.subscription_id, body.quantity,
        body.occurred_at, body.metric))


@router.get("/subscriptions/{subscription_id}/usage/{period_index}")
def get_window(subscription_id: str, period_index: int, request: Request):
    return _svc(request).get_window(subscription_id, period_index)


@router.post("/invoices")
def generate_invoice(body: InvoiceIn, request: Request):
    return _respond(_svc(request).generate_invoice(
        body.event_id, body.subscription_id, body.period_index))


@router.get("/subscriptions/{subscription_id}/invoices/{period_index}")
def get_invoice(subscription_id: str, period_index: int, request: Request):
    return _svc(request).get_invoice(subscription_id, period_index)


@router.post("/settlements")
def settle(body: SettleIn, request: Request):
    return _respond(_svc(request).settle(
        body.event_id, body.subscription_id, body.period_index,
        decline_once=body.decline_once))


@router.post("/replay")
def replay(request: Request):
    return _svc(request).replay_all()

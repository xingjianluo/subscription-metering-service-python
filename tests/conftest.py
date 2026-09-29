import logging

import pytest

from app.billing import Wallet
from app.service import MeteringService
from app.store import Store

logging.basicConfig(level=logging.INFO)

BASIC_PLAN = {
    "event_id": "ev-plan-basic",
    "plan_id": "basic",
    "name": "基础版",
    "base_price": "100.00",
    "included_quantity": "1000",
    "overage_unit_price": "0.10",
}
PRO_PLAN = {
    "event_id": "ev-plan-pro",
    "plan_id": "pro",
    "name": "专业版",
    "base_price": "300.00",
    "included_quantity": "5000",
    "overage_unit_price": "0.05",
}
START = "2026-01-01T00:00:00Z"
AFTER_P0 = "2026-02-02T00:00:00Z"


@pytest.fixture
def service():
    store = Store()
    wallet = Wallet("100000")
    svc = MeteringService(store, wallet)
    svc.define_plan(BASIC_PLAN)
    svc.define_plan(PRO_PLAN)
    svc.create_subscription(
        {
            "event_id": "ev-sub",
            "subscription_id": "s1",
            "customer_id": "c1",
            "plan_id": "basic",
            "start_at": START,
        }
    )
    return svc

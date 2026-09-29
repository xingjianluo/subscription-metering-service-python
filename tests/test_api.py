"""HTTP 层：路由装配、错误码映射、幂等响应。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.clock import FixedClock
from app.services import MeteringService
from app.storage import InMemoryStore
from main import create_app


@pytest.fixture
def client():
    svc = MeteringService(store=InMemoryStore(),
                          clock=FixedClock(datetime(2026, 1, 1, 9)))
    return TestClient(create_app(svc))


def _plan_and_sub(client):
    r = client.post("/api/v1/plans", json={
        "event_id": "p1", "name": "基础版", "base_price": "100",
        "included_quantity": "1000", "overage_unit_price": "0.1"})
    pid = r.json()["data"]["plan"]["id"]
    r = client.post("/api/v1/subscriptions", json={"event_id": "s1", "plan_id": pid})
    return pid, r.json()["data"]["subscription"]["id"]


def test_root(client):
    assert client.get("/").json()["status"] == "ok"


def test_usage_negative_returns_422_with_distinct_code(client):
    _, sid = _plan_and_sub(client)
    r = client.post("/api/v1/usage", json={
        "event_id": "neg", "subscription_id": sid, "quantity": "-5",
        "occurred_at": "2026-01-10T09:00:00", "metric": "m"})
    assert r.status_code == 422
    assert r.json()["error"] == "negative_quantity"


def test_missing_field_returns_missing_field_code(client):
    _, sid = _plan_and_sub(client)
    r = client.post("/api/v1/usage", json={
        "event_id": "miss", "subscription_id": sid, "quantity": "1",
        "occurred_at": "2026-01-10T09:00:00", "metric": ""})
    assert r.status_code == 422
    assert r.json()["error"] == "missing_field"
    assert r.json()["details"]["field"] == "metric"


def test_duplicate_usage_event_outcome(client):
    _, sid = _plan_and_sub(client)
    body = {"event_id": "u1", "subscription_id": sid, "quantity": "100",
            "occurred_at": "2026-01-10T09:00:00", "metric": "m"}
    assert client.post("/api/v1/usage", json=body).json()["outcome"] == "applied"
    r2 = client.post("/api/v1/usage", json=body)
    assert r2.json()["outcome"] == "duplicate"


def test_conflict_event_returns_422_conflict(client):
    _, sid = _plan_and_sub(client)
    client.post("/api/v1/usage", json={
        "event_id": "u1", "subscription_id": sid, "quantity": "100",
        "occurred_at": "2026-01-10T09:00:00", "metric": "m"})
    r = client.post("/api/v1/usage", json={
        "event_id": "u1", "subscription_id": sid, "quantity": "200",
        "occurred_at": "2026-01-11T09:00:00", "metric": "m"})
    assert r.status_code == 422
    assert r.json()["error"] == "event_processed_conflict"


def test_invoice_and_settle_flow_over_http(client):
    _, sid = _plan_and_sub(client)
    client.post("/api/v1/usage", json={
        "event_id": "u1", "subscription_id": sid, "quantity": "1200",
        "occurred_at": "2026-01-10T09:00:00", "metric": "m"})
    inv = client.post("/api/v1/invoices", json={
        "event_id": "i1", "subscription_id": sid, "period_index": 0})
    assert inv.json()["data"]["invoice"]["total_amount"] == "120.00"
    settled = client.post("/api/v1/settlements", json={
        "event_id": "pay1", "subscription_id": sid, "period_index": 0})
    assert settled.json()["data"]["invoice"]["status"] == "settled"

    # 新事件对已结算账单再次结算 -> 422
    again = client.post("/api/v1/settlements", json={
        "event_id": "pay2", "subscription_id": sid, "period_index": 0})
    assert again.status_code == 422
    assert again.json()["error"] == "invoice_already_settled"


def test_replay_endpoint(client):
    r = client.post("/api/v1/replay")
    assert r.status_code == 200
    assert r.json()["consistent"] is True

"""HTTP 冒烟：状态码、错误码区分、端到端链路。"""
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api import create_app
from app.billing import Wallet
from app.service import MeteringService
from app.store import Store


def _client():
    svc = MeteringService(Store(), Wallet("100000"))
    return TestClient(create_app(svc)), svc


def test_end_to_end_http():
    client, svc = _client()
    r = client.post("/admin/plans", json={
        "event_id": "p1", "plan_id": "basic", "name": "基础版",
        "base_price": "100.00", "included_quantity": "1000", "overage_unit_price": "0.10",
    })
    assert r.status_code == 200
    r = client.post("/subscriptions", json={
        "event_id": "s1", "subscription_id": "sub-1", "customer_id": "c1",
        "plan_id": "basic", "start_at": "2026-01-01T00:00:00Z",
    })
    assert r.status_code == 200

    r = client.post("/usage", json={
        "event_id": "u1", "subscription_id": "sub-1",
        "ts": "2026-01-12T00:00:00Z", "quantity": "1300",
    })
    assert r.status_code == 200 and r.json()["period_index"] == 0

    # 负额 => 422 + 稳定错误码
    r = client.post("/usage", json={
        "event_id": "u2", "subscription_id": "sub-1",
        "ts": "2026-01-12T00:00:00Z", "quantity": "-1",
    })
    assert r.status_code == 422 and r.json()["error"] == "usage_negative_quantity"

    # 缺字段 => 422，错误码可区分
    r = client.post("/usage", json={
        "event_id": "u3", "subscription_id": "sub-1",
        "ts": "2026-01-12T00:00:00Z",
    })
    assert r.status_code == 422 and r.json()["error"] == "missing_field"

    # 重复结算链路
    r = client.post("/subscriptions/sub-1/invoices", json={
        "event_id": "iv1", "period_index": 0, "as_of": "2026-02-02T00:00:00Z",
    })
    inv = r.json()
    assert inv["total_amount"] == "130.00"
    r = client.post(f"/invoices/{inv['invoice_id']}/settle", json={
        "event_id": "st1", "request_id": "req-1",
    })
    assert r.status_code == 200 and r.json()["status"] == "settled"
    r = client.post(f"/invoices/{inv['invoice_id']}/settle", json={
        "event_id": "st2", "request_id": "req-2",
    })
    assert r.status_code == 409 and r.json()["error"] == "invoice_already_settled"
    assert svc.wallet.balance == Decimal("100000") - Decimal("130.00")

    r = client.post("/admin/replay")
    assert r.status_code == 200 and r.json()["events"] >= 5

"""账单结算：重复结算、结算失败重试、幂等不重复扣款。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.clock import FixedClock
from app.errors import DomainError, ErrorCode
from app.models import EventOutcome
from app.payment import PaymentGateway, PaymentResult
from app.services import MeteringService
from app.storage import InMemoryStore

ANCHOR = datetime(2026, 1, 1, 9, 0)


def _billed_service(total=Decimal("1200"), *, decline=False):
    gateway = PaymentGateway()
    svc = MeteringService(store=InMemoryStore(), clock=FixedClock(ANCHOR),
                          gateway=gateway)
    pid = svc.create_plan("p1", "基础版", Decimal("100"),
                          Decimal("1000"), Decimal("0.1")).data["plan"]["id"]
    sid = svc.create_subscription("s1", pid).data["subscription"]["id"]
    svc.report_usage("u1", sid, total, datetime(2026, 1, 10, 9), "m")
    svc.generate_invoice("i1", sid, 0)
    return svc, gateway, sid


def test_settle_success_then_duplicate_event_does_not_charge_again():
    svc, gateway, sid = _billed_service()
    r1 = svc.settle("pay1", sid, 0)
    assert r1.data["invoice"]["status"] == "settled"
    txn = r1.data["invoice"]["payment_txn_id"]
    assert txn and len(gateway._captured) == 1

    # 同一结算事件重放 -> duplicate，交易号相同，扣款记录不增加
    r2 = svc.settle("pay1", sid, 0)
    assert r2.outcome == EventOutcome.DUPLICATE
    assert r2.data["invoice"]["payment_txn_id"] == txn
    assert len(gateway._captured) == 1


def test_new_settlement_event_on_settled_invoice_is_rejected():
    svc, gateway, sid = _billed_service()
    svc.settle("pay1", sid, 0)
    with pytest.raises(DomainError) as ei:
        svc.settle("pay2", sid, 0)
    assert ei.value.code == ErrorCode.INVOICE_ALREADY_SETTLED
    # 拒绝事件也不触发新扣款
    assert len(gateway._captured) == 1


def test_failed_settlement_can_retry_without_double_charge():
    svc, gateway, sid = _billed_service()
    # 第一次结算模拟渠道失败
    with pytest.raises(DomainError) as ei:
        svc.settle("pay1", sid, 0, decline_once=True)
    assert ei.value.code == ErrorCode.PAYMENT_DECLINED
    assert svc.get_invoice(sid, 0)["status"] == "failed"
    assert len(gateway._captured) == 0  # 失败未扣款

    # 用一个新的结算事件重试（pay1 在台账里是 rejected，也可直接重放）
    r = svc.settle("retry1", sid, 0)
    assert r.data["invoice"]["status"] == "settled"
    assert r.data["invoice"]["payment_txn_id"]
    assert len(gateway._captured) == 1  # 只成功扣了一次

    # 已失败的 pay1 重放：仍给出首次的“渠道拒绝”结论
    with pytest.raises(DomainError) as ei2:
        svc.settle("pay1", sid, 0)
    assert ei2.value.code == ErrorCode.PAYMENT_DECLINED
    assert len(gateway._captured) == 1


def test_gateway_idempotency_key_prevents_duplicate_capture_directly():
    gw = PaymentGateway()
    a = gw.charge("k1", Decimal("10"), "CNY")
    b = gw.charge("k1", Decimal("10"), "CNY")
    assert a.result == PaymentResult.CAPTURED
    assert b.txn_id == a.txn_id


def test_zero_amount_invoice_settles_without_txn():
    # 构造一个零元账单（套餐基础费 0，无超量）
    svc = MeteringService(store=InMemoryStore(), clock=FixedClock(ANCHOR))
    pid = svc.create_plan("p1", "免费版", Decimal("0"),
                          Decimal("1000"), Decimal("0")).data["plan"]["id"]
    sid = svc.create_subscription("s1", pid).data["subscription"]["id"]
    svc.generate_invoice("i1", sid, 0)
    r = svc.settle("pay1", sid, 0)
    assert r.data["invoice"]["status"] == "settled"
    assert r.data["invoice"]["total_amount"] == "0.00"


def test_settle_before_generation_is_not_found():
    svc = MeteringService(store=InMemoryStore(), clock=FixedClock(ANCHOR))
    pid = svc.create_plan("p1", "基础版", Decimal("100"),
                          Decimal("1000"), Decimal("0.1")).data["plan"]["id"]
    sid = svc.create_subscription("s1", pid).data["subscription"]["id"]
    with pytest.raises(DomainError) as ei:
        svc.settle("pay1", sid, 0)
    assert ei.value.code == ErrorCode.INVOICE_NOT_FOUND

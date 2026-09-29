"""结算幂等：重复结算、并发结算、失败重试都只能扣一次钱。"""
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.errors import ErrorCode, MeteringError

log = logging.getLogger("metering.tests.settle")


def _close_p0_invoice(svc, event_id="iv1"):
    return svc.invoice_period(
        {
            "event_id": event_id,
            "subscription_id": "s1",
            "period_index": 0,
            "as_of": "2026-02-02T00:00:00Z",
        }
    )


def test_duplicate_settle_charges_once(service):
    inv = _close_p0_invoice(service)
    r1 = service.settle(
        {"event_id": "st1", "invoice_id": inv["invoice_id"], "request_id": "req-1"}
    )
    # 同一事件/请求重试：返回缓存结论，钱包不再变化
    r2 = service.settle(
        {"event_id": "st1", "invoice_id": inv["invoice_id"], "request_id": "req-1"}
    )
    assert r1["status"] == r2["status"] == "settled"
    assert service.wallet.balance == Decimal("100000") - Decimal("100.00")
    log.info("账期=p0 账单=%s 判定依据=成功台账存在且 request_id 相同 => 幂等返回", inv["invoice_id"])

    # 不同请求再结算：明确拒绝，不扣款
    with pytest.raises(MeteringError) as ei:
        service.settle(
            {"event_id": "st2", "invoice_id": inv["invoice_id"], "request_id": "req-2"}
        )
    assert ei.value.code is ErrorCode.INVOICE_ALREADY_SETTLED
    assert service.wallet.balance == Decimal("100000") - Decimal("100.00")
    log.info("判定依据=已结算但 request_id 不同 => %s", ei.value.code.value)


def test_concurrent_settle_charges_once(service):
    inv = _close_p0_invoice(service)

    outcomes = []

    def one(i):
        try:
            service.settle(
                {
                    "event_id": f"st-{i}",
                    "invoice_id": inv["invoice_id"],
                    "request_id": f"req-{i}",
                }
            )
            outcomes.append("settled")
        except MeteringError as exc:
            assert exc.code is ErrorCode.INVOICE_ALREADY_SETTLED
            outcomes.append("rejected")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(8)))

    # 只允许一个请求结算成功，其余 7 个拿到明确的“已结算”结论
    assert outcomes.count("settled") == 1
    assert outcomes.count("rejected") == 7
    assert service.wallet.balance == Decimal("100000") - Decimal("100.00")
    ledger = service.store.get_settlement(inv["invoice_id"])
    assert ledger is not None
    log.info("并发 8 个不同请求：成功 1 / 拒绝 7，成功 request=%s，判定依据=锁 + 成功台账唯一",
             ledger.request_id)


def test_failed_payment_retry_does_not_double_charge():
    from app.billing import Wallet
    from app.service import MeteringService
    from app.store import Store

    svc = MeteringService(Store(), Wallet(Decimal("0")))
    svc.define_plan(
        {
            "event_id": "p",
            "plan_id": "basic",
            "name": "基础版",
            "base_price": "100.00",
            "included_quantity": "0",
            "overage_unit_price": "1",
        }
    )
    svc.create_subscription(
        {
            "event_id": "s",
            "subscription_id": "s1",
            "customer_id": "c1",
            "plan_id": "basic",
            "start_at": "2026-01-01T00:00:00Z",
        }
    )
    inv = _close_p0_invoice(svc)
    with pytest.raises(MeteringError) as ei:
        svc.settle({"event_id": "st-fail", "invoice_id": inv["invoice_id"], "request_id": "r1"})
    assert ei.value.code is ErrorCode.INVOICE_SETTLE_FAILED
    # 失败时钱包未扣、账单仍 PENDING、事件保持 PENDING 可重试
    assert svc.wallet.balance == 0
    assert svc.store.get_invoice(inv["invoice_id"]).status.value == "pending"
    assert svc.store.get_event("st-fail").status.value == "pending"
    log.info("账期=p0 判定依据=余额不足支付失败，不记成功台账，事件保持 pending")

    svc.wallet._balance = Decimal("500")
    result = svc.settle(
        {"event_id": "st-fail", "invoice_id": inv["invoice_id"], "request_id": "r1"}
    )
    assert result["status"] == "settled"
    assert svc.wallet.balance == Decimal("400.00")
    assert len(svc.wallet.paid_keys) == 1
    log.info("充值后重试：仅 1 笔扣款，幂等键=%s", next(iter(svc.wallet.paid_keys)))


def test_crash_after_payment_retry_charges_once(service):
    """支付渠道已扣款，但本地台账写入前崩溃：重试绝不能二次扣款。"""
    inv = _close_p0_invoice(service)
    service.store.crash_after_payment.add(inv["invoice_id"])

    try:
        service.settle(
            {"event_id": "st1", "invoice_id": inv["invoice_id"], "request_id": "req-1"}
        )
        assert False, "应当模拟崩溃"
    except RuntimeError:
        pass

    # 钱已扣，但账单仍 pending、无成功台账
    assert service.wallet.balance == Decimal("100000") - Decimal("100.00")
    assert service.store.get_invoice(inv["invoice_id"]).status.value == "pending"
    assert service.store.get_settlement(inv["invoice_id"]) is None
    assert service.store.get_event("st1").status.value == "pending"
    log.info("账单=%s 判定=interrupted_after_payment：渠道已扣，本地无台账", inv["invoice_id"])

    # 崩溃恢复：钱包用同一幂等键直接放行（不二次扣款），台账补齐
    fixed = service.recover_pending()
    assert fixed == 1
    assert service.wallet.balance == Decimal("100000") - Decimal("100.00")
    assert service.store.get_invoice(inv["invoice_id"]).status.value == "settled"
    assert len(service.wallet.paid_keys) == 1
    log.info("恢复后判定依据=钱包幂等键 %s 只扣一次 + 成功台账补齐",
             next(iter(service.wallet.paid_keys)))

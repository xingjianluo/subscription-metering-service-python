"""账单：超量、套餐变更比例、中途取消抵扣；金额可解释、可复现。"""
import logging

log = logging.getLogger("metering.tests.billing")


def _invoice(svc, period=0, as_of="2026-02-02T00:00:00Z", event_id="iv1"):
    inv = svc.invoice_period(
        {
            "event_id": event_id,
            "subscription_id": "s1",
            "period_index": period,
            "as_of": as_of,
        }
    )
    log.info(
        "账单=%s 账期=p%s 窗口=[%s,%s) 明细=%s 总额=%s 判定依据=比例基础费+分段额度+超量+取消抵扣",
        inv["invoice_id"],
        period,
        inv["period_start"],
        inv["period_end"],
        [(l["line_type"], l["amount"]) for l in inv["lines"]],
        inv["total_amount"],
    )
    return inv


def test_overage_billing(service):
    service.report_usage({"event_id": "u1", "subscription_id": "s1",
                          "ts": "2026-01-20T00:00:00Z", "quantity": "1300"})
    inv = _invoice(service)
    # 基础费 100 + 超量 (1300-1000)*0.10 = 130
    assert inv["total_amount"] == "130.00"
    assert [l["line_type"] for l in inv["lines"]] == ["base", "overage"]


def test_plan_change_proration(service):
    # 1/16 00:00 升级 pro：31 天账期，前 15 天 basic，后 16 天 pro
    service.change_plan(
        {
            "event_id": "ev-pc",
            "subscription_id": "s1",
            "plan_id": "pro",
            "effective_from": "2026-01-16T00:00:00Z",
        }
    )
    service.report_usage({"event_id": "u1", "subscription_id": "s1",
                          "ts": "2026-01-10T00:00:00Z", "quantity": "2000"})
    service.report_usage({"event_id": "u2", "subscription_id": "s1",
                          "ts": "2026-01-20T00:00:00Z", "quantity": "6000"})
    inv = _invoice(service)
    types = [l["line_type"] for l in inv["lines"]]
    assert types == ["base", "overage", "base", "overage"]
    # 总额 = 明细之和（舍入口径统一）
    from decimal import Decimal
    assert inv["total_amount"] == str(
        sum((Decimal(l["amount"]) for l in inv["lines"]), Decimal("0"))
    )
    assert inv["total_amount"] == "525.81"


def test_midperiod_cancel_credit(service):
    service.cancel_subscription(
        {
            "event_id": "ev-cancel",
            "subscription_id": "s1",
            "canceled_at": "2026-01-10T00:00:00Z",
        }
    )
    service.report_usage({"event_id": "u1", "subscription_id": "s1",
                          "ts": "2026-01-05T00:00:00Z", "quantity": "500"})
    inv = _invoice(service)
    # 9 个有效天：基础费净额 100*9/31=29.03
    # 内含额度按比例 1000*9/31=290.32，超量 (500-290.32)*0.10=20.97
    assert inv["total_amount"] == "50.00"
    assert [l["line_type"] for l in inv["lines"]] == ["base", "overage", "credit"]
    credit = [l for l in inv["lines"] if l["line_type"] == "credit"]
    assert len(credit) == 1
    assert credit[0]["amount"] == "-70.97"  # 100*22/31
    log.info("取消抵扣判定依据=整期基础费 - 未使用天数(%s/31)*100",
             credit[0]["basis"]["unused_days"])


def test_invoice_generation_is_idempotent(service):
    inv1 = _invoice(service, event_id="iv1")
    inv2 = _invoice(service, event_id="iv1")
    assert inv1["invoice_id"] == inv2["invoice_id"]
    assert inv2["total_amount"] == inv1["total_amount"]

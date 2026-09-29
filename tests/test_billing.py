"""出账规则：超量、套餐变更、中途取消的按比例费用与抵扣，稳定性可解释。"""

from datetime import datetime
from decimal import Decimal

from app.billing import compute_invoice
from app.models import (
    InvoiceStatus,
    LineItemKind,
    Plan,
    PlanAssignment,
    Subscription,
    SubscriptionStatus,
    UsageRecord,
)

S = datetime(2026, 1, 1)
E = datetime(2026, 2, 1)


def _plans():
    basic = Plan.create("基础版", Decimal("100"), Decimal("1000"), Decimal("0.1"))
    pro = Plan.create("专业版", Decimal("200"), Decimal("3000"), Decimal("0.2"))
    return {basic.id: basic, pro.id: pro}


def _change(sub, new_plan_id, at):
    sub.plan_history[-1].effective_to = at
    sub.plan_history.append(PlanAssignment(new_plan_id, at, None))
    sub.plan_id = new_plan_id


def _amounts(invoice, kind):
    return [li.amount for li in invoice.line_items if li.kind == kind]


def test_plain_overage_bill():
    plans = _plans()
    basic = plans[next(iter(plans))]
    sub = Subscription.create(basic.id, S)
    records = [UsageRecord(sub.id, Decimal("1200"), datetime(2026, 1, 10), "m")]
    inv = compute_invoice("i", sub, plans, S, E, records, datetime(2026, 2, 2))
    assert inv.total_amount == Decimal("120.00")
    over = _amounts(inv, LineItemKind.OVERAGE)
    assert over == [Decimal("20.00")]


def test_plan_change_produces_credit_and_charge_and_segment_overage():
    plans = _plans()
    ids = list(plans)
    basic, pro = plans[ids[0]], plans[ids[1]]
    sub = Subscription.create(basic.id, S)
    _change(sub, pro.id, datetime(2026, 1, 16))
    records = [
        UsageRecord(sub.id, Decimal("1200"), datetime(2026, 1, 10), "m"),
        UsageRecord(sub.id, Decimal("5000"), datetime(2026, 1, 20), "m"),
    ]
    inv = compute_invoice("i", sub, plans, S, E, records, datetime(2026, 2, 2))
    # 基础 100；抵扣 -100*16/31；补差 200*16/31；两段超量
    assert _amounts(inv, LineItemKind.BASE) == [Decimal("100.00")]
    assert _amounts(inv, LineItemKind.PRORATED_CREDIT) == [Decimal("-51.61")]
    assert _amounts(inv, LineItemKind.PRORATED_CHARGE) == [Decimal("103.23")]
    assert _amounts(inv, LineItemKind.OVERAGE) == [Decimal("71.61"), Decimal("690.32")]
    assert inv.total_amount == Decimal("913.55")


def test_mid_cycle_cancellation_prorates_credit_only():
    plans = _plans()
    basic = plans[next(iter(plans))]
    sub = Subscription.create(basic.id, S)
    sub.status = SubscriptionStatus.CANCELLED
    sub.cancelled_at = datetime(2026, 1, 21)
    records = [UsageRecord(sub.id, Decimal("500"), datetime(2026, 1, 10), "m")]
    inv = compute_invoice("i", sub, plans, S, E, records, datetime(2026, 2, 2))
    # 整月 100，退未使用 11 天：-100*11/31 = -35.48；用量未超按比例额度
    assert _amounts(inv, LineItemKind.PRORATED_CREDIT) == [Decimal("-35.48")]
    assert _amounts(inv, LineItemKind.OVERAGE) == []
    assert inv.total_amount == Decimal("64.52")


def test_invoice_is_deterministic_and_checksum_stable():
    plans = _plans()
    basic = plans[next(iter(plans))]
    sub = Subscription.create(basic.id, S)
    records = [
        UsageRecord(sub.id, Decimal("300"), datetime(2026, 1, 20), "m"),
        UsageRecord(sub.id, Decimal("200"), datetime(2026, 1, 5), "m"),
    ]
    inv1 = compute_invoice("i", sub, plans, S, E, records, datetime(2026, 2, 2))
    # 相同输入再次计算（模拟重算/重放）必须逐行一致
    inv2 = compute_invoice("i", sub, plans, S, E, list(reversed(records)),
                           datetime(2026, 2, 2))
    assert [li.to_dict() for li in inv1.line_items] == \
           [li.to_dict() for li in inv2.line_items]
    assert inv1.total_amount == inv2.total_amount
    assert inv1.content_checksum == inv2.content_checksum
    assert inv1.status == InvoiceStatus.DRAFT

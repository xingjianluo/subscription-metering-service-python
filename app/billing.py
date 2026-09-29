"""出账：按周期生成稳定、可解释的账单。

定价模型（望远镜等价，逐行金额四舍五入到分）：

* BASE            账期开始时所属套餐的整月基础费；
* PRORATED_CREDIT 每个套餐变更/取消边界，把“当前套餐未使用天数”按比例退为负向行；
* PRORATED_CHARGE 每个套餐变更边界，把“新套餐从边界到账期末”按比例计为正向行；
* OVERAGE         每个套餐片段内，实际用量超出该片段按比例套餐额度的部分。

上述各项代数上恒等于“把账期按套餐切成连续片段、逐片段按日按比例计费”，
因此多行之和稳定且无重复计费；抵扣单独成负向行，账目可解释。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.errors import DomainError, ErrorCode
from app.models import (
    Invoice,
    InvoiceStatus,
    LineItem,
    LineItemKind,
    Plan,
    PlanAssignment,
    Subscription,
    SubscriptionStatus,
    UsageRecord,
)

_CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def _days_ratio(days: int, total_days: int) -> Decimal:
    if total_days <= 0:
        raise DomainError(ErrorCode.INVALID_PERIOD, "账期天数必须为正")
    return Decimal(days) / Decimal(total_days)


def segment_plan_coverage(
    subscription: Subscription,
    period_start,
    period_end,
) -> list[tuple[PlanAssignment, "datetime", "datetime"]]:
    """把账期切成与 plan_history 相交的连续片段（不查 Plan 表）。

    取消时末段截断到 cancelled_at；返回 (assignment, seg_start, seg_end)。
    """
    segments: list[tuple[PlanAssignment, object, object]] = []
    for assignment in subscription.plan_history:
        seg_start = max(period_start, assignment.effective_from)
        open_end = assignment.effective_to if assignment.effective_to is not None else period_end
        seg_end = min(period_end, open_end)
        if (
            subscription.status == SubscriptionStatus.CANCELLED
            and subscription.cancelled_at is not None
        ):
            seg_end = min(seg_end, subscription.cancelled_at)
        if seg_end > seg_start:
            segments.append((assignment, seg_start, seg_end))
    return segments


def _prorated(base: Decimal, days: int, total_days: int) -> Decimal:
    return _money(Decimal(base) * _days_ratio(days, total_days))


def compute_invoice(
    invoice_id: str,
    subscription: Subscription,
    plans: dict[str, Plan],
    period_start,
    period_end,
    records: list[UsageRecord],
    created_at,
    currency: str = "CNY",
) -> Invoice:
    from app.periods import day_count

    total_days = day_count(period_start, period_end)
    if total_days <= 0:
        raise DomainError(ErrorCode.INVALID_PERIOD, "账期天数必须为正",
                          details={"period_start": period_start.isoformat(),
                                   "period_end": period_end.isoformat()})

    def plan_of(plan_id: str) -> Plan:
        plan = plans.get(plan_id)
        if plan is None:
            raise DomainError(ErrorCode.PLAN_NOT_FOUND,
                              f"账单引用的套餐不存在: {plan_id}",
                              details={"plan_id": plan_id})
        return plan

    segments = segment_plan_coverage(subscription, period_start, period_end)
    if not segments:
        raise DomainError(ErrorCode.INVALID_PERIOD,
                          "账期内没有有效的套餐归属片段，无法出账")

    items: list[LineItem] = []

    # 1) 账期起始套餐的整月基础费
    first_assignment = segments[0][0]
    first_plan = plan_of(first_assignment.plan_id)
    base_amount = _money(first_plan.base_price)
    items.append(LineItem(
        kind=LineItemKind.BASE,
        plan_id=first_plan.id,
        description=(
            f"套餐 {first_plan.name} 整月基础费 "
            f"({period_start.date()} ~ {period_end.date()}，{total_days} 天)"
        ),
        quantity=Decimal(total_days),
        unit_price=first_plan.base_price,
        amount=base_amount,
        period_start=period_start,
        period_end=period_end,
    ))

    # 2) 变更/取消边界的抵扣与补差
    #    遍历历史，凡边界落在账期内部都生成调整行（信用区间为 [boundary, period_end)）。
    current_plan = first_plan
    for assignment in subscription.plan_history[1:]:
        boundary = assignment.effective_from
        if boundary <= period_start or boundary >= period_end:
            continue
        unused_days = day_count(boundary, period_end)
        # 退当前套餐未使用部分
        credit = -_prorated(current_plan.base_price, unused_days, total_days)
        if credit != 0:
            items.append(LineItem(
                kind=LineItemKind.PRORATED_CREDIT,
                plan_id=current_plan.id,
                description=(
                    f"套餐变更抵扣：{current_plan.name} 未使用 {unused_days}/{total_days} 天"
                ),
                quantity=Decimal(unused_days),
                unit_price=current_plan.base_price,
                amount=credit,
                period_start=boundary,
                period_end=period_end,
            ))
        # 计新套餐从边界到期末
        new_plan = plan_of(assignment.plan_id)
        charge = _prorated(new_plan.base_price, unused_days, total_days)
        if charge != 0:
            items.append(LineItem(
                kind=LineItemKind.PRORATED_CHARGE,
                plan_id=new_plan.id,
                description=(
                    f"套餐变更补差：{new_plan.name} 使用 {unused_days}/{total_days} 天"
                ),
                quantity=Decimal(unused_days),
                unit_price=new_plan.base_price,
                amount=charge,
                period_start=boundary,
                period_end=period_end,
            ))
        current_plan = new_plan

    # 取消边界：只退当前套餐未使用部分
    if (
        subscription.status == SubscriptionStatus.CANCELLED
        and subscription.cancelled_at is not None
        and period_start < subscription.cancelled_at < period_end
    ):
        boundary = subscription.cancelled_at
        unused_days = day_count(boundary, period_end)
        credit = -_prorated(current_plan.base_price, unused_days, total_days)
        if credit != 0:
            items.append(LineItem(
                kind=LineItemKind.PRORATED_CREDIT,
                plan_id=current_plan.id,
                description=(
                    f"中途取消抵扣：{current_plan.name} 未使用 {unused_days}/{total_days} 天"
                ),
                quantity=Decimal(unused_days),
                unit_price=current_plan.base_price,
                amount=credit,
                period_start=boundary,
                period_end=period_end,
            ))

    # 3) 每个片段的超量费用：片段额度按日按比例，片段内用量超出即计费
    for assignment, seg_start, seg_end in segments:
        plan = plan_of(assignment.plan_id)
        seg_days = day_count(seg_start, seg_end)
        seg_usage = sum(
            (Decimal(r.quantity) for r in records
             if seg_start <= r.occurred_at < seg_end),
            Decimal("0"),
        )
        included = plan.included_quantity * _days_ratio(seg_days, total_days)
        overage_qty = seg_usage - included
        if overage_qty > 0:
            amount = _money(overage_qty * plan.overage_unit_price)
            items.append(LineItem(
                kind=LineItemKind.OVERAGE,
                plan_id=plan.id,
                description=(
                    f"套餐 {plan.name} 片段超量：用量 {seg_usage}，"
                    f"按比例额度 {round(included, 4)}，超出 {round(overage_qty, 4)}"
                ),
                quantity=overage_qty,
                unit_price=plan.overage_unit_price,
                amount=amount,
                period_start=seg_start,
                period_end=seg_end,
            ))

    invoice = Invoice(
        id=invoice_id,
        subscription_id=subscription.id,
        period_start=period_start,
        period_end=period_end,
        line_items=items,
        status=InvoiceStatus.DRAFT,
        currency=currency,
        created_at=created_at,
    )
    invoice.total_amount = invoice.recompute_total()
    invoice.content_checksum = invoice.compute_checksum()
    return invoice

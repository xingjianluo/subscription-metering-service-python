"""计费引擎：时间窗口聚合、按比例费用/抵扣、账单生成与结算。

规则口径：
- 账期按订阅锚点日按月切分，区间左闭右开；用量按事件时间戳落入对应窗口，
  因此重复/乱序上报的聚合结果一致，跨账期天然隔离（service 层分桶）。
- 套餐变更把账期切成小段，基础费与内含额度都按 小段天数/账期天数 比例计算。
- 中途取消：取消所在小段先按整期基础费收取，再用一条 credit 行抵扣未使用天数，
  净额与按比例收费完全一致，抵扣金额在账单上显式可见、可解释。
- 所有金额按 ROUND_HALF_UP 舍入到分；账单总额 = 各明细行金额之和。
"""
from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from app.errors import ErrorCode, MeteringError
from app.models import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LineType,
    Plan,
    Subscription,
    TimelineSegment,
    UsageRecord,
)
from app.periods import days_between, period_bounds
from app.store import Store

CENTS = Decimal("0.01")
_QUANT_Q = Decimal("0.00000001")


def money(value: Decimal) -> Decimal:
    """统一舍入口径：四舍五入到分（ROUND_HALF_UP），保证结果可复现。"""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def aggregate_usage(
    usages: list[UsageRecord], window_start: datetime, window_end: datetime
) -> Decimal:
    """聚合落在 [window_start, window_end) 的用量。

    每条用量事件以 event_id 在事件流中唯一，重复投递在更上层被去重；
    本函数只做时间窗口过滤与求和，乱序输入得到相同结果（聚合的确定性）。
    """
    total = Decimal("0")
    for u in usages:
        if window_start <= u.ts < window_end:
            total += u.quantity
    return total


def build_segments(
    sub: Subscription, period_start: datetime, period_end: datetime
) -> list[TimelineSegment]:
    """按套餐变更/取消把账期切成左闭右开小段，零长度段剔除。"""
    total_days = days_between(period_start, period_end)

    # 收集账期内的生效边界：套餐变更点 + 取消点（取消点是最后一段的右端）
    cuts: list[datetime] = [period_start]
    for phase in sub.phases:
        if period_start < phase.effective_from < period_end:
            cuts.append(phase.effective_from)

    cancel = sub.canceled_at
    effective_end = period_end
    if cancel is not None and period_start <= cancel < period_end:
        cuts.append(cancel)
        effective_end = cancel
    cuts = sorted(set(cuts))
    if effective_end not in cuts:
        cuts.append(effective_end)
    cuts = sorted(set(cuts))

    def plan_at(ts: datetime) -> str:
        current = sub.phases[0].plan_id
        for phase in sub.phases:
            if ts >= phase.effective_from:
                current = phase.plan_id
        return current

    segments: list[TimelineSegment] = []
    for left, right in zip(cuts, cuts[1:]):
        if right <= period_start or left >= effective_end:
            continue
        seg_start = max(left, period_start)
        seg_end = min(right, effective_end)
        if seg_end <= seg_start:
            continue
        segments.append(
            TimelineSegment(
                plan_id=plan_at(seg_start),
                start=seg_start,
                end=seg_end,
                days=days_between(seg_start, seg_end),
                total_days=total_days,
            )
        )
    return segments


def _invoice_id(subscription_id: str, period_index: int) -> str:
    return f"inv-{subscription_id}-p{period_index:04d}"


def generate_invoice(
    store: Store,
    subscription_id: str,
    period_index: int,
    as_of: datetime | None = None,
    recovered: bool = False,
) -> Invoice:
    """为某账期生成账单；同 (订阅, 账期) 幂等返回同一张。账期必须已关闭。"""
    sub = store.find_subscription(subscription_id)
    if sub is None:
        raise MeteringError(
            ErrorCode.UNKNOWN_SUBSCRIPTION,
            f"subscription {subscription_id} not found",
            {"subscription_id": subscription_id},
        )

    period_start, period_end = period_bounds(sub.start_at, period_index)
    now = as_of
    if now is not None and now < period_end:
        raise MeteringError(
            ErrorCode.PERIOD_NOT_CLOSED,
            f"period {period_index} is not closed until {_iso(period_end)}",
            {
                "period_index": period_index,
                "period_end": _iso(period_end),
                "as_of": _iso(now),
            },
        )

    existing = store.get_invoice_by_key(subscription_id, period_index)
    if existing is not None:
        return existing

    segments = build_segments(sub, period_start, period_end)
    period_usages = store.list_usage(subscription_id, period_index)
    if not segments:
        raise MeteringError(
            ErrorCode.INVALID_STATE,
            "subscription has no active phase within the period",
            {"subscription_id": subscription_id, "period_index": period_index},
        )

    plan_at_cancel: Plan | None = None
    cancel = sub.canceled_at
    lines: list[InvoiceLine] = []

    for seg in segments:
        plan = store.get_plan(seg.plan_id)
        if plan is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_PLAN,
                f"plan {seg.plan_id} not found",
                {"plan_id": seg.plan_id},
            )
        ratio = (seg.days / seg.total_days).quantize(_QUANT_Q)
        seg_usage = aggregate_usage(period_usages, seg.start, seg.end)
        is_cancel_segment = (
            cancel is not None
            and period_start <= cancel < period_end
            and seg.end == cancel
        )

        if is_cancel_segment:
            # 取消所在小段：先按整期基础费收取，未使用部分在末尾以 credit 行抵扣
            base_amount = money(plan.base_price)
            plan_at_cancel = plan
            base_desc = f"{plan.name} 整期基础费（中途取消，未使用部分另行抵扣）"
            base_basis_ratio = Decimal("1")
        else:
            base_amount = money(plan.base_price * ratio)
            base_desc = f"{plan.name} 按比例基础费"
            base_basis_ratio = ratio

        lines.append(
            InvoiceLine(
                line_type=LineType.BASE,
                plan_id=plan.plan_id,
                description=base_desc,
                amount=base_amount,
                basis={
                    "segment_start": _iso(seg.start),
                    "segment_end": _iso(seg.end),
                    "segment_days": str(seg.days.quantize(_QUANT_Q)),
                    "period_days": str(seg.total_days.quantize(_QUANT_Q)),
                    "ratio": str(base_basis_ratio),
                    "full_base_price": str(plan.base_price),
                },
            )
        )

        included = plan.included_quantity * ratio
        overage_qty = seg_usage - included
        if overage_qty > 0:
            overage_fee = money(overage_qty * plan.overage_unit_price)
            lines.append(
                InvoiceLine(
                    line_type=LineType.OVERAGE,
                    plan_id=plan.plan_id,
                    description=f"{plan.name} 超量费用",
                    amount=overage_fee,
                    basis={
                        "segment_start": _iso(seg.start),
                        "segment_end": _iso(seg.end),
                        "usage": str(seg_usage),
                        "included_prorated": str(included.quantize(_QUANT_Q)),
                        "overage_quantity": str(overage_qty.quantize(_QUANT_Q)),
                        "unit_price": str(plan.overage_unit_price),
                        "ratio": str(ratio),
                    },
                )
            )

    # 中途取消的抵扣：未使用天数占账期比例 × 取消时套餐整期基础费
    if plan_at_cancel is not None and cancel is not None and period_start <= cancel < period_end:
        unused_days = days_between(cancel, period_end)
        unused_ratio = (unused_days / days_between(period_start, period_end)).quantize(_QUANT_Q)
        credit_amount = -money(plan_at_cancel.base_price * unused_ratio)
        if credit_amount < 0:
            lines.append(
                InvoiceLine(
                    line_type=LineType.CREDIT,
                    plan_id=plan_at_cancel.plan_id,
                    description="周期中途取消，未使用天数抵扣",
                    amount=credit_amount,
                    basis={
                        "canceled_at": _iso(cancel),
                        "unused_days": str(unused_days.quantize(_QUANT_Q)),
                        "period_days": str(days_between(period_start, period_end).quantize(_QUANT_Q)),
                        "unused_ratio": str(unused_ratio),
                        "full_base_price": str(plan_at_cancel.base_price),
                    },
                )
            )

    total = sum((ln.amount for ln in lines), Decimal("0"))
    invoice = Invoice(
        invoice_id=_invoice_id(subscription_id, period_index),
        subscription_id=subscription_id,
        period_index=period_index,
        period_start=period_start,
        period_end=period_end,
        currency=store.get_plan(segments[0].plan_id).currency,  # type: ignore[union-attr]
        lines=lines,
        total_amount=total,
    )
    store.put_invoice(invoice)
    return invoice


def settle_invoice(
    store: Store,
    invoice_id: str,
    request_id: str,
    wallet: "Wallet",
    at: datetime | None = None,
    recovered: bool = False,
) -> Invoice:
    """结算账单。

    - 已结算 + 同一 request_id：原样返回（重试幂等，不扣款）。
    - 已结算 + 不同 request_id：INVOICE_ALREADY_SETTLED，不重复扣款。
    - 待结算：调用钱包；成功才记台账并置 SETTLED；失败抛错、账单保持 PENDING。
    成功台账是“是否扣过钱”的唯一判据；钱包对同一幂等键也只扣一次，
    因此“扣款成功但记账前崩溃”的重试也不会重复扣钱。
    """
    invoice = store.find_invoice(invoice_id)
    if invoice is None:
        raise MeteringError(
            ErrorCode.UNKNOWN_INVOICE,
            f"invoice {invoice_id} not found",
            {"invoice_id": invoice_id},
        )

    ledger = store.get_settlement(invoice_id)
    if ledger is not None:
        if ledger.request_id == request_id:
            return invoice
        raise MeteringError(
            ErrorCode.INVOICE_ALREADY_SETTLED,
            f"invoice {invoice_id} already settled by request {ledger.request_id}",
            {"invoice_id": invoice_id, "original_request_id": ledger.request_id},
        )

    # 幂等键绑定请求：同一事件崩溃重试，钱包层面也不会二次扣款
    idem_key = f"{invoice_id}:{request_id}"
    if not wallet.pay(invoice.total_amount, idem_key):
        raise MeteringError(
            ErrorCode.INVOICE_SETTLE_FAILED,
            f"payment rejected for invoice {invoice_id}",
            {"invoice_id": invoice_id, "amount": str(invoice.total_amount)},
        )

    # 测试钩子：支付已成功、本地台账写入前崩溃
    crash = getattr(store, "crash_after_payment", None)
    if crash and invoice_id in crash:
        crash.discard(invoice_id)
        raise RuntimeError(f"simulated crash after payment for {invoice_id}")

    invoice.status = InvoiceStatus.SETTLED
    invoice.settlement_request_id = request_id
    invoice.settled_at = at
    store.record_settlement(
        LedgerEntry(
            invoice_id=invoice_id,
            request_id=request_id,
            amount=invoice.total_amount,
            success=True,
            at=at or invoice.settled_at or datetime.now(),  # type: ignore[arg-type]
            recovered=recovered,
        )
    )
    return invoice


class Wallet:
    """本地钱包（模拟支付渠道，不接真实渠道）。

    余额不足 => 支付失败且不扣钱；同一幂等键的重复支付请求只扣一次，
    用于覆盖“渠道侧成功、本地记账前崩溃”的场景。
    """

    def __init__(self, initial: Decimal | None = None) -> None:
        self._lock = threading.Lock()
        self._balance = Decimal("999999") if initial is None else Decimal(initial)
        self._paid: dict[str, Decimal] = {}

    def pay(self, amount: Decimal, idempotency_key: str) -> bool:
        amount = Decimal(amount)
        with self._lock:
            if idempotency_key in self._paid:
                return True
            if self._balance < amount:
                return False
            self._balance -= amount
            self._paid[idempotency_key] = amount
            return True

    @property
    def balance(self) -> Decimal:
        return self._balance

    @property
    def paid_keys(self) -> set[str]:
        return set(self._paid)

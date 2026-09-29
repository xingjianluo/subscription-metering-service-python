"""应用服务层：事件驱动的命令处理，统一加锁 + 台账幂等。

每个命令携带一个 event_id，处理流程：
  1. 获取全局锁（并发投递被串行化）；
  2. 查台账：同 id 同载荷 -> 回放首次结果；同 id 不同载荷 -> 冲突拒绝；
  3. 执行业务；业务拒绝（DomainError）也登记台账并可回放，状态不变；
  4. 成功登记 APPLIED 并原子保存快照（失败则整体丢弃，重来不残留）。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.billing import compute_invoice
from app.clock import Clock, SystemClock
from app.errors import DomainError, ErrorCode
from app.ledger import check_ledger, hash_payload
from app.metering import aggregate_window, insert_usage_ordered, validate_usage
from app.models import (
    EventOutcome,
    EventType,
    InvoiceStatus,
    LedgerEntry,
    Plan,
    PlanAssignment,
    Subscription,
    SubscriptionStatus,
    UsageRecord,
)
from app.payment import PaymentGateway
from app.periods import period_for, period_index_for
from app.settlement import settle_invoice
from app.storage import InMemoryStore, Store, StoreState, state_from_dict, state_to_dict

logger = logging.getLogger("metering")


@dataclass
class CommandResult:
    outcome: EventOutcome
    data: dict
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "data": self.data,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _invoice_id(subscription_id: str, period_index: int) -> str:
    digest = hashlib.sha256(
        f"{subscription_id}#{period_index}".encode("utf-8")
    ).hexdigest()[:16]
    return f"inv_{digest}"


def _invoice_to_dict(invoice) -> dict:
    return {
        "id": invoice.id,
        "subscription_id": invoice.subscription_id,
        "period_start": invoice.period_start.isoformat(),
        "period_end": invoice.period_end.isoformat(),
        "status": invoice.status.value,
        "total_amount": str(invoice.total_amount),
        "currency": invoice.currency,
        "payment_txn_id": invoice.payment_txn_id,
        "settled_at": invoice.settled_at.isoformat() if invoice.settled_at else None,
        "content_checksum": invoice.content_checksum,
        "line_items": [item.to_dict() for item in invoice.line_items],
    }


def _any_plan(state: StoreState, sub: Subscription) -> Plan:
    for assignment in sub.plan_history:
        plan = state.plans.get(assignment.plan_id)
        if plan is not None:
            return plan
    raise DomainError(ErrorCode.PLAN_NOT_FOUND, "订阅没有任何有效套餐",
                      details={"subscription_id": sub.id})


def _subscription_to_dict(sub: Subscription) -> dict:
    return {
        "id": sub.id,
        "plan_id": sub.plan_id,
        "status": sub.status.value,
        "anchored_at": sub.anchored_at.isoformat(),
        "cancelled_at": sub.cancelled_at.isoformat() if sub.cancelled_at else None,
        "plan_history": [{
            "plan_id": a.plan_id,
            "effective_from": a.effective_from.isoformat(),
            "effective_to": a.effective_to.isoformat() if a.effective_to else None,
        } for a in sub.plan_history],
    }


class MeteringService:
    def __init__(
        self,
        store: Store | None = None,
        clock: Clock | None = None,
        gateway: PaymentGateway | None = None,
    ) -> None:
        self.store = store or InMemoryStore()
        self.clock = clock
        self.gateway = gateway or PaymentGateway()

    def _now(self) -> datetime:
        return (self.clock or SystemClock()).now()

    # ------------------------------------------------------------ 包装

    def _run_event(
        self,
        event_id: str,
        event_type: EventType,
        payload: dict,
        period_label: str,
        fn,
    ) -> CommandResult:
        if not event_id or not str(event_id).strip():
            raise DomainError(ErrorCode.MISSING_FIELD, "缺少关键字段: event_id",
                              details={"field": "event_id"})

        with self.store.lock():
            committed = self.store.load()
            # 在“工作副本”上执行业务：只有 save 成功，修改才会被提交。
            # 若业务中途抛错或保存时崩溃，已提交快照保持不变，重来不残留。
            state = state_from_dict(state_to_dict(committed))
            fingerprint = hash_payload(event_type, payload)
            try:
                existing = check_ledger(state.ledger, event_id, event_type, fingerprint)
            except DomainError as conflict:
                logger.warning(
                    "事件判定为冲突 event_id=%s type=%s 账期=%s 依据=%s",
                    event_id, event_type.value, period_label, conflict.message,
                )
                raise

            if existing is not None:
                if existing.outcome == EventOutcome.REJECTED:
                    logger.info(
                        "重放拒绝事件 event_id=%s type=%s 账期=%s 判定=duplicate-rejected "
                        "依据=台账记录首次拒绝原因 %s",
                        event_id, event_type.value, period_label, existing.error_code,
                    )
                    # 重放被拒绝事件：抛出与首次完全一致的业务错误，
                    # 调用方拿到的失败原因稳定可区分，且状态不发生任何变化
                    raise DomainError(
                        ErrorCode(existing.error_code),
                        existing.error_message or "事件此前已被拒绝",
                        details={"event_id": event_id, "replay": True},
                    )
                logger.info(
                    "重放已处理事件 event_id=%s type=%s 账期=%s 判定=duplicate "
                    "依据=event_id 命中且载荷指纹一致，返回首次结果",
                    event_id, event_type.value, period_label,
                )
                return CommandResult(EventOutcome.DUPLICATE, existing.result)

            # 首次处理
            try:
                result = fn(state)
            except DomainError as exc:
                state.ledger[event_id] = LedgerEntry(
                    event_id=event_id, event_type=event_type, payload_hash=fingerprint,
                    outcome=EventOutcome.REJECTED, result={},
                    error_code=exc.code.value, error_message=exc.message,
                    recorded_at=self._now(),
                )
                self.store.save(state)
                logger.info(
                    "事件被拒绝 event_id=%s type=%s 账期=%s 判定=rejected 依据=%s",
                    event_id, event_type.value, period_label, exc.message,
                )
                raise

            state.ledger[event_id] = LedgerEntry(
                event_id=event_id, event_type=event_type, payload_hash=fingerprint,
                outcome=EventOutcome.APPLIED, result=result, recorded_at=self._now(),
            )
            self.store.save(state)
            logger.info(
                "事件首次应用 event_id=%s type=%s 账期=%s 判定=applied",
                event_id, event_type.value, period_label,
            )
            return CommandResult(EventOutcome.APPLIED, result)

    # ------------------------------------------------------------ 套餐

    def create_plan(
        self,
        event_id: str,
        name: str,
        base_price: Decimal,
        included_quantity: Decimal,
        overage_unit_price: Decimal,
        currency: str = "CNY",
    ) -> CommandResult:
        payload = {
            "name": name, "base_price": str(base_price),
            "included_quantity": str(included_quantity),
            "overage_unit_price": str(overage_unit_price), "currency": currency,
        }

        def work(state: StoreState) -> dict:
            plan = Plan.create(name, base_price, included_quantity,
                               overage_unit_price, currency)
            state.plans[plan.id] = plan
            return {"plan": {"id": plan.id, "name": plan.name,
                             "base_price": str(plan.base_price),
                             "included_quantity": str(plan.included_quantity),
                             "overage_unit_price": str(plan.overage_unit_price),
                             "currency": plan.currency}}

        return self._run_event(event_id, EventType.PLAN_CREATED, payload, "-", work)

    # ------------------------------------------------------------ 订阅

    def create_subscription(self, event_id: str, plan_id: str) -> CommandResult:
        payload = {"plan_id": plan_id}

        def work(state: StoreState) -> dict:
            if plan_id not in state.plans:
                raise DomainError(ErrorCode.PLAN_NOT_FOUND, f"套餐不存在: {plan_id}",
                                  details={"plan_id": plan_id})
            sub = Subscription.create(plan_id, self._now())
            state.subscriptions[sub.id] = sub
            return {"subscription": _subscription_to_dict(sub)}

        return self._run_event(event_id, EventType.SUBSCRIPTION_CREATED,
                               payload, "-", work)

    def change_plan(self, event_id: str, subscription_id: str, new_plan_id: str,
                    effective_at: datetime | None = None) -> CommandResult:
        eff = effective_at or self._now()
        payload = {"subscription_id": subscription_id, "new_plan_id": new_plan_id,
                   "effective_at": eff.isoformat()}

        def work(state: StoreState) -> dict:
            sub = state.subscriptions.get(subscription_id)
            if sub is None:
                raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND,
                                  f"订阅不存在: {subscription_id}",
                                  details={"subscription_id": subscription_id})
            if new_plan_id not in state.plans:
                raise DomainError(ErrorCode.PLAN_NOT_FOUND,
                                  f"套餐不存在: {new_plan_id}",
                                  details={"plan_id": new_plan_id})
            if sub.status != SubscriptionStatus.ACTIVE:
                raise DomainError(ErrorCode.SUBSCRIPTION_INACTIVE,
                                  "订阅已取消，不能变更套餐",
                                  details={"subscription_id": subscription_id})
            open_assignment = sub.plan_history[-1]
            if eff < open_assignment.effective_from:
                raise DomainError(
                    ErrorCode.CHANGE_TIME_INVALID,
                    "变更时间不能早于当前套餐生效时间",
                    details={"effective_at": eff.isoformat(),
                             "current_from": open_assignment.effective_from.isoformat()},
                )
            if new_plan_id == sub.plan_id:
                raise DomainError(ErrorCode.PLAN_CHANGE_SAME_PLAN,
                                  "新套餐与当前套餐相同，无需变更",
                                  details={"plan_id": new_plan_id})

            open_assignment.effective_to = eff
            sub.plan_history.append(PlanAssignment(new_plan_id, eff, None))
            sub.plan_id = new_plan_id
            idx = period_index_for(sub.anchored_at, eff)
            return {"subscription": _subscription_to_dict(sub), "period_index": idx}

        return self._run_event(event_id, EventType.PLAN_CHANGED,
                               payload, f"sub={subscription_id}", work)

    def cancel_subscription(self, event_id: str, subscription_id: str,
                            cancelled_at: datetime | None = None) -> CommandResult:
        at = cancelled_at or self._now()
        payload = {"subscription_id": subscription_id,
                   "cancelled_at": at.isoformat()}

        def work(state: StoreState) -> dict:
            sub = state.subscriptions.get(subscription_id)
            if sub is None:
                raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND,
                                  f"订阅不存在: {subscription_id}",
                                  details={"subscription_id": subscription_id})
            if sub.status == SubscriptionStatus.CANCELLED:
                raise DomainError(ErrorCode.ALREADY_CANCELLED,
                                  "订阅已取消，不能重复取消",
                                  details={"subscription_id": subscription_id,
                                           "cancelled_at": sub.cancelled_at.isoformat()})
            if at < sub.anchored_at:
                raise DomainError(ErrorCode.CHANGE_TIME_INVALID,
                                  "取消时间不能早于订阅起始时间",
                                  details={"cancelled_at": at.isoformat(),
                                           "anchored_at": sub.anchored_at.isoformat()})
            sub.status = SubscriptionStatus.CANCELLED
            sub.cancelled_at = at
            idx = period_index_for(sub.anchored_at, at)
            return {"subscription": _subscription_to_dict(sub), "period_index": idx}

        return self._run_event(event_id, EventType.SUBSCRIPTION_CANCELLED,
                               payload, f"sub={subscription_id}", work)

    # ------------------------------------------------------------ 用量

    def report_usage(
        self,
        event_id: str,
        subscription_id: str,
        quantity: Decimal,
        occurred_at: datetime,
        metric: str,
    ) -> CommandResult:
        payload = {
            "subscription_id": subscription_id, "quantity": str(quantity),
            "occurred_at": occurred_at.isoformat(), "metric": metric,
        }

        def work(state: StoreState) -> dict:
            sub = state.subscriptions.get(subscription_id)
            if sub is None:
                raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND,
                                  f"订阅不存在: {subscription_id}",
                                  details={"subscription_id": subscription_id})
            record = UsageRecord(subscription_id, Decimal(quantity), occurred_at, metric)
            idx = validate_usage(record, sub, sub.anchored_at)
            start, end = period_for(sub.anchored_at, idx)
            bucket = state.usage.setdefault((subscription_id, idx), [])
            insert_usage_ordered(bucket, record)
            window = aggregate_window(subscription_id, idx, sub.anchored_at, bucket)
            logger.info(
                "用量归入窗口 event_id=%s 账期=sub=%s period=%d [%s,%s) "
                "本周期累计=%s 条数=%d",
                event_id, subscription_id, idx, start.isoformat(), end.isoformat(),
                window.total_quantity, window.record_count,
            )
            return {
                "subscription_id": subscription_id,
                "period_index": idx,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "total_quantity": str(window.total_quantity),
                "record_count": window.record_count,
            }

        return self._run_event(event_id, EventType.USAGE_REPORTED,
                               payload, f"sub={subscription_id}", work)

    def get_window(self, subscription_id: str, period_index: int) -> dict:
        state = self.store.load()
        sub = state.subscriptions.get(subscription_id)
        if sub is None:
            raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND,
                              f"订阅不存在: {subscription_id}",
                              details={"subscription_id": subscription_id})
        start, end = period_for(sub.anchored_at, period_index)
        bucket = state.usage.get((subscription_id, period_index), [])
        window = aggregate_window(subscription_id, period_index,
                                  sub.anchored_at, bucket)
        return {
            "subscription_id": subscription_id,
            "period_index": period_index,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "total_quantity": str(window.total_quantity),
            "record_count": window.record_count,
            "records": [{
                "quantity": str(r.quantity),
                "occurred_at": r.occurred_at.isoformat(),
                "metric": r.metric,
            } for r in window.records],
        }

    # ------------------------------------------------------------ 账单

    def generate_invoice(self, event_id: str, subscription_id: str,
                         period_index: int) -> CommandResult:
        payload = {"subscription_id": subscription_id, "period_index": period_index}

        def work(state: StoreState) -> dict:
            sub = state.subscriptions.get(subscription_id)
            if sub is None:
                raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND,
                                  f"订阅不存在: {subscription_id}",
                                  details={"subscription_id": subscription_id})
            if period_index < 0:
                raise DomainError(ErrorCode.INVALID_PERIOD, "账期序号不能为负",
                                  details={"period_index": period_index})
            start, end = period_for(sub.anchored_at, period_index)
            existing = state.invoices.get((subscription_id, period_index))
            if existing is not None and existing.status == InvoiceStatus.SETTLED:
                raise DomainError(
                    ErrorCode.INVOICE_ALREADY_SETTLED,
                    f"第 {period_index} 期账单已结算，内容不可变更",
                    details={"invoice_id": existing.id},
                )
            records = state.usage.get((subscription_id, period_index), [])
            invoice = compute_invoice(
                invoice_id=_invoice_id(subscription_id, period_index),
                subscription=sub, plans=state.plans,
                period_start=start, period_end=end, records=records,
                created_at=self._now(),
                currency=(state.plans.get(sub.plan_id) or _any_plan(state, sub)).currency,
            )
            state.invoices[(subscription_id, period_index)] = invoice
            logger.info(
                "账单生成 event_id=%s 账期=sub=%s period=%d [%s,%s) 行数=%d 总额=%s 校验=%s",
                event_id, subscription_id, period_index,
                start.isoformat(), end.isoformat(),
                len(invoice.line_items), invoice.total_amount,
                invoice.content_checksum[:12],
            )
            return {"invoice": _invoice_to_dict(invoice)}

        return self._run_event(event_id, EventType.INVOICE_GENERATED,
                               payload, f"sub={subscription_id} period={period_index}",
                               work)

    def settle(self, event_id: str, subscription_id: str, period_index: int,
               *, decline_once: bool = False) -> CommandResult:
        # 注意：decline_once 是“模拟渠道首次失败”的执行选项，不属于事件语义，
        # 不纳入载荷指纹——同一结算意图无论是否带该选项，指纹都一致。
        payload = {"subscription_id": subscription_id, "period_index": period_index}

        def work(state: StoreState) -> dict:
            sub = state.subscriptions.get(subscription_id)
            if sub is None:
                raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND,
                                  f"订阅不存在: {subscription_id}",
                                  details={"subscription_id": subscription_id})
            invoice = state.invoices.get((subscription_id, period_index))
            if invoice is None:
                raise DomainError(ErrorCode.INVOICE_NOT_FOUND,
                                  f"第 {period_index} 期账单不存在，请先生成",
                                  details={"subscription_id": subscription_id,
                                           "period_index": period_index})
            if invoice.status == InvoiceStatus.SETTLED:
                raise DomainError(ErrorCode.INVOICE_ALREADY_SETTLED,
                                  f"账单 {invoice.id} 已结算，禁止重复结算",
                                  details={"invoice_id": invoice.id,
                                           "payment_txn_id": invoice.payment_txn_id})

            updated = settle_invoice(invoice, self.gateway, self._now(),
                                     decline_once=decline_once)
            if updated.status != InvoiceStatus.SETTLED:
                state.invoices[(subscription_id, period_index)] = updated
                raise DomainError(
                    ErrorCode.PAYMENT_DECLINED,
                    f"账单 {invoice.id} 结算被渠道拒绝，可重试且不会重复扣款",
                    details={"invoice_id": invoice.id,
                             "amount": str(invoice.total_amount)},
                )

            state.invoices[(subscription_id, period_index)] = updated
            logger.info(
                "账单结算成功 event_id=%s 账期=sub=%s period=%d invoice=%s 金额=%s 交易=%s",
                event_id, subscription_id, period_index, updated.id,
                updated.total_amount, updated.payment_txn_id,
            )
            return {"invoice": _invoice_to_dict(updated)}

        return self._run_event(event_id, EventType.INVOICE_SETTLED,
                               payload, f"sub={subscription_id} period={period_index}",
                               work)

    # ------------------------------------------------------------ 查询/重放

    def get_invoice(self, subscription_id: str, period_index: int):
        state = self.store.load()
        invoice = state.invoices.get((subscription_id, period_index))
        if invoice is None:
            raise DomainError(ErrorCode.INVOICE_NOT_FOUND,
                              f"第 {period_index} 期账单不存在",
                              details={"subscription_id": subscription_id,
                                       "period_index": period_index})
        return _invoice_to_dict(invoice)

    def get_plan(self, plan_id: str) -> Plan:
        state = self.store.load()
        plan = state.plans.get(plan_id)
        if plan is None:
            raise DomainError(ErrorCode.PLAN_NOT_FOUND, f"套餐不存在: {plan_id}",
                              details={"plan_id": plan_id})
        return plan

    def get_subscription(self, subscription_id: str) -> Subscription:
        state = self.store.load()
        sub = state.subscriptions.get(subscription_id)
        if sub is None:
            raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND,
                              f"订阅不存在: {subscription_id}",
                              details={"subscription_id": subscription_id})
        return sub

    def replay_all(self) -> dict:
        """重放自检：重新加载快照并交叉校验账单指纹与台账一致性。

        模拟“进程重启后重放事件日志”：当前状态能从持久化快照无损重建，
        且账单内容指纹不变、台账事件完备，则账目与只处理一次完全一致。
        """
        with self.store.lock():
            state = self.store.load()
            checks: dict[str, object] = {
                "ledger_events": len(state.ledger),
                "applied": sum(1 for e in state.ledger.values()
                               if e.outcome == EventOutcome.APPLIED),
                "rejected": sum(1 for e in state.ledger.values()
                                if e.outcome == EventOutcome.REJECTED),
                "invoices_checked": 0,
                "usage_windows": len(state.usage),
            }
            mismatches: list[dict] = []
            for (sid, idx), invoice in state.invoices.items():
                current = invoice.compute_checksum()
                checks["invoices_checked"] = int(checks["invoices_checked"]) + 1
                if invoice.content_checksum != current:
                    mismatches.append({
                        "subscription_id": sid, "period_index": idx,
                        "stored": invoice.content_checksum, "recomputed": current,
                    })
            if len(state.ledger) != len({e.event_id for e in state.ledger.values()}):
                mismatches.append({"reason": "duplicate event_id in ledger"})
            result = {"consistent": not mismatches, "checks": checks,
                      "mismatches": mismatches}
            logger.info("重放自检完成 结果=%s 明细=%s",
                        "consistent" if not mismatches else "mismatch", checks)
            return result

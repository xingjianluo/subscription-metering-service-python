"""应用服务：事件准入校验、幂等判定、冲突检测、事件溯源与重放。

核心约定：
- 所有状态变更都先写入事件流（PENDING），再提交效果（APPLIED）；
  处理中途崩溃会留下 PENDING，recover_pending 按 seq 重做即可收敛。
- event_id 是幂等键：内容相同的重复投递直接返回原结果；
  event_id 相同但 kind/内容不同 => EVENT_CONFLICT，绝不以后来者覆盖。
- 拒绝也会留痕（REJECTED），拒绝原因 code 保存在事件结果里，可区分、可审计。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.billing import Wallet, generate_invoice, settle_invoice
from app.errors import ErrorCode, MeteringError
from app.models import (
    EventKind,
    EventRecord,
    EventStatus,
    Invoice,
    InvoiceLine,
    Plan,
    PlanPhase,
    Subscription,
    UsageRecord,
)
from app.periods import period_bounds, period_index_for
from app.store import Store

logger = logging.getLogger("metering.service")

# 临时性失败：重试可能成功，事件保持 PENDING，不做终态判定
_TRANSIENT_ERRORS = frozenset(
    {ErrorCode.PERIOD_NOT_CLOSED, ErrorCode.INVOICE_SETTLE_FAILED}
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(kind: EventKind, payload: dict) -> str:
    return f"{kind.value}|" + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(kind: EventKind, payload: dict) -> str:
    """事件内容指纹：event_id 相同但 kind/payload 不同 => 明确冲突。"""
    return hashlib.sha256(_canonical(kind, payload).encode("utf-8")).hexdigest()


def parse_ts(value) -> datetime:
    """容忍 Z 结尾的 ISO 时间；无法解析返回 None（由调用方归类错误）。"""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        raise ValueError("empty timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _require(payload: dict, field: str):
    if field not in payload or payload[field] is None or payload[field] == "":
        raise MeteringError(
            ErrorCode.MISSING_FIELD,
            f"required field '{field}' is missing",
            {"field": field},
        )
    return payload[field]


def _require_decimal(payload: dict, field: str) -> Decimal:
    raw = _require(payload, field)
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise MeteringError(
            ErrorCode.INVALID_FIELD,
            f"field '{field}' must be a number, got {raw!r}",
            {"field": field, "value": raw},
        )


def _require_ts(payload: dict, field: str) -> datetime:
    raw = _require(payload, field)
    try:
        return parse_ts(raw)
    except (ValueError, TypeError):
        raise MeteringError(
            ErrorCode.INVALID_FIELD,
            f"field '{field}' must be ISO-8601 datetime, got {raw!r}",
            {"field": field, "value": raw},
        )


# ----------------------------- 序列化 -----------------------------

def plan_dict(plan: Plan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "name": plan.name,
        "base_price": str(plan.base_price),
        "included_quantity": str(plan.included_quantity),
        "overage_unit_price": str(plan.overage_unit_price),
        "currency": plan.currency,
    }


def subscription_dict(sub: Subscription) -> dict:
    return {
        "subscription_id": sub.subscription_id,
        "customer_id": sub.customer_id,
        "start_at": sub.start_at.isoformat(),
        "phases": [
            {"plan_id": p.plan_id, "effective_from": p.effective_from.isoformat()}
            for p in sub.phases
        ],
        "canceled_at": sub.canceled_at.isoformat() if sub.canceled_at else None,
    }


def invoice_dict(inv: Invoice) -> dict:
    def line_dict(ln: InvoiceLine) -> dict:
        return {
            "line_type": ln.line_type.value,
            "plan_id": ln.plan_id,
            "description": ln.description,
            "amount": str(ln.amount),
            "basis": ln.basis,
        }

    return {
        "invoice_id": inv.invoice_id,
        "subscription_id": inv.subscription_id,
        "period_index": inv.period_index,
        "period_start": inv.period_start.isoformat(),
        "period_end": inv.period_end.isoformat(),
        "currency": inv.currency,
        "lines": [line_dict(ln) for ln in inv.lines],
        "total_amount": str(inv.total_amount),
        "status": inv.status.value,
        "settlement_request_id": inv.settlement_request_id,
        "settled_at": inv.settled_at.isoformat() if inv.settled_at else None,
    }


def event_dict(ev: EventRecord) -> dict:
    return {
        "event_id": ev.event_id,
        "kind": ev.kind.value,
        "seq": ev.seq,
        "status": ev.status.value,
        "payload_hash": ev.payload_hash,
        "payload": ev.payload,
        "result": ev.result,
        "received_at": ev.received_at.isoformat() if ev.received_at else None,
        "applied_at": ev.applied_at.isoformat() if ev.applied_at else None,
    }


class _NullWallet(Wallet):
    """重放用钱包：不触碰真实余额；结算台账标记 recovered=True。"""

    def __init__(self) -> None:  # noqa: D401
        super().__init__(Decimal("0"))

    def pay(self, amount: Decimal, idempotency_key: str) -> bool:
        return True


class MeteringService:
    def __init__(self, store: Store, wallet: Wallet | None = None) -> None:
        self.store = store
        self.wallet = wallet or Wallet(Decimal("999999"))

    # ========================= 命令 API =========================

    def define_plan(self, payload: dict) -> dict:
        return self._submit(EventKind.PLAN_DEFINED, dict(payload))

    def create_subscription(self, payload: dict) -> dict:
        return self._submit(EventKind.SUBSCRIPTION_CREATED, dict(payload))

    def report_usage(self, payload: dict) -> dict:
        return self._submit(EventKind.USAGE_REPORTED, dict(payload))

    def change_plan(self, payload: dict) -> dict:
        return self._submit(EventKind.PLAN_CHANGED, dict(payload))

    def cancel_subscription(self, payload: dict) -> dict:
        return self._submit(EventKind.SUBSCRIPTION_CANCELED, dict(payload))

    def invoice_period(self, payload: dict) -> dict:
        return self._submit(EventKind.INVOICE_GENERATED, dict(payload))

    def settle(self, payload: dict) -> dict:
        return self._submit(EventKind.INVOICE_SETTLED, dict(payload))

    # ========================= 事件核心 =========================

    def _submit(self, kind: EventKind, payload: dict) -> dict:
        """登记 + 处理事件。同一 event_id 并发投递由 store 锁串行化，只生效一次。"""
        event_id = payload.get("event_id")
        if not event_id:
            raise MeteringError(
                ErrorCode.MISSING_FIELD,
                "required field 'event_id' is missing",
                {"field": "event_id"},
            )

        with self.store.lock():
            digest = payload_hash(kind, payload)
            existing = self.store.get_event(event_id)
            if existing is not None:
                if existing.kind != kind or existing.payload_hash != digest:
                    raise MeteringError(
                        ErrorCode.EVENT_CONFLICT,
                        f"event_id {event_id!r} already used with different content",
                        {
                            "event_id": event_id,
                            "existing_kind": existing.kind.value,
                            "incoming_kind": kind.value,
                        },
                    )
                if existing.status is EventStatus.APPLIED:
                    logger.info(
                        "event=%s kind=%s decision=duplicate_return_cached",
                        event_id, kind.value,
                    )
                    return dict(existing.result or {})
                if existing.status is EventStatus.REJECTED:
                    err = existing.result or {}
                    logger.info(
                        "event=%s kind=%s decision=duplicate_rejected code=%s",
                        event_id, kind.value, err.get("error_code"),
                    )
                    raise MeteringError(
                        ErrorCode(err["error_code"]),
                        err.get("message", "rejected"),
                        err.get("details", {}),
                    )
                # PENDING：可能是并发（锁保护下不会出现）或崩溃残留 -> 继续重做
                event = existing
                logger.info(
                    "event=%s kind=%s decision=pending_reprocess seq=%s",
                    event_id, kind.value, event.seq,
                )
            else:
                event = EventRecord(
                    event_id=event_id,
                    kind=kind,
                    payload=payload,
                    payload_hash=digest,
                    seq=self.store.next_seq(),
                    received_at=utcnow(),
                )
                self.store.append_event(event)
                logger.info(
                    "event=%s kind=%s seq=%s decision=ingested",
                    event_id, kind.value, event.seq,
                )

            # 测试崩溃钩子：事件已落 PENDING，效果未提交
            if event_id in self.store.crash_before_apply:
                self.store.crash_before_apply.discard(event_id)
                logger.warning(
                    "event=%s kind=%s decision=simulated_crash_before_apply",
                    event_id, kind.value,
                )
                raise RuntimeError(f"simulated crash while applying {event_id}")

            try:
                result = self._apply(event)
            except MeteringError as exc:
                if exc.code in _TRANSIENT_ERRORS:
                    logger.info(
                        "event=%s kind=%s period=%s decision=transient_pending code=%s",
                        event_id, kind.value, exc.details.get("period_index", "-"),
                        exc.code.value,
                    )
                    raise
                self.store.update_event_status(
                    event_id,
                    EventStatus.REJECTED,
                    {
                        "error_code": exc.code.value,
                        "message": exc.message,
                        "details": exc.details,
                    },
                )
                logger.info(
                    "event=%s kind=%s period=%s decision=rejected code=%s reason=%s",
                    event_id, kind.value, exc.details.get("period_index", "-"),
                    exc.code.value, exc.message,
                )
                raise

            self.store.update_event_status(event_id, EventStatus.APPLIED, result)
            event.applied_at = utcnow()
            logger.info(
                "event=%s kind=%s period=%s decision=applied",
                event_id, kind.value, result.get("period_index", "-"),
            )
            return result

    def _apply(self, event: EventRecord, *, recovered: bool = False) -> dict:
        """提交事件效果。处理程序必须对同一事件重复执行保持幂等。"""
        kind = event.kind
        p = event.payload
        if kind is EventKind.PLAN_DEFINED:
            return self._apply_define_plan(p)
        if kind is EventKind.SUBSCRIPTION_CREATED:
            return self._apply_create_subscription(p)
        if kind is EventKind.USAGE_REPORTED:
            return self._apply_report_usage(event.event_id, p)
        if kind is EventKind.PLAN_CHANGED:
            return self._apply_change_plan(p)
        if kind is EventKind.SUBSCRIPTION_CANCELED:
            return self._apply_cancel(p)
        if kind is EventKind.INVOICE_GENERATED:
            return self._apply_invoice(p)
        if kind is EventKind.INVOICE_SETTLED:
            return self._apply_settle(p, event.received_at, recovered=recovered)
        raise MeteringError(
            ErrorCode.INVALID_STATE, f"unknown event kind {kind}", {"kind": str(kind)}
        )

    # ------------------------- 效果处理程序 -------------------------

    def _apply_define_plan(self, p: dict) -> dict:
        plan_id = _require(p, "plan_id")
        name = _require(p, "name")
        base_price = _require_decimal(p, "base_price")
        included = _require_decimal(p, "included_quantity")
        overage_price = _require_decimal(p, "overage_unit_price")
        if base_price < 0 or included < 0 or overage_price < 0:
            raise MeteringError(
                ErrorCode.INVALID_FIELD,
                "price and included_quantity must be non-negative",
                {"plan_id": plan_id},
            )
        currency = p.get("currency", "CNY")
        plan = Plan(str(plan_id), str(name), base_price, included, overage_price, str(currency))
        self.store.put_plan(plan)
        return plan_dict(plan)

    def _apply_create_subscription(self, p: dict) -> dict:
        sub_id = _require(p, "subscription_id")
        customer_id = _require(p, "customer_id")
        start_at = _require_ts(p, "start_at")
        plan_id = _require(p, "plan_id")
        if self.store.get_plan(plan_id) is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_PLAN, f"plan {plan_id} not found", {"plan_id": plan_id}
            )
        existing = self.store.find_subscription(sub_id)
        if existing is not None:
            raise MeteringError(
                ErrorCode.INVALID_STATE,
                f"subscription {sub_id} already exists",
                {"subscription_id": sub_id},
            )
        sub = Subscription(
            subscription_id=str(sub_id),
            customer_id=str(customer_id),
            start_at=start_at,
            phases=[PlanPhase(str(plan_id), start_at)],
        )
        self.store.put_subscription(sub)
        return subscription_dict(sub)

    def _apply_report_usage(self, event_id: str, p: dict) -> dict:
        sub_id = str(_require(p, "subscription_id"))
        ts = _require_ts(p, "ts")
        quantity = _require_decimal(p, "quantity")
        if quantity < 0:
            raise MeteringError(
                ErrorCode.NEGATIVE_QUANTITY,
                f"usage quantity must be >= 0, got {quantity}",
                {"subscription_id": sub_id, "event_id": event_id, "quantity": str(quantity)},
            )
        sub = self.store.find_subscription(sub_id)
        if sub is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_SUBSCRIPTION,
                f"subscription {sub_id} not found",
                {"subscription_id": sub_id},
            )
        period_index = period_index_for(sub.start_at, ts)
        period_start, period_end = period_bounds(sub.start_at, max(period_index, 0))
        if period_index < 0:
            raise MeteringError(
                ErrorCode.USAGE_OUT_OF_RANGE,
                f"usage ts {ts.isoformat()} is before subscription start {sub.start_at.isoformat()}",
                {
                    "subscription_id": sub_id,
                    "event_id": event_id,
                    "ts": ts.isoformat(),
                    "subscription_start": sub.start_at.isoformat(),
                    "period_index": -1,
                },
            )
        if sub.canceled_at is not None and ts >= sub.canceled_at:
            raise MeteringError(
                ErrorCode.USAGE_OUT_OF_RANGE,
                f"usage ts {ts.isoformat()} is at/after cancellation {sub.canceled_at.isoformat()}",
                {
                    "subscription_id": sub_id,
                    "event_id": event_id,
                    "ts": ts.isoformat(),
                    "canceled_at": sub.canceled_at.isoformat(),
                    "period_index": period_index,
                },
            )
        self.store.add_usage(
            sub_id,
            period_index,
            UsageRecord(event_id=event_id, subscription_id=sub_id, ts=ts, quantity=quantity),
        )
        logger.info(
            "event=%s usage bucketed period=%s window=[%s,%s) qty=%s",
            event_id, period_index, period_start.isoformat(), period_end.isoformat(), quantity,
        )
        return {
            "subscription_id": sub_id,
            "event_id": event_id,
            "period_index": period_index,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "quantity": str(quantity),
        }

    def _apply_change_plan(self, p: dict) -> dict:
        sub_id = str(_require(p, "subscription_id"))
        plan_id = str(_require(p, "plan_id"))
        effective_from = _require_ts(p, "effective_from")
        sub = self.store.find_subscription(sub_id)
        if sub is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_SUBSCRIPTION,
                f"subscription {sub_id} not found",
                {"subscription_id": sub_id},
            )
        if self.store.get_plan(plan_id) is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_PLAN, f"plan {plan_id} not found", {"plan_id": plan_id}
            )
        if effective_from < sub.start_at:
            raise MeteringError(
                ErrorCode.PLAN_CHANGE_OUT_OF_RANGE,
                "plan change effective time is before subscription start",
                {
                    "subscription_id": sub_id,
                    "effective_from": effective_from.isoformat(),
                    "subscription_start": sub.start_at.isoformat(),
                },
            )
        if sub.canceled_at is not None and effective_from > sub.canceled_at:
            raise MeteringError(
                ErrorCode.PLAN_CHANGE_OUT_OF_RANGE,
                "plan change effective time is at/after cancellation",
                {
                    "subscription_id": sub_id,
                    "effective_from": effective_from.isoformat(),
                    "canceled_at": sub.canceled_at.isoformat(),
                },
            )
        sub.phases.append(PlanPhase(plan_id, effective_from))
        sub.phases.sort(key=lambda ph: ph.effective_from)
        return subscription_dict(sub)

    def _apply_cancel(self, p: dict) -> dict:
        sub_id = str(_require(p, "subscription_id"))
        canceled_at = _require_ts(p, "canceled_at")
        sub = self.store.find_subscription(sub_id)
        if sub is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_SUBSCRIPTION,
                f"subscription {sub_id} not found",
                {"subscription_id": sub_id},
            )
        if canceled_at < sub.start_at:
            raise MeteringError(
                ErrorCode.CANCEL_OUT_OF_RANGE,
                "cancel time is before subscription start",
                {
                    "subscription_id": sub_id,
                    "canceled_at": canceled_at.isoformat(),
                    "subscription_start": sub.start_at.isoformat(),
                },
            )
        if sub.canceled_at is not None and sub.canceled_at != canceled_at:
            raise MeteringError(
                ErrorCode.INVALID_STATE,
                "subscription already canceled with a different effective time",
                {
                    "subscription_id": sub_id,
                    "existing_canceled_at": sub.canceled_at.isoformat(),
                    "canceled_at": canceled_at.isoformat(),
                },
            )
        sub.canceled_at = canceled_at
        return subscription_dict(sub)

    def _apply_invoice(self, p: dict) -> dict:
        sub_id = str(_require(p, "subscription_id"))
        period_index = int(_require(p, "period_index"))
        as_of = parse_ts(p["as_of"]) if p.get("as_of") else utcnow()
        invoice = generate_invoice(
            self.store, sub_id, period_index, as_of=as_of
        )
        logger.info(
            "invoice=%s subscription=%s period=%s lines=%d total=%s",
            invoice.invoice_id, sub_id, period_index,
            len(invoice.lines), invoice.total_amount,
        )
        return invoice_dict(invoice)

    def _apply_settle(self, p: dict, default_at: datetime | None, *, recovered: bool = False) -> dict:
        invoice_id = str(_require(p, "invoice_id"))
        request_id = str(_require(p, "request_id"))
        at = parse_ts(p["at"]) if p.get("at") else (default_at or utcnow())
        wallet = _NullWallet() if recovered else self.wallet
        invoice = settle_invoice(
            self.store, invoice_id, request_id, wallet, at=at, recovered=recovered
        )
        logger.info(
            "invoice=%s settle request=%s status=%s recovered=%s",
            invoice_id, request_id, invoice.status.value, recovered,
        )
        result = invoice_dict(invoice)
        result["recovered"] = recovered
        return result

    # ========================= 恢复与重放 =========================

    def recover_pending(self) -> int:
        """按 seq 重做所有 PENDING 事件（崩溃自愈）。返回修复条数。"""
        fixed = 0
        with self.store.lock():
            for event in self.store.list_events():
                if event.status is not EventStatus.PENDING:
                    continue
                try:
                    result = self._apply(event)
                    self.store.update_event_status(event.event_id, EventStatus.APPLIED, result)
                    event.applied_at = utcnow()
                    fixed += 1
                    logger.info(
                        "recover event=%s kind=%s decision=applied seq=%s",
                        event.event_id, event.kind.value, event.seq,
                    )
                except MeteringError as exc:
                    if exc.code in _TRANSIENT_ERRORS:
                        logger.info(
                            "recover event=%s kind=%s decision=still_pending code=%s",
                            event.event_id, event.kind.value, exc.code.value,
                        )
                        continue
                    self.store.update_event_status(
                        event.event_id,
                        EventStatus.REJECTED,
                        {
                            "error_code": exc.code.value,
                            "message": exc.message,
                            "details": exc.details,
                        },
                    )
                    fixed += 1
                    logger.info(
                        "recover event=%s kind=%s decision=rejected code=%s",
                        event.event_id, event.kind.value, exc.code.value,
                    )
        return fixed

    def replay(self) -> dict:
        """以事件流重建全部投影状态，结论与逐条处理一致。

        - APPLIED/PENDING 事件按 seq 重放效果；结算走空钱包且台账记 recovered；
        - REJECTED 事件跳过效果，保留拒绝结论；
        - 账单 ID 由 (订阅, 账期) 确定性生成，重放后 ID/金额完全一致。
        """
        events = self.store.list_events()
        self.store.reset(keep_seq=True)
        applied = rejected = pending = 0
        for event in events:
            digest = payload_hash(event.kind, event.payload)
            rebuilt = EventRecord(
                event_id=event.event_id,
                kind=event.kind,
                payload=event.payload,
                payload_hash=digest,
                seq=event.seq,
                received_at=event.received_at,
                applied_at=event.applied_at,
            )
            if event.status is EventStatus.REJECTED:
                # 终态拒绝：跳过效果，保留拒绝结论
                rebuilt.status = EventStatus.REJECTED
                rebuilt.result = event.result
                rejected += 1
            elif event.status is EventStatus.PENDING:
                # 未完成（崩溃/临时性失败）：先重建为 PENDING，稍后用真实状态重做
                rebuilt.status = EventStatus.PENDING
                pending += 1
            else:
                is_settle = event.kind is EventKind.INVOICE_SETTLED
                result = self._apply(rebuilt, recovered=is_settle)
                rebuilt.status = EventStatus.APPLIED
                rebuilt.result = result or event.result
                applied += 1
            self.store.append_event(rebuilt)
        # 重建后对未完成事件做一次崩溃自愈（使用真实钱包，临时性失败仍保持 PENDING）
        fixed = self.recover_pending()
        logger.info(
            "replay done applied=%d rejected=%d pending=%d recovered=%d",
            applied, rejected, pending, fixed,
        )
        return {
            "events": len(events),
            "applied": applied,
            "rejected": rejected,
            "pending_recovered": fixed,
        }

    # ========================= 查询 =========================

    def get_subscription(self, subscription_id: str) -> dict:
        sub = self.store.find_subscription(subscription_id)
        if sub is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_SUBSCRIPTION,
                f"subscription {subscription_id} not found",
                {"subscription_id": subscription_id},
            )
        return subscription_dict(sub)

    def get_invoice(self, invoice_id: str) -> dict:
        inv = self.store.find_invoice(invoice_id)
        if inv is None:
            raise MeteringError(
                ErrorCode.UNKNOWN_INVOICE,
                f"invoice {invoice_id} not found",
                {"invoice_id": invoice_id},
            )
        return invoice_dict(inv)

    def list_events(self) -> list[dict]:
        return [event_dict(ev) for ev in self.store.list_events()]

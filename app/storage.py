"""本地存储：内存实现 + 原子写 JSON 文件实现（模拟持久化）。

所有状态放在单个 StoreState 快照里，保存时先写临时文件再 os.replace，
保证“处理到一半中断”要么看到旧快照、要么看到新快照，不会出现半成品。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.models import (
    EventOutcome,
    EventType,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LineItem,
    LineItemKind,
    Plan,
    PlanAssignment,
    Subscription,
    SubscriptionStatus,
    UsageRecord,
)


@dataclass
class StoreState:
    plans: dict[str, Plan] = field(default_factory=dict)
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    usage: dict[tuple[str, int], list[UsageRecord]] = field(default_factory=dict)
    invoices: dict[tuple[str, int], Invoice] = field(default_factory=dict)
    ledger: dict[str, LedgerEntry] = field(default_factory=dict)


class Store:
    def load(self) -> StoreState:
        raise NotImplementedError

    def save(self, state: StoreState) -> None:
        raise NotImplementedError

    def lock(self):
        raise NotImplementedError


class InMemoryStore(Store):
    def __init__(self) -> None:
        self._state = StoreState()
        self._lock = threading.RLock()

    def load(self) -> StoreState:
        return self._state

    def save(self, state: StoreState) -> None:
        # 内存实现直接持有同一对象；调用方已在锁内完成全部修改
        self._state = state

    def lock(self):
        return self._lock


class JsonFileStore(Store):
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> StoreState:
        if not self.path.exists():
            return StoreState()
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return state_from_dict(data)

    def save(self, state: StoreState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state_to_dict(state), ensure_ascii=False, indent=2)
        # 同目录临时文件 + 原子替换，杜绝半截文件
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def lock(self):
        return self._lock


# ---------------------------------------------------------------- 序列化


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _parse_decimal(value) -> Decimal:
    return Decimal(str(value))


def state_to_dict(state: StoreState) -> dict:
    plans = {pid: {
        "id": p.id, "name": p.name, "base_price": str(p.base_price),
        "included_quantity": str(p.included_quantity),
        "overage_unit_price": str(p.overage_unit_price), "currency": p.currency,
    } for pid, p in state.plans.items()}

    subs = {}
    for sid, sub in state.subscriptions.items():
        subs[sid] = {
            "id": sub.id, "plan_id": sub.plan_id, "status": sub.status.value,
            "anchored_at": sub.anchored_at.isoformat(),
            "cancelled_at": sub.cancelled_at.isoformat() if sub.cancelled_at else None,
            "plan_history": [{
                "plan_id": a.plan_id,
                "effective_from": a.effective_from.isoformat(),
                "effective_to": a.effective_to.isoformat() if a.effective_to else None,
            } for a in sub.plan_history],
        }

    usage = {
        f"{sid}|{idx}": [{
            "subscription_id": r.subscription_id,
            "quantity": str(r.quantity),
            "occurred_at": r.occurred_at.isoformat(),
            "metric": r.metric,
        } for r in records]
        for (sid, idx), records in state.usage.items()
    }

    invoices = {
        f"{sid}|{idx}": _invoice_to_dict(inv)
        for (sid, idx), inv in state.invoices.items()
    }

    ledger = {eid: {
        "event_id": e.event_id,
        "event_type": e.event_type.value,
        "payload_hash": e.payload_hash,
        "outcome": e.outcome.value,
        "result": e.result,
        "error_code": e.error_code,
        "error_message": e.error_message,
        "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
    } for eid, e in state.ledger.items()}

    return {"plans": plans, "subscriptions": subs, "usage": usage,
            "invoices": invoices, "ledger": ledger}


def _invoice_to_dict(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "subscription_id": inv.subscription_id,
        "period_start": inv.period_start.isoformat(),
        "period_end": inv.period_end.isoformat(),
        "line_items": [{
            "kind": it.kind.value, "plan_id": it.plan_id,
            "description": it.description, "quantity": str(it.quantity),
            "unit_price": str(it.unit_price), "amount": str(it.amount),
            "period_start": it.period_start.isoformat(),
            "period_end": it.period_end.isoformat(),
        } for it in inv.line_items],
        "status": inv.status.value,
        "total_amount": str(inv.total_amount),
        "currency": inv.currency,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "settled_at": inv.settled_at.isoformat() if inv.settled_at else None,
        "payment_txn_id": inv.payment_txn_id,
        "content_checksum": inv.content_checksum,
    }


def state_from_dict(data: dict) -> StoreState:
    state = StoreState()
    for pid, p in data.get("plans", {}).items():
        state.plans[pid] = Plan(
            id=p["id"], name=p["name"], base_price=_parse_decimal(p["base_price"]),
            included_quantity=_parse_decimal(p["included_quantity"]),
            overage_unit_price=_parse_decimal(p["overage_unit_price"]),
            currency=p.get("currency", "CNY"),
        )
    for sid, s in data.get("subscriptions", {}).items():
        state.subscriptions[sid] = Subscription(
            id=s["id"], plan_id=s["plan_id"],
            status=SubscriptionStatus(s["status"]),
            anchored_at=_parse_dt(s["anchored_at"]),
            cancelled_at=_parse_dt(s.get("cancelled_at")),
            plan_history=[PlanAssignment(
                plan_id=a["plan_id"],
                effective_from=_parse_dt(a["effective_from"]),
                effective_to=_parse_dt(a.get("effective_to")),
            ) for a in s.get("plan_history", [])],
        )
    for key, records in data.get("usage", {}).items():
        sid, idx = key.split("|", 1)
        state.usage[(sid, int(idx))] = [UsageRecord(
            subscription_id=r["subscription_id"],
            quantity=_parse_decimal(r["quantity"]),
            occurred_at=_parse_dt(r["occurred_at"]),
            metric=r["metric"],
        ) for r in records]
    for key, raw in data.get("invoices", {}).items():
        sid, idx = key.split("|", 1)
        state.invoices[(sid, int(idx))] = _invoice_from_dict(raw)
    for eid, e in data.get("ledger", {}).items():
        state.ledger[eid] = LedgerEntry(
            event_id=e["event_id"],
            event_type=EventType(e["event_type"]),
            payload_hash=e["payload_hash"],
            outcome=EventOutcome(e["outcome"]),
            result=e.get("result", {}),
            error_code=e.get("error_code"),
            error_message=e.get("error_message"),
            recorded_at=_parse_dt(e.get("recorded_at")),
        )
    return state


def _invoice_from_dict(raw: dict) -> Invoice:
    return Invoice(
        id=raw["id"],
        subscription_id=raw["subscription_id"],
        period_start=_parse_dt(raw["period_start"]),
        period_end=_parse_dt(raw["period_end"]),
        line_items=[LineItem(
            kind=LineItemKind(it["kind"]), plan_id=it["plan_id"],
            description=it["description"],
            quantity=_parse_decimal(it["quantity"]),
            unit_price=_parse_decimal(it["unit_price"]),
            amount=_parse_decimal(it["amount"]),
            period_start=_parse_dt(it["period_start"]),
            period_end=_parse_dt(it["period_end"]),
        ) for it in raw.get("line_items", [])],
        status=InvoiceStatus(raw["status"]),
        total_amount=_parse_decimal(raw["total_amount"]),
        currency=raw.get("currency", "CNY"),
        created_at=_parse_dt(raw.get("created_at")),
        settled_at=_parse_dt(raw.get("settled_at")),
        payment_txn_id=raw.get("payment_txn_id"),
        content_checksum=raw.get("content_checksum", ""),
    )

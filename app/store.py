"""本地存储：内存实现 + 单进程锁。事件流为唯一事实来源（single source of truth）。"""
from __future__ import annotations

import threading
from collections import OrderedDict

from app.models import (
    EventRecord,
    EventStatus,
    Invoice,
    LedgerEntry,
    Plan,
    Subscription,
    UsageRecord,
)


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plans: dict[str, Plan] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._events: OrderedDict[str, EventRecord] = OrderedDict()
        self._usages: dict[tuple[str, int], list[UsageRecord]] = {}
        self._invoices: dict[tuple[str, int], Invoice] = {}
        self._invoice_by_id: dict[str, Invoice] = {}
        self._settlement: dict[str, LedgerEntry] = {}  # invoice_id -> 成功扣款台账
        self._seq: int = 0
        # 测试钩子：标记某事件在“业务效果已算好、提交前”崩溃
        self.crash_before_apply: set[str] = set()
        # 测试钩子：支付成功、台账落盘前崩溃（key 为 invoice_id）
        self.crash_after_payment: set[str] = set()

    # ---- 锁：服务层复合操作期间持有，保证“校验+写入”原子 ----
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 主数据 ----
    def put_plan(self, plan: Plan) -> None:
        with self._lock:
            self._plans[plan.plan_id] = plan

    def get_plan(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def put_subscription(self, sub: Subscription) -> None:
        with self._lock:
            self._subscriptions[sub.subscription_id] = sub

    def get_subscription(self, sub_id: str) -> Subscription:
        return self._subscriptions[sub_id]

    def find_subscription(self, sub_id: str) -> Subscription | None:
        return self._subscriptions.get(sub_id)

    # ---- 事件流 ----
    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def append_event(self, event: EventRecord) -> None:
        with self._lock:
            self._events[event.event_id] = event

    def get_event(self, event_id: str) -> EventRecord | None:
        return self._events.get(event_id)

    def list_events(self) -> list[EventRecord]:
        return list(self._events.values())

    def update_event_status(
        self, event_id: str, status: EventStatus, result: dict | None = None
    ) -> None:
        with self._lock:
            event = self._events[event_id]
            event.status = status
            if result is not None:
                event.result = result

    # ---- 用量（按 订阅+账期 分桶；桶内按 ts 排序）----
    def add_usage(self, sub_id: str, period_index: int, usage: UsageRecord) -> None:
        """写入用量；同一 event_id 已存在则忽略（事件重放天然幂等）。"""
        with self._lock:
            bucket = self._usages.setdefault((sub_id, period_index), [])
            if any(u.event_id == usage.event_id for u in bucket):
                return
            bucket.append(usage)
            bucket.sort(key=lambda u: u.ts)

    def list_usage(self, sub_id: str, period_index: int) -> list[UsageRecord]:
        return list(self._usages.get((sub_id, period_index), []))

    # ---- 账单 ----
    def put_invoice(self, invoice: Invoice) -> None:
        with self._lock:
            self._invoices[(invoice.subscription_id, invoice.period_index)] = invoice
            self._invoice_by_id[invoice.invoice_id] = invoice

    def get_invoice_by_key(self, sub_id: str, period_index: int) -> Invoice | None:
        return self._invoices.get((sub_id, period_index))

    def get_invoice(self, invoice_id: str) -> Invoice:
        return self._invoice_by_id[invoice_id]

    def find_invoice(self, invoice_id: str) -> Invoice | None:
        return self._invoice_by_id.get(invoice_id)

    def list_invoices(self, sub_id: str) -> list[Invoice]:
        return [inv for (sid, _idx), inv in self._invoices.items() if sid == sub_id]

    # ---- 结算台账（只记录成功扣款，因此天然不存在重复扣钱）----
    def record_settlement(self, entry: LedgerEntry) -> None:
        with self._lock:
            self._settlement[entry.invoice_id] = entry

    def get_settlement(self, invoice_id: str) -> LedgerEntry | None:
        return self._settlement.get(invoice_id)

    def reset(self, *, keep_seq: bool = False) -> None:
        """清空全部状态（测试用）。重放场景保留 seq 与崩溃钩子。"""
        with self._lock:
            saved_seq = self._seq if keep_seq else 0
            saved_crash = set(self.crash_before_apply) if keep_seq else set()
            saved_crash_pay = set(self.crash_after_payment) if keep_seq else set()
            self._plans.clear()
            self._subscriptions.clear()
            self._events.clear()
            self._usages.clear()
            self._invoices.clear()
            self._invoice_by_id.clear()
            self._settlement.clear()
            self._seq = saved_seq
            self.crash_before_apply = saved_crash
            self.crash_after_payment = saved_crash_pay

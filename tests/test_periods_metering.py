"""账期窗口与用量聚合：乱序归位、跨周期隔离、拒绝原因可区分。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.clock import FixedClock
from app.errors import DomainError, ErrorCode
from app.metering import aggregate_window
from app.models import Subscription
from app.periods import period_for, period_index_for
from app.services import MeteringService
from app.storage import InMemoryStore

ANCHOR = datetime(2026, 1, 1, 9, 0)


def _service(moment=ANCHOR):
    return MeteringService(store=InMemoryStore(), clock=FixedClock(moment))


def _basic_plan(svc, event_id="p1"):
    r = svc.create_plan(event_id, "基础版", Decimal("100"),
                        Decimal("1000"), Decimal("0.1"))
    return r.data["plan"]["id"]


def _sub(svc, plan_id, event_id="s1"):
    return svc.create_subscription(event_id, plan_id).data["subscription"]["id"]


# ---------------------------------------------------------------- 账期


def test_periods_are_left_closed_right_open_and_monthly():
    s0, e0 = period_for(ANCHOR, 0)
    assert (s0, e0) == (datetime(2026, 1, 1, 9), datetime(2026, 2, 1, 9))
    s1, e1 = period_for(ANCHOR, 1)
    assert (s1, e1) == (datetime(2026, 2, 1, 9), datetime(2026, 3, 1, 9))
    # 边界时刻：2/1 09:00 属于第 1 期，不属于第 0 期
    assert period_index_for(ANCHOR, datetime(2026, 2, 1, 8, 59)) == 0
    assert period_index_for(ANCHOR, datetime(2026, 2, 1, 9, 0)) == 1


def test_month_end_anchor_clamps_to_last_day():
    anchor = datetime(2026, 3, 31, 10)
    s, e = period_for(anchor, 1)
    assert s == datetime(2026, 4, 30, 10)
    assert e == datetime(2026, 5, 31, 10)


# ---------------------------------------------------------------- 聚合/乱序


def test_out_of_order_usage_aggregates_in_correct_window():
    svc = _service()
    pid = _basic_plan(svc)
    sid = _sub(svc, pid)
    # 先投递晚的，再投递早的
    svc.report_usage("u-late", sid, Decimal("700"),
                     datetime(2026, 1, 20, 10), "m")
    svc.report_usage("u-early", sid, Decimal("300"),
                     datetime(2026, 1, 5, 10), "m")
    win = svc.get_window(sid, 0)
    assert win["total_quantity"] == "1000"
    assert win["record_count"] == 2
    # 记录按发生时间有序
    assert win["records"][0]["occurred_at"] == "2026-01-05T10:00:00"
    assert win["records"][1]["occurred_at"] == "2026-01-20T10:00:00"


def test_usage_does_not_leak_across_periods():
    svc = _service()
    pid = _basic_plan(svc)
    sid = _sub(svc, pid)
    svc.report_usage("u0", sid, Decimal("100"),
                     datetime(2026, 1, 15, 9), "m")
    svc.report_usage("u1", sid, Decimal("200"),
                     datetime(2026, 2, 10, 9), "m")
    assert svc.get_window(sid, 0)["total_quantity"] == "100"
    assert svc.get_window(sid, 1)["total_quantity"] == "200"


# ---------------------------------------------------------------- 拒绝原因


def test_negative_quantity_rejected_with_distinct_reason():
    svc = _service()
    pid = _basic_plan(svc)
    sid = _sub(svc, pid)
    with pytest.raises(DomainError) as ei:
        svc.report_usage("neg", sid, Decimal("-1"),
                         datetime(2026, 1, 10), "m")
    assert ei.value.code == ErrorCode.NEGATIVE_QUANTITY


def test_missing_metric_rejected_as_missing_field():
    svc = _service()
    pid = _basic_plan(svc)
    sid = _sub(svc, pid)
    with pytest.raises(DomainError) as ei:
        svc.report_usage("miss", sid, Decimal("1"),
                         datetime(2026, 1, 10), "")
    assert ei.value.code == ErrorCode.MISSING_FIELD


def test_usage_before_subscription_rejected():
    svc = _service()
    pid = _basic_plan(svc)
    sid = _sub(svc, pid)
    with pytest.raises(DomainError) as ei:
        svc.report_usage("before", sid, Decimal("1"),
                         datetime(2025, 12, 31), "m")
    assert ei.value.code == ErrorCode.USAGE_BEFORE_SUBSCRIPTION


def test_usage_after_cancellation_rejected():
    svc = _service()
    pid = _basic_plan(svc)
    sid = _sub(svc, pid)
    svc.cancel_subscription("c1", sid, datetime(2026, 1, 15, 9))
    with pytest.raises(DomainError) as ei:
        svc.report_usage("after", sid, Decimal("1"),
                         datetime(2026, 1, 16, 9), "m")
    assert ei.value.code == ErrorCode.USAGE_AFTER_CANCELLATION


def test_usage_just_at_or_before_cancellation_accepted():
    svc = _service()
    pid = _basic_plan(svc)
    sid = _sub(svc, pid)
    cancel_at = datetime(2026, 1, 15, 9)
    svc.cancel_subscription("c1", sid, cancel_at)
    # 恰在取消时刻仍接受（用量时间 <= 取消时刻）
    r = svc.report_usage("at", sid, Decimal("10"), cancel_at, "m")
    assert r.outcome.value == "applied"

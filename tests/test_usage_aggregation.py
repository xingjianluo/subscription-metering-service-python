"""用量上报：时间窗口聚合、乱序/重复、跨周期隔离、拒绝原因区分。"""
import logging

import pytest

from app.errors import ErrorCode, MeteringError

log = logging.getLogger("metering.tests.usage")


def _report(svc, event_id, ts, qty):
    result = svc.report_usage(
        {
            "event_id": event_id,
            "subscription_id": "s1",
            "ts": ts,
            "quantity": qty,
        }
    )
    log.info(
        "事件=%s 账期=p%s 窗口=[%s,%s) 用量=%s 判定=accepted",
        event_id,
        result["period_index"],
        result["period_start"],
        result["period_end"],
        qty,
    )
    return result


def test_duplicate_and_out_of_order_aggregate_once(service):
    # 乱序到达 + 重复投递：窗口聚合结果必须等于 800+500
    _report(service, "u2", "2026-01-10T00:00:00Z", "500")
    _report(service, "u1", "2026-01-20T00:00:00Z", "800")
    cached = _report(service, "u1", "2026-01-20T00:00:00Z", "800")

    bucket = service.store.list_usage("s1", 0)
    log.info("账期=p0 事件=%s 判定依据=event_id 去重 + 窗口求和", [u.event_id for u in bucket])
    assert len(bucket) == 2
    assert sum((u.quantity for u in bucket), __import__("decimal").Decimal("0")) == 1300
    # 重复事件返回首次结果
    assert cached["period_index"] == 0


def test_cross_period_does_not_mix(service):
    _report(service, "u-a", "2026-01-31T23:59:59Z", "100")   # p0
    _report(service, "u-b", "2026-02-01T00:00:00Z", "200")   # p1（锚点 1 号）
    _report(service, "u-c", "2026-03-01T00:00:00Z", "300")   # p2
    assert len(service.store.list_usage("s1", 0)) == 1
    assert len(service.store.list_usage("s1", 1)) == 1
    assert len(service.store.list_usage("s1", 2)) == 1
    log.info("跨周期判定依据=锚点 2026-01-01 按月切窗，用量按 ts 分桶 p0/p1/p2")


def test_rejection_reasons_are_distinct(service):
    cases = [
        ("u-neg", "2026-01-11T00:00:00Z", "-1", ErrorCode.NEGATIVE_QUANTITY),
        ("u-missing", "2026-01-11T00:00:00Z", None, ErrorCode.MISSING_FIELD),
    ]
    for event_id, ts, qty, expected in cases:
        payload = {"event_id": event_id, "subscription_id": "s1", "ts": ts}
        if qty is not None:
            payload["quantity"] = qty
        with pytest.raises(MeteringError) as ei:
            service.report_usage(payload)
        log.info("事件=%s 判定=rejected 原因=%s", event_id, ei.value.code.value)
        assert ei.value.code is expected

    # 订阅时间之前 => 超范围（跨周期串账防护）
    with pytest.raises(MeteringError) as ei:
        _report(service, "u-before", "2025-12-31T23:59:59Z", "1")
    assert ei.value.code is ErrorCode.USAGE_OUT_OF_RANGE
    log.info("事件=u-before 判定依据=ts 早于订阅起点，period_index=-1，拒绝")

    # 取消之后 => 超范围
    service.cancel_subscription(
        {
            "event_id": "ev-cancel",
            "subscription_id": "s1",
            "canceled_at": "2026-01-15T00:00:00Z",
        }
    )
    with pytest.raises(MeteringError) as ei:
        _report(service, "u-after-cancel", "2026-01-15T00:00:00Z", "1")
    assert ei.value.code is ErrorCode.USAGE_OUT_OF_RANGE
    log.info("事件=u-after-cancel 判定依据=ts >= canceled_at，拒绝")


def test_same_event_id_different_content_is_conflict(service):
    _report(service, "u1", "2026-01-20T00:00:00Z", "800")
    with pytest.raises(MeteringError) as ei:
        _report(service, "u1", "2026-01-20T00:00:00Z", "999")
    assert ei.value.code is ErrorCode.EVENT_CONFLICT
    log.info("事件=u1 判定依据=同 event_id 内容指纹不同 => conflict，不覆盖")

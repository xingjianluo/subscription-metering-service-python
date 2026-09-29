"""事件幂等、冲突判定与重放：重复投递、载荷冲突、回放结论一致。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.clock import FixedClock
from app.errors import DomainError, ErrorCode
from app.models import EventOutcome
from app.services import MeteringService
from app.storage import InMemoryStore, JsonFileStore

ANCHOR = datetime(2026, 1, 1, 9, 0)


def _svc(store=None, moment=ANCHOR):
    return MeteringService(store=store or InMemoryStore(),
                           clock=FixedClock(moment))


def _plan_and_sub(svc):
    pid = svc.create_plan("p1", "基础版", Decimal("100"),
                          Decimal("1000"), Decimal("0.1")).data["plan"]["id"]
    sid = svc.create_subscription("s1", pid).data["subscription"]["id"]
    return pid, sid


def test_duplicate_usage_event_returns_duplicate_and_does_not_double_count():
    svc = _svc()
    _, sid = _plan_and_sub(svc)
    r1 = svc.report_usage("u1", sid, Decimal("500"),
                          datetime(2026, 1, 10, 9), "m")
    r2 = svc.report_usage("u1", sid, Decimal("500"),
                          datetime(2026, 1, 10, 9), "m")
    assert r1.outcome == EventOutcome.APPLIED
    assert r2.outcome == EventOutcome.DUPLICATE
    assert r1.data == r2.data
    assert svc.get_window(sid, 0)["total_quantity"] == "500"


def test_two_distinct_events_same_quantity_are_two_usages():
    svc = _svc()
    _, sid = _plan_and_sub(svc)
    svc.report_usage("u1", sid, Decimal("500"), datetime(2026, 1, 10, 9), "m")
    svc.report_usage("u2", sid, Decimal("500"), datetime(2026, 1, 11, 9), "m")
    assert svc.get_window(sid, 0)["total_quantity"] == "1000"


def test_same_event_id_different_payload_is_conflict_not_overwrite():
    svc = _svc()
    _, sid = _plan_and_sub(svc)
    svc.report_usage("u1", sid, Decimal("500"), datetime(2026, 1, 10, 9), "m")
    with pytest.raises(DomainError) as ei:
        # 后来的事件试图用同一 event_id 报不同的量/时间——必须被明确拒绝
        svc.report_usage("u1", sid, Decimal("999"),
                         datetime(2026, 1, 12, 9), "m")
    assert ei.value.code == ErrorCode.EVENT_PROCESSED_CONFLICT
    assert "existing_hash" in ei.value.details
    # 原始账目未被覆盖
    assert svc.get_window(sid, 0)["total_quantity"] == "500"


def test_same_event_id_different_event_type_is_conflict():
    svc = _svc()
    pid, sid = _plan_and_sub(svc)
    # 复用已存在的事件 id 去做另一种操作
    with pytest.raises(DomainError) as ei:
        svc.cancel_subscription("p1", sid)  # p1 是 plan_created
    assert ei.value.code == ErrorCode.EVENT_PROCESSED_CONFLICT


def test_rejected_event_is_idempotent_and_replays_same_reason():
    svc = _svc()
    _, sid = _plan_and_sub(svc)
    for _ in range(2):
        with pytest.raises(DomainError) as ei:
            svc.report_usage("neg", sid, Decimal("-1"),
                             datetime(2026, 1, 10, 9), "m")
        assert ei.value.code == ErrorCode.NEGATIVE_QUANTITY
    # 拒绝事件也进台账，但用量窗口不受影响
    report = svc.replay_all()
    assert report["checks"]["rejected"] == 1
    assert svc.get_window(sid, 0)["record_count"] == 0


def test_replay_after_restart_from_json_matches_single_processing(tmp_path):
    path = tmp_path / "state.json"
    svc = _svc(JsonFileStore(path))
    _, sid = _plan_and_sub(svc)
    svc.report_usage("u1", sid, Decimal("500"), datetime(2026, 1, 10, 9), "m")
    svc.report_usage("u2", sid, Decimal("700"), datetime(2026, 1, 20, 9), "m")
    svc.generate_invoice("i1", sid, 0)
    svc.settle("pay1", sid, 0)

    # 重启：用一个全新的服务实例从同一文件加载
    svc2 = _svc(JsonFileStore(path))
    win = svc2.get_window(sid, 0)
    assert win["total_quantity"] == "1200"
    inv = svc2.get_invoice(sid, 0)
    assert inv["status"] == "settled"
    assert inv["total_amount"] == "120.00"
    assert svc2.replay_all()["consistent"] is True

    # 重启后重放结算事件：duplicate，交易号不变
    replay = svc2.settle("pay1", sid, 0)
    assert replay.outcome == EventOutcome.DUPLICATE
    assert replay.data["invoice"]["payment_txn_id"] == inv["payment_txn_id"]

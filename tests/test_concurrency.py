"""并发场景：同一事件并发投递、并发结算，最终结论等同于只处理一次。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal

from app.clock import FixedClock
from app.models import EventOutcome
from app.services import MeteringService
from app.storage import InMemoryStore
from app.errors import DomainError

ANCHOR = datetime(2026, 1, 1, 9, 0)


def _ready_service(store=None):
    svc = MeteringService(store=store or InMemoryStore(),
                          clock=FixedClock(ANCHOR))
    pid = svc.create_plan("p1", "基础版", Decimal("100"),
                          Decimal("1000"), Decimal("0.1")).data["plan"]["id"]
    sid = svc.create_subscription("s1", pid).data["subscription"]["id"]
    return svc, sid


def test_concurrent_duplicate_usage_applied_once():
    svc, sid = _ready_service()

    def send(_):
        return svc.report_usage("u1", sid, Decimal("500"),
                                datetime(2026, 1, 10, 9), "m")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(send, range(32)))

    applied = sum(1 for r in results if r.outcome == EventOutcome.APPLIED)
    duplicate = sum(1 for r in results if r.outcome == EventOutcome.DUPLICATE)
    assert applied == 1
    assert duplicate == 31
    # 无论并发多少份，用量只累计一次
    assert svc.get_window(sid, 0)["total_quantity"] == "500"


def test_concurrent_distinct_usage_events_all_apply():
    svc, sid = _ready_service()

    def send(i):
        return svc.report_usage(f"u{i}", sid, Decimal("1"),
                                datetime(2026, 1, 10, 9) + __import__("datetime").timedelta(minutes=i),
                                "m")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(send, range(20)))
    assert all(r.outcome == EventOutcome.APPLIED for r in results)
    assert svc.get_window(sid, 0)["total_quantity"] == "20"


def test_concurrent_settlement_charges_exactly_once():
    store = InMemoryStore()
    svc, sid = _ready_service(store)
    svc.report_usage("u1", sid, Decimal("1200"), datetime(2026, 1, 10, 9), "m")
    svc.generate_invoice("i1", sid, 0)

    def settle(_):
        try:
            return ("ok", svc.settle("pay1", sid, 0))
        except DomainError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(settle, range(16)))

    ok = [o for o in outcomes if o[0] == "ok"]
    applied = sum(1 for _, r in ok if r.outcome == EventOutcome.APPLIED)
    duplicate = sum(1 for _, r in ok if r.outcome == EventOutcome.DUPLICATE)
    assert applied == 1
    assert duplicate == 15
    # 网关侧只存在一笔成功扣款
    assert len(svc.gateway._captured) == 1
    assert svc.get_invoice(sid, 0)["status"] == "settled"


def test_concurrent_conflicting_events_one_wins_rest_rejected():
    svc, sid = _ready_service()

    def send(payload):
        try:
            svc.report_usage("same-id", sid, Decimal(str(payload)),
                             datetime(2026, 1, 10, 9), "m")
            return "applied"
        except DomainError as exc:
            return exc.code.value

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(send, [10, 20, 30, 40]))

    # 恰好一个生效；其余要么冲突（若首个已落盘），这里最终累计只能等于生效那笔
    assert outcomes.count("applied") == 1
    total = Decimal(svc.get_window(sid, 0)["total_quantity"])
    assert total in (Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40"))

"""并发重复投递、处理中断后恢复、事件重放：最终结论与只处理一次一致。"""
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.errors import MeteringError

log = logging.getLogger("metering.tests.replay")

PAYLOAD = {
    "event_id": "u-conc",
    "subscription_id": "s1",
    "ts": "2026-01-12T00:00:00Z",
    "quantity": "700",
}


def test_concurrent_duplicate_event_applied_once(service):
    results = []

    def fire(_):
        try:
            results.append(("ok", service.report_usage(dict(PAYLOAD))))
        except MeteringError as exc:
            results.append(("conflict", exc))

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(fire, range(10)))

    bucket = service.store.list_usage("s1", 0)
    # 10 次并发重复投递：每个调用都拿到同一结果（首个生效，其余返回缓存），
    # 账目效果只有 1 条
    assert len(bucket) == 1
    assert bucket[0].quantity == Decimal("700")
    assert all(kind == "ok" for kind, _ in results)
    assert len({r["event_id"] for _, r in results}) == 1
    log.info("并发 10 次重复投递：返回=%s 账期=p0 实际用量条数=%s 判定依据=store 锁串行化 + event_id 去重",
             len(results), len(bucket))


def test_crash_midway_then_recover(service):
    # 事件先落 PENDING，提交效果前崩溃
    service.store.crash_before_apply.add("u-crash")
    try:
        service.report_usage(
            {
                "event_id": "u-crash",
                "subscription_id": "s1",
                "ts": "2026-01-25T00:00:00Z",
                "quantity": "100",
            }
        )
        assert False, "应当模拟崩溃"
    except RuntimeError:
        pass

    assert service.store.get_event("u-crash").status.value == "pending"
    assert service.store.list_usage("s1", 0) == []
    log.info("事件=u-crash 账期=p0 判定=interrupted（事件已落盘，效果未提交）")

    fixed = service.recover_pending()
    assert fixed == 1
    bucket = service.store.list_usage("s1", 0)
    assert len(bucket) == 1 and bucket[0].quantity == Decimal("100")
    log.info("恢复判定依据=PENDING 按 seq 重做，账期=p0 最终用量=%s", len(bucket))

    # 再恢复一次无副作用
    assert service.recover_pending() == 0


def test_replay_rebuilds_same_accounts(service):
    # 造一组包含成功、拒绝、账单与结算的完整历史
    service.report_usage({"event_id": "u1", "subscription_id": "s1",
                          "ts": "2026-01-20T00:00:00Z", "quantity": "1300"})
    try:
        service.report_usage({"event_id": "u-bad", "subscription_id": "s1",
                              "ts": "2026-01-20T00:00:00Z", "quantity": "-1"})
    except MeteringError:
        pass
    inv = service.invoice_period(
        {"event_id": "iv1", "subscription_id": "s1", "period_index": 0,
         "as_of": "2026-02-02T00:00:00Z"}
    )
    service.settle({"event_id": "st1", "invoice_id": inv["invoice_id"], "request_id": "r1"})

    balance_before = service.wallet.balance
    summary = service.replay()
    log.info("重放结果=%s 判定依据=事件流为唯一事实来源，按 seq 重建投影", summary)

    rebuilt = service.get_invoice(inv["invoice_id"])
    assert rebuilt["invoice_id"] == inv["invoice_id"]
    assert rebuilt["total_amount"] == "130.00"
    assert rebuilt["status"] == "settled"
    assert len(service.store.list_usage("s1", 0)) == 1
    # 被拒绝事件结论保留，拒绝原因可区分
    rejected = service.store.get_event("u-bad")
    assert rejected.status.value == "rejected"
    assert rejected.result["error_code"] == "usage_negative_quantity"
    # 重放不重复扣款
    assert service.wallet.balance == balance_before
    ledger = service.store.get_settlement(inv["invoice_id"])
    assert ledger.recovered is True
    log.info("账期=p0 账单=%s 重放后金额/状态一致，扣款不重复（recovered 台账）", inv["invoice_id"])


def test_replay_after_crash_converges(service):
    # 崩溃事件尚未 recover 直接 replay，也要收敛为与处理一次相同
    service.store.crash_before_apply.add("u-crash")
    try:
        service.report_usage(
            {"event_id": "u-crash", "subscription_id": "s1",
             "ts": "2026-01-08T00:00:00Z", "quantity": "250"}
        )
    except RuntimeError:
        pass
    summary = service.replay()
    bucket = service.store.list_usage("s1", 0)
    log.info("崩溃后直接重放：%s 账期=p0 用量条数=%s", summary, len(bucket))
    assert len(bucket) == 1
    assert service.store.get_event("u-crash").status.value == "applied"

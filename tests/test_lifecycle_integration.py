"""端到端集成：套餐变更、中途取消、跨周期、出账、结算、重放闭环。"""

from datetime import datetime
from decimal import Decimal

from app.clock import FixedClock
from app.errors import DomainError, ErrorCode
from app.models import EventOutcome
from app.services import MeteringService
from app.storage import InMemoryStore

T0 = datetime(2026, 1, 1, 0, 0)


def _svc():
    return MeteringService(store=InMemoryStore(), clock=FixedClock(T0))


def test_full_lifecycle_two_periods_with_change_and_cancel():
    svc = _svc()
    basic = svc.create_plan("plan-basic", "基础版", Decimal("100"),
                            Decimal("1000"), Decimal("0.1")).data["plan"]["id"]
    pro = svc.create_plan("plan-pro", "专业版", Decimal("200"),
                          Decimal("3000"), Decimal("0.2")).data["plan"]["id"]
    sid = svc.create_subscription("sub-1", basic).data["subscription"]["id"]

    # 第 0 期：1/10 用量 1200（超 200），1/16 升级专业版，1/25 用量 4000
    svc.report_usage("u-0-a", sid, Decimal("1200"),
                     datetime(2026, 1, 10), "m")
    svc.change_plan("chg-1", sid, pro, datetime(2026, 1, 16))
    svc.report_usage("u-0-b", sid, Decimal("4000"),
                     datetime(2026, 1, 25), "m")

    # 乱序补一笔第 0 期用量（升级前），应归入基础版片段并参与超量
    svc.report_usage("u-0-late", sid, Decimal("100"),
                     datetime(2026, 1, 5), "m")

    # 第 1 期：2/10 用量 2000（未超专业版 3000 额度），2/20 取消
    svc.report_usage("u-1-a", sid, Decimal("2000"),
                     datetime(2026, 2, 10), "m")
    svc.cancel_subscription("cancel-1", sid, datetime(2026, 2, 20))

    # 取消后的用量必须被拒绝
    try:
        svc.report_usage("u-after", sid, Decimal("1"),
                         datetime(2026, 2, 21), "m")
        raise AssertionError("取消后的用量应被拒绝")
    except DomainError as e:
        assert e.code == ErrorCode.USAGE_AFTER_CANCELLATION

    # ---- 第 0 期出账 ----
    inv0 = svc.generate_invoice("inv-0", sid, 0).data["invoice"]
    kinds0 = [li["kind"] for li in inv0["line_items"]]
    assert "base" in kinds0
    assert "prorated_credit" in kinds0
    assert "prorated_charge" in kinds0
    assert "overage" in kinds0
    # 抵扣行金额必须为负，补差行为正
    credits = [Decimal(li["amount"]) for li in inv0["line_items"]
               if li["kind"] == "prorated_credit"]
    charges = [Decimal(li["amount"]) for li in inv0["line_items"]
               if li["kind"] == "prorated_charge"]
    assert all(c < 0 for c in credits)
    assert all(c > 0 for c in charges)

    # ---- 第 1 期出账：中途取消抵扣，无超量 ----
    inv1 = svc.generate_invoice("inv-1", sid, 1).data["invoice"]
    # 2/1~2/20 用 19 天，2/20~3/1 退 9 天（2026 年 2 月 28 天）
    credit1 = [Decimal(li["amount"]) for li in inv1["line_items"]
               if li["kind"] == "prorated_credit"]
    assert credit1 == [Decimal("-64.29")]  # -200*9/28
    over1 = [li for li in inv1["line_items"] if li["kind"] == "overage"]
    assert over1 == []
    assert inv1["total_amount"] == "135.71"  # 200 - 64.29

    # 跨周期隔离：两期账单各自独立
    assert inv0["period_start"] != inv1["period_start"]

    # ---- 结算两期 ----
    pay0 = svc.settle("pay-0", sid, 0)
    pay1res = svc.settle("pay-1", sid, 1)
    assert pay0.data["invoice"]["status"] == "settled"
    assert pay1res.data["invoice"]["status"] == "settled"
    txn0 = pay0.data["invoice"]["payment_txn_id"]
    txn1 = pay1res.data["invoice"]["payment_txn_id"]
    assert txn0 != txn1
    assert len(svc.gateway._captured) == 2

    # ---- 重放所有结算/用量事件，结果稳定 ----
    assert svc.settle("pay-0", sid, 0).outcome == EventOutcome.DUPLICATE
    assert svc.settle("pay-1", sid, 1).outcome == EventOutcome.DUPLICATE
    assert len(svc.gateway._captured) == 2  # 无重复扣款

    replay = svc.replay_all()
    assert replay["consistent"] is True
    assert replay["checks"]["invoices_checked"] == 2

    # 已结算账单内容不可被重新生成覆盖
    try:
        svc.generate_invoice("inv-regen", sid, 0)
        raise AssertionError("已结算账单不应允许重新生成")
    except DomainError as e:
        assert e.code == ErrorCode.INVOICE_ALREADY_SETTLED

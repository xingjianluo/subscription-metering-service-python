"""中断重试：在原子保存点制造故障，重来后状态等同于只成功处理一次。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.clock import FixedClock
from app.errors import DomainError
from app.models import EventOutcome
from app.services import MeteringService
from app.storage import InMemoryStore, JsonFileStore

ANCHOR = datetime(2026, 1, 1, 9, 0)


class CrashOnSave(InMemoryStore):
    """第 fail_on 次 save 时抛错，模拟进程在落盘瞬间被杀死。"""

    def __init__(self, fail_on: int):
        super().__init__()
        self.fail_on = fail_on
        self.saves = 0

    def save(self, state):
        self.saves += 1
        if self.saves == self.fail_on:
            raise RuntimeError("模拟处理到一半崩溃")
        super().save(state)


def _svc(store):
    return MeteringService(store=store, clock=FixedClock(ANCHOR))


def _plan_sub(svc):
    pid = svc.create_plan("p1", "基础版", Decimal("100"),
                          Decimal("1000"), Decimal("0.1")).data["plan"]["id"]
    sid = svc.create_subscription("s1", pid).data["subscription"]["id"]
    return sid


def test_crash_during_usage_save_then_retry_is_clean():
    # 第 1 次 save 是 create_plan，让“用量上报”那次 save（第 3 次）崩溃
    store = CrashOnSave(fail_on=3)
    svc = _svc(store)
    sid = _plan_sub(svc)
    with pytest.raises(RuntimeError):
        svc.report_usage("u1", sid, Decimal("500"),
                         datetime(2026, 1, 10, 9), "m")

    # 崩溃后：台账没有该事件，状态无残留
    assert "u1" not in store.load().ledger
    assert store.load().usage.get((sid, 0)) is None

    # 用同一事件重试 -> 首次应用成功（不是 duplicate，因为没落盘）
    r = svc.report_usage("u1", sid, Decimal("500"),
                         datetime(2026, 1, 10, 9), "m")
    assert r.outcome == EventOutcome.APPLIED
    assert svc.get_window(sid, 0)["total_quantity"] == "500"


def test_crash_during_settle_save_gateway_still_consistent():
    # create_plan / create_subscription / usage / invoice = 4 次成功提交，
    # 第 5 次（结算落盘）崩溃，模拟扣款外部副作用已发生但本地快照未提交。
    store = CrashOnSave(fail_on=5)
    svc = _svc(store)
    sid = _plan_sub(svc)
    svc.report_usage("u1", sid, Decimal("1200"), datetime(2026, 1, 10, 9), "m")
    svc.generate_invoice("i1", sid, 0)

    with pytest.raises(RuntimeError):
        svc.settle("pay1", sid, 0)

    # 网关已扣款（外部副作用发生），但提交快照仍是 draft 且台账无结算事件
    assert len(svc.gateway._captured) == 1
    assert store.load().invoices[(sid, 0)].status.value == "draft"
    assert "pay1" not in store.load().ledger

    # 重来：同一结算事件再处理。网关以账单幂等键返回原交易（不重复扣款），
    # 本地补记为 settled，最终账目与一次成功完全一致。
    r = svc.settle("pay1", sid, 0)
    assert r.outcome == EventOutcome.APPLIED
    assert r.data["invoice"]["status"] == "settled"
    assert len(svc.gateway._captured) == 1
    assert svc.replay_all()["consistent"] is True


def test_json_store_never_exposes_half_written_snapshot(tmp_path):
    path = tmp_path / "state.json"
    store = JsonFileStore(path)
    svc = _svc(store)
    sid = _plan_sub(svc)
    svc.report_usage("u1", sid, Decimal("10"), datetime(2026, 1, 10, 9), "m")

    # 磁盘上的快照可完整重新加载
    reloaded = JsonFileStore(path).load()
    assert reloaded.usage[(sid, 0)][0].quantity == Decimal("10")
    # 不应残留临时文件
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []

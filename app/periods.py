"""账期与时间窗口：账期锚定订阅起始时刻，按月切分，区间左闭右开。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

# 一个账期 = 1 个自然月（锚点日超过月末时收口到月末）
_DAY_SECONDS = Decimal("86400")


def _shift_year_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def add_months(dt: datetime, months: int) -> datetime:
    """dt 加 months 个月；锚点日超过目标月月末时收口到月末（如 1/31 +1月 => 2/28）。"""
    y, m = _shift_year_month(dt.year, dt.month, months)
    # 目标月的最大日
    if m == 12:
        next_y, next_m = y + 1, 1
    else:
        next_y, next_m = y, m + 1
    import calendar

    max_day = calendar.monthrange(y, m)[1]
    day = min(dt.day, max_day)
    return dt.replace(year=y, month=m, day=day)


def period_bounds(start_at: datetime, index: int) -> tuple[datetime, datetime]:
    """第 index 个账期的 [start, end)。index 从 0 开始；index 必须非负。"""
    if index < 0:
        raise ValueError(f"period index must be >= 0, got {index}")
    start = add_months(start_at, index)
    end = add_months(start_at, index + 1)
    return start, end


def period_index_for(start_at: datetime, ts: datetime) -> int:
    """ts 落在第几个账期。早于订阅起点返回 -1（调用方负责拒绝跨账期串账）。"""
    if ts < start_at:
        return -1
    # 按 (年, 月) 相对锚点直接计算，天然处理乱序/补报
    months = (ts.year - start_at.year) * 12 + (ts.month - start_at.month)
    if _is_before_anchor_day(ts, start_at):
        months -= 1
    return months


def _is_before_anchor_day(ts: datetime, anchor: datetime) -> bool:
    """ts 是否还没到本账期的锚点日（用于锚点日非 1 号的情况）。"""
    import calendar

    anchor_day = min(anchor.day, calendar.monthrange(ts.year, ts.month)[1])
    if ts.day < anchor_day:
        return True
    if ts.day > anchor_day:
        return False
    return (ts.hour, ts.minute, ts.second, ts.microsecond) < (
        anchor.hour,
        anchor.minute,
        anchor.second,
        anchor.microsecond,
    )


def days_between(start: datetime, end: datetime) -> Decimal:
    """两天时间点之间的天数（含小数，秒级精度）。"""
    delta = end - start
    return Decimal(delta.total_seconds()) / _DAY_SECONDS


def days_ratio(
    start: datetime,
    end: datetime,
    period_start: datetime,
    period_end: datetime,
) -> Decimal:
    """小段天数 / 账期总天数。比例费用与内含额度都使用同一口径，保证可解释。"""
    total = days_between(period_start, period_end)
    part = days_between(start, end)
    if total <= 0:
        raise ValueError("period length must be positive")
    return (part / total).quantize(Decimal("0.00000001"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

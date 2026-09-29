"""计费周期计算：以订阅锚点对齐的自然月周期。

周期为左闭右开 [start, end)。锚点的“日/时/分/秒”决定每月对齐点；
当目标月份没有对应日（如 3/31 锚点遇到 2 月），落到目标月最后一天
的相同时刻。
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta


def add_months(dt: datetime, months: int) -> datetime:
    total = (dt.year * 12 + (dt.month - 1)) + months
    year, zero_based_month = divmod(total, 12)
    month = zero_based_month + 1
    last_day = monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)


def period_for(anchored_at: datetime, period_index: int) -> tuple[datetime, datetime]:
    start = add_months(anchored_at, period_index)
    end = add_months(anchored_at, period_index + 1)
    return start, end


def period_index_for(anchored_at: datetime, when: datetime) -> int:
    """粗略用 (年*12+月) 之差，再用精确边界修正锚点日/时的偏移。"""
    index = (when.year - anchored_at.year) * 12 + (when.month - anchored_at.month)
    start, _ = period_for(anchored_at, index)
    if when < start:
        index -= 1
    else:
        end = add_months(anchored_at, index + 1)
        if when >= end:
            index += 1
    return index


def day_count(start: datetime, end: datetime) -> int:
    """[start, end) 之间的整日数（按 24 小时一天计）。"""
    delta = end - start
    return int(delta // timedelta(days=1))

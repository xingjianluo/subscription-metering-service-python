"""可注入的时钟，便于测试乱序与重放。"""

from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        # 统一使用无时区的本地 UTC 时间，保证可比较、可序列化
        return datetime.now(timezone.utc).replace(tzinfo=None)


class FixedClock(Clock):
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

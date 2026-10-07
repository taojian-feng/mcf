from __future__ import annotations

from datetime import datetime, timedelta, timezone


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """Test clock: time only moves when told to."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, t: datetime) -> None:
        self._now = t

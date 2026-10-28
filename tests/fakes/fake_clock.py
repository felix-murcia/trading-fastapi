"""Fake Clock en memoria (satisface Clock por typing estructural)."""

from datetime import datetime
from src.domain.ports import Clock


class FakeClock(Clock):
    def __init__(self, now: datetime | None = None):
        self._now = now or datetime(2026, 10, 28, 0, 0, 0)

    def now(self):
        return self._now

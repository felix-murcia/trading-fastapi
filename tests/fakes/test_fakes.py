"""Tests de fakes en memoria."""

from datetime import datetime
from src.domain.entities import Trade
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_trade_repository import FakeTradeRepository


def test_fake_trade_repository_save_and_find():
    repo = FakeTradeRepository()
    t = Trade(
        id="t1", symbol="EURUSD", side="BUY",
        entry_price=1.10, exit_price=1.20, pnl=10.0,
        opened_at=datetime.now(), closed_at=datetime.now(),
    )
    repo.save(t)
    assert repo.find_by_id("t1") is t
    assert repo.find_by_id("inexistente") is None


def test_fake_clock_now():
    expected = datetime(2026, 10, 28, 12, 0, 0)
    clock = FakeClock(now=expected)
    assert clock.now() == expected

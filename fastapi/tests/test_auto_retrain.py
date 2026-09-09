"""
Tests for services/auto_retrain.py
"""
import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock
import numpy as np
import pandas as pd

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.auto_retrain import (
    _parse_timestamp,
    _persist_trade_outcome,
    _load_persisted_outcomes,
    TradeOutcome,
    get_state,
    RetrainConfig,
)
from ml.trading_env_v2 import ForexTradingEnvV2


class TestParseTimestamp:
    """Tests for _parse_timestamp function."""

    def test_parses_unix_timestamp_float(self):
        ts = 1725450000.0
        result = _parse_timestamp(ts)
        assert isinstance(result, float)
        assert result == 1725450000.0

    def test_parses_unix_timestamp_int(self):
        ts = 1725450000
        result = _parse_timestamp(ts)
        assert result == 1725450000.0

    def test_parses_unix_timestamp_string(self):
        """Numeric strings like MQL5 sends (e.g. '1725450000') must be handled."""
        ts = "1725450000"
        result = _parse_timestamp(ts)
        assert result == 1725450000.0

    def test_parses_iso_format_with_z(self):
        ts = "2024-09-04T10:00:00Z"
        result = _parse_timestamp(ts)
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert result == pytest.approx(dt.timestamp(), rel=1)

    def test_parses_iso_format_with_timezone(self):
        ts = "2024-09-04T10:00:00+00:00"
        result = _parse_timestamp(ts)
        dt = datetime.fromisoformat(ts)
        assert result == pytest.approx(dt.timestamp(), rel=1)

    def test_invalid_string_raises(self):
        ts = "not-a-timestamp"
        with pytest.raises(ValueError):
            _parse_timestamp(ts)

    def test_empty_string_raises(self):
        ts = ""
        with pytest.raises(ValueError):
            _parse_timestamp(ts)


class TestRetrainConfig:
    """Tests for RetrainConfig dataclass."""

    def test_default_values(self):
        config = RetrainConfig()
        assert config.trades_before_retrain == 10
        assert config.min_trades_for_retrain == 5
        assert config.lookback_candles == 500
        assert config.retrain_window_size == 10
        assert config.initial_balance == 1000.0
        assert config.n_epochs == 3

    def test_custom_values(self):
        config = RetrainConfig(trades_before_retrain=20, n_epochs=5)
        assert config.trades_before_retrain == 20
        assert config.n_epochs == 5


class TestGetState:
    """Tests for get_state function."""

    def test_returns_retrain_state(self):
        from services.auto_retrain import get_state, RetrainState
        state = get_state()
        assert isinstance(state, RetrainState)
        assert hasattr(state, "filled_count")
        assert hasattr(state, "outcomes")

    def test_filled_count_starts_at_zero(self):
        state = get_state()
        assert state.filled_count == 0


@pytest.mark.asyncio
async def test_load_persisted_outcomes_excludes_invalid_entry_times(monkeypatch):
    class FakePool:
        async def fetch(self, query):
            return [
                {
                    "symbol": "EURUSD",
                    "entry_time": datetime(2026, 9, 7, 10, 0),
                    "exit_time": datetime(2026, 9, 7, 11, 0),
                    "pnl": -15.5,
                    "pnl_pct": -0.0155,
                    "direction": "LONG",
                    "sl_hit": True,
                    "tp_hit": False,
                    "exit_reason": "sl",
                }
            ]

    monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
    outcomes = await _load_persisted_outcomes()

    assert len(outcomes) == 1
    assert outcomes[0].pnl == -15.5
    assert outcomes[0].direction == "LONG"


def test_real_loss_produces_negative_feedback():
    candles = pd.DataFrame({
        "time": pd.date_range("2026-09-07", periods=20, freq="h"),
        "open": np.full(20, 1.1),
        "high": np.full(20, 1.101),
        "low": np.full(20, 1.099),
        "close": np.full(20, 1.1),
    })
    outcome = TradeOutcome(
        symbol="EURUSD",
        entry_time=candles.iloc[10]["time"].timestamp(),
        exit_time=candles.iloc[11]["time"].timestamp(),
        pnl=-15.5,
        pnl_pct=-0.0155,
        direction="LONG",
        sl_hit=True,
        tp_hit=False,
        exit_reason="sl",
    )
    env = ForexTradingEnvV2(
        df=candles,
        window_size=10,
        real_outcomes=[outcome],
        real_outcome_weight=0.3,
    )
    env.reset()

    feedback = env._apply_real_outcome_feedback(
        np.array([1.0, 0.2, 0.2, 0.2], dtype=np.float32)
    )

    assert feedback < 0

"""
Tests for services/auto_retrain.py
"""
import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.auto_retrain import (
    _parse_timestamp,
    _persist_trade_outcome,
    get_state,
    RetrainConfig,
)


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

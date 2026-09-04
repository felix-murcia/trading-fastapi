"""
Pytest configuration and fixtures for FastAPI tests.

SET ENVIRONMENT VARS BEFORE any app imports happen — pydantic Settings
instantiates at module level (settings = Settings()) and will fail if
required fields are missing.
"""
import os
import sys

# Set required env vars BEFORE any app code is imported
os.environ.setdefault("database_url", "postgresql://test:test@localhost/test")
os.environ.setdefault("internal_token", "test-token")
os.environ.setdefault("hmac_secret", "test-hmac")
os.environ.setdefault("mt5_http_url", "http://localhost:8000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.fixture
def mock_settings():
    """Mock settings for tests that don't need real DB/MT5."""
    with patch("config.settings") as mock:
        mock.internal_token = "test-token"
        mock.database_url = "postgresql://test:test@localhost/test"
        mock.hmac_secret = "test-hmac"
        mock.mt5_http_url = "http://localhost:8000"
        mock.mt5_http_timeout = 5.0
        mock.min_volume = 0.01
        mock.max_volume = 0.50
        yield mock


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg pool that does nothing (for unit tests)."""
    mock = AsyncMock()
    mock.fetchval = AsyncMock(return_value=1)
    mock.fetch = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def sample_trade_request():
    """Sample TradeFilledRequest payload matching what MQL5 sends."""
    return {
        "symbol": "EURUSD",
        "entry_time": "1725450000",
        "exit_time": "1725453600",
        "pnl": -15.50,
        "pnl_pct": -0.0155,
        "direction": "SHORT",
        "sl_hit": True,
        "tp_hit": False,
        "exit_reason": "sl",
    }


@pytest.fixture
def sample_iso_trade_request():
    """Sample TradeFilledRequest with ISO timestamps."""
    return {
        "symbol": "EURUSD",
        "entry_time": "2024-09-04T10:00:00Z",
        "exit_time": "2024-09-04T11:00:00Z",
        "pnl": 25.00,
        "pnl_pct": 0.025,
        "direction": "LONG",
        "sl_hit": False,
        "tp_hit": True,
        "exit_reason": "tp",
    }


@pytest.fixture
def mock_alerting():
    """Mock alerting that records alerts without sending anywhere."""
    with patch("services.alerting._send_to_telegram") as mock_telegram:
        mock_telegram.return_value = None
        yield mock_telegram

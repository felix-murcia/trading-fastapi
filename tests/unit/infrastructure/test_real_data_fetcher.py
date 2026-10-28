"""Test de RealDataFetcher con monkeypatch (sin servicios reales).

Usa asyncio.run() para evitar depender de pytest-asyncio.
"""

import asyncio
from datetime import datetime

import pytest

from src.infrastructure.adapters.real_data_fetcher import RealDataFetcher


class _FakeRow:
    def __init__(self, **kw):
        self._d = kw

    def __getitem__(self, key):
        return self._d[key]


class _FakePool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, query, *params):
        return self.rows


class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, timeout=60.0):
        self.timeout = timeout
        self.last_url = None
        self.last_params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, params=None):
        self.last_url = url
        self.last_params = params
        return _FakeResponse([{"time": 1700000000, "open": 1.0, "high": 1.1,
                               "low": 0.9, "close": 1.0, "volume": 100}])


def test_fetch_trade_outcomes_devuelve_lista(monkeypatch):
    """fetch_trade_outcomes usa el pool inyectado (no asyncpg real)."""
    import db.connection as db_conn

    rows = [
        _FakeRow(symbol="EURUSD", entry_time=datetime(2026, 1, 1, 0, 0),
                 exit_time=datetime(2026, 1, 1, 1, 0), pnl=5.0, pnl_pct=0.5,
                 direction="LONG", sl_hit=False, tp_hit=True, exit_reason="tp"),
        _FakeRow(symbol="EURUSD", entry_time=datetime(2026, 1, 1, 1, 0),
                 exit_time=datetime(2026, 1, 1, 2, 0), pnl=-2.0, pnl_pct=-0.2,
                 direction="SHORT", sl_hit=True, tp_hit=False, exit_reason="sl"),
    ]
    pool = _FakePool(rows)

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(db_conn, "get_pool", _fake_get_pool)

    fetcher = RealDataFetcher(pool=pool)
    outcomes = asyncio.run(fetcher.fetch_trade_outcomes("EURUSD"))

    assert len(outcomes) == 2
    assert outcomes[0]["symbol"] == "EURUSD"
    assert outcomes[0]["direction"] == "LONG"
    assert outcomes[0]["pnl"] == 5.0
    assert outcomes[1]["direction"] == "SHORT"
    assert outcomes[1]["pnl"] == -2.0


def test_fetch_candles_usa_httpx(monkeypatch):
    """fetch_candles llama al MT5 MCP vía httpx (monkeypatcheado)."""
    import httpx

    client = _FakeAsyncClient()

    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=60.0: client)

    fetcher = RealDataFetcher(mt5_http_url="http://localhost:8000")
    candles = asyncio.run(fetcher.fetch_candles("EURUSD", count=500, timeframe="H1"))

    assert len(candles) == 1
    assert candles[0]["time"] == 1700000000
    assert client.last_params == {
        "symbol_name": "EURUSD",
        "timeframe": "H1",
        "count": 500,
    }

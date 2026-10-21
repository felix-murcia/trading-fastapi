"""
Curva de equity mark-to-market (2026-10-21).

Contrato:
- una foto por minuto con equity, balance y flotante (equity - balance);
- una cuenta sin equity válido NO se guarda como 0 (no se inventa el dato);
- `compute_dd` mide desde el pico del tramo, no desde el pico global;
- el bucle registra el fallo y sigue (el trading no depende de él).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import equity_curve


class FakeClient:
    def __init__(self, payload=None, *args, **kwargs):
        self.payload = payload or {"equity": 499.78, "balance": 502.31}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = self.payload
        return response


def _client(payload=None):
    def factory(*args, **kwargs):
        return FakeClient(payload)
    return factory


class TestComputeDD:
    def test_max_drawdown_from_peak(self):
        out = equity_curve.compute_dd([100.0, 120.0, 90.0, 110.0])
        assert out["n"] == 4
        assert out["max_dd_pct"] == 25.0
        assert out["peak_eur"] == 120.0
        assert out["trough_eur"] == 90.0

    def test_marks_the_worst_tranche_not_global_extremes(self):
        # Pico global 130, valle global 65: el peor tramo es 130 → 65 (50 %).
        out = equity_curve.compute_dd([120.0, 100.0, 130.0, 65.0])
        assert out["max_dd_pct"] == 50.0
        assert out["peak_eur"] == 130.0
        assert out["trough_eur"] == 65.0

    def test_no_drawdown_has_no_peak_trough(self):
        out = equity_curve.compute_dd([100.0, 110.0, 120.0])
        assert out["max_dd_pct"] == 0.0
        assert out["peak_eur"] is None
        assert out["trough_eur"] is None

    def test_empty_series_is_zero_not_an_invented_number(self):
        out = equity_curve.compute_dd([])
        assert out == {"n": 0, "max_dd_pct": 0.0, "peak_eur": None, "trough_eur": None}


class TestRecordSnapshot:
    def test_persists_equity_balance_and_floating(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={
            "ts": "2026-10-21T10:00:00+00:00",
            "equity": 499.78,
            "balance": 502.31,
            "floating": -2.53,
        })
        with patch("services.equity_curve.httpx.AsyncClient", _client()), \
             patch("services.equity_curve.get_pool", return_value=pool):
            out = asyncio.run(equity_curve.record_snapshot())

        sql, equity, balance, floating, source = pool.fetchrow.await_args.args
        assert "INSERT INTO equity_snapshots" in sql
        assert equity == 499.78
        assert balance == 502.31
        assert round(floating, 2) == -2.53
        assert source == "mt5_account_info"
        assert out["floating"] == -2.53

    def test_rejects_account_without_valid_equity(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock()
        with patch("services.equity_curve.httpx.AsyncClient", _client({"equity": 0, "balance": 0})), \
             patch("services.equity_curve.get_pool", return_value=pool):
            with pytest.raises(ValueError):
                asyncio.run(equity_curve.record_snapshot())
        pool.fetchrow.assert_not_awaited()


class TestGetDD:
    def test_reads_the_stored_curve(self):
        pool = MagicMock()
        pool.fetch = AsyncMock(return_value=[
            {"equity": 100.0}, {"equity": 80.0}, {"equity": 90.0},
        ])
        with patch("services.equity_curve.get_pool", return_value=pool):
            out = asyncio.run(equity_curve.get_dd())
        assert out["max_dd_pct"] == 20.0
        assert "ORDER BY ts" in pool.fetch.await_args.args[0]

    def test_since_filters_with_a_parameter(self):
        pool = MagicMock()
        pool.fetch = AsyncMock(return_value=[{"equity": 100.0}])
        since = "2026-10-21T00:00:00+00:00"
        with patch("services.equity_curve.get_pool", return_value=pool):
            asyncio.run(equity_curve.get_dd(since=since))
        assert pool.fetch.await_args.args[1] == since


class TestInit:
    def test_creates_the_table_idempotently(self):
        pool = MagicMock()
        pool.execute = AsyncMock()
        with patch("services.equity_curve.get_pool", return_value=pool):
            asyncio.run(equity_curve.init_equity_table())
        sql = pool.execute.await_args.args[0]
        assert "CREATE TABLE IF NOT EXISTS equity_snapshots" in sql
        assert "CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ts" in sql

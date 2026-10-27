"""
E-07.2 — Filtros de rango de fechas y granularidad en /v1/smc/analytics.

TDD: tests escritos ANTES de la implementación. El endpoint actual no
acepta parámetros de query; estos tests deben fallar inicialmente (RED),
luego se implementa el endpoint (GREEN) y finalmente se refactoriza.

Reglas del proyecto (AGENTS.md + CLAUDE.md + PROTOCOLO_ANTI_BUCLE.md):
- No silent failures: errores se propagan y se devuelven al cliente.
- Dos relojes: backend/Postgres en UTC real; MT5/EA en reloj del bróker.
  La conversión broker→UTC usa services.timebase.broker_offset_hours.
- Sin joins temporales (protocolo P0.2c).
- Simplicity first: mínimo código, nada especulativo.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

AUTH_HEADERS = {"x-internal-token": "test-token", "x-cycle-id": "analytics-filters"}


def _deal(profit, time_msc, entry=1, symbol="EURUSD"):
    return {
        "entry": entry,
        "profit": profit,
        "swap": 0.0,
        "commission": 0.0,
        "time_msc": time_msc,
        "symbol": symbol,
    }


def _ms(dt: datetime) -> int:
    """Datetime aware → epoch ms tal cual lo emite MT5 (sin corregir)."""
    return int(dt.timestamp() * 1000)


def _fake_httpx_client(deals, candle_times, candle_status=200):
    """httpx.AsyncClient falso: deals + account + vela(s) de detección."""

    class _Resp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                request = httpx.Request("GET", "http://test")
                raise httpx.HTTPStatusError(
                    f"HTTP {self.status_code}",
                    request=request,
                    response=httpx.Response(self.status_code, request=request),
                )

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *args, **kwargs):
            if url.endswith("/api/v1/history/deals"):
                return _Resp(deals)
            if "/api/v1/market/candles/latest" in url:
                return _Resp(
                    [{"time": t} for t in candle_times],
                    status_code=candle_status,
                )
            return _Resp({"balance": 4200.0})

    return _Client


def _analytics(deals, candle_times, params=None, candle_status=200):
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.load_inference_artifacts"), \
         patch("httpx.AsyncClient",
               _fake_httpx_client(deals, candle_times, candle_status)), \
         patch("services.broker_clock_store.persist_broker_offset",
               AsyncMock()):
        with TestClient(app) as client:
            return client.get(
                "/v1/smc/analytics",
                headers=AUTH_HEADERS,
                params=params,
            )


def _broker_lying_candle() -> str:
    """Vela formándose con sello broker (UTC+3)."""
    stamp = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
        minute=0, second=0, microsecond=0
    )
    return stamp.isoformat().replace("+00:00", "Z")


class TestAnalyticsDateFilters:
    """Filtros de rango de fechas (from/to) en /v1/smc/analytics."""

    def test_from_filters_deals_before_start_date(self):
        """Deals antes de 'from' quedan excluidos."""
        deals = [
            _deal(10.0, _ms(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))),
            _deal(20.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))),
            _deal(30.0, _ms(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))),
        ]
        res = _analytics(
            deals,
            [_broker_lying_candle()],
            params={"from": "2026-10-01"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        # Solo el deal del 2026-10-01 y el del 2026-10-05 pasan el filtro
        keys = sorted(data["daily_pnl"].keys())
        assert keys == ["2026-10-01", "2026-10-05"]
        assert data["daily_pnl"]["2026-10-01"] == 20.0
        assert data["daily_pnl"]["2026-10-05"] == 30.0

    def test_to_filters_deals_after_end_date(self):
        """Deals después de 'to' quedan excluidos."""
        deals = [
            _deal(10.0, _ms(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))),
            _deal(20.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))),
            _deal(30.0, _ms(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))),
        ]
        res = _analytics(
            deals,
            [_broker_lying_candle()],
            params={"to": "2026-10-02"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        keys = sorted(data["daily_pnl"].keys())
        # Offset bróker +3h: 2026-09-28 10:00 broker → 2026-09-28 07:00 UTC
        # 2026-10-01 10:00 broker → 2026-10-01 07:00 UTC
        # 2026-10-05 10:00 broker → 2026-10-05 07:00 UTC (fuera de to)
        assert keys == ["2026-09-28", "2026-10-01"]
        assert data["daily_pnl"]["2026-09-28"] == 10.0
        assert data["daily_pnl"]["2026-10-01"] == 20.0

    def test_from_and_to_together(self):
        """Ambos filtros aplicados simultáneamente."""
        deals = [
            _deal(10.0, _ms(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))),
            _deal(20.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))),
            _deal(30.0, _ms(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))),
        ]
        res = _analytics(
            deals,
            [_broker_lying_candle()],
            params={"from": "2026-10-01", "to": "2026-10-03"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        keys = sorted(data["daily_pnl"].keys())
        assert keys == ["2026-10-01"]

    def test_from_greater_than_to_returns_400(self):
        """from posterior a to es error 400 (no silent fallback)."""
        res = _analytics(
            [_deal(10.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)))],
            [_broker_lying_candle()],
            params={"from": "2026-10-05", "to": "2026-10-01"},
        )
        assert res.status_code == 400

    def test_invalid_date_format_returns_400(self):
        """Fecha con formato inválido devuelve error 400."""
        res = _analytics(
            [_deal(10.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)))],
            [_broker_lying_candle()],
            params={"from": "not-a-date"},
        )
        assert res.status_code == 400


class TestAnalyticsGranularity:
    """Filtro de granularidad (M/H/D/MN) en /v1/smc/analytics."""

    def test_granularity_d_groups_by_day(self):
        """Granularidad D (día) agrupa por día UTC."""
        deals = [
            _deal(10.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))),
            _deal(20.0, _ms(datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc))),
            _deal(30.0, _ms(datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc))),
        ]
        res = _analytics(
            deals,
            [_broker_lying_candle()],
            params={"granularity": "D"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        keys = sorted(data["daily_pnl"].keys())
        assert keys == ["2026-10-01", "2026-10-02"]
        assert data["daily_pnl"]["2026-10-01"] == 30.0
        assert data["daily_pnl"]["2026-10-02"] == 30.0

    def test_granularity_invalid_returns_400(self):
        """Granularidad no válida devuelve error 400."""
        res = _analytics(
            [_deal(10.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)))],
            [_broker_lying_candle()],
            params={"granularity": "W"},
        )
        assert res.status_code == 400


class TestAnalyticsSymbolFilter:
    """Filtro por símbolo en /v1/smc/analytics."""

    def test_symbol_filters_deals(self):
        """Solo se incluyen deals del símbolo solicitado."""
        deals = [
            _deal(10.0, _ms(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)), symbol="EURUSD"),
            _deal(20.0, _ms(datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)), symbol="USDCHF"),
            _deal(30.0, _ms(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)), symbol="EURUSD"),
        ]
        res = _analytics(
            deals,
            [_broker_lying_candle()],
            params={"symbol": "EURUSD"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        assert data["by_symbol"] == {"EURUSD": 40.0}

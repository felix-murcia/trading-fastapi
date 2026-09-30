"""
D-3 / R-2 (plan aprobado 2026-09-30): dominio del "día" unificado en UTC.

Evidencia forense del hallazgo: orders.created_at 06:50:17.456 (UTC real,
reloj de la DB) ↔ deal de apertura time_msc 09:50:17.535 (epoch→UTC) =
+3h exactos, mismos ms → MT5 vende el reloj BROKER como epoch.

Sin corregir, /v1/smc/analytics agrupaba daily_pnl por DÍA BROKER mientras
risk_state_store/risk_guardian (límite diario de riesgo) agrupan por UTC
real: mismo cierre, dos días distintos entre 21:00-00:00 UTC.

La corrección detecta el desfase con la vela formándose
(market_time.detect_utc_offset_hours, auto-corrector ya probado en P2-3):
- feed broker que miente +3h → agrupa por el día UTC verdadero.
- feed honesto (UTC) → desfase 0 → agrupa por el epoch tal cual.
- sin vela detectable → el endpoint propaga error (sin agrupar en falso).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app

AUTH_HEADERS = {"x-internal-token": "test-token", "x-cycle-id": "d3-utc-day"}


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


def _analytics(deals, candle_times, candle_status=200):
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.load_inference_artifacts"), \
         patch("httpx.AsyncClient",
               _fake_httpx_client(deals, candle_times, candle_status)):
        with TestClient(app) as client:
            return client.get("/v1/smc/analytics", headers=AUTH_HEADERS)


def _broker_lying_candle() -> str:
    """Vela formándose con sello broker (UTC+3): floor(now)+3h.

    market_time la interpreta como su propio reloj y devuelve offset 3
    (±1h en el borde exacto de redondeo, que los tests toleran porque el
    día UTC no cambia dentro de ese margen).
    """
    stamp = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
        minute=0, second=0, microsecond=0
    )
    return stamp.isoformat().replace("+00:00", "Z")


class TestBrokerDayRollsToUtc:
    """Un cierre a las 01:30 del reloj broker es 22:30 UTC del día ANTERIOR."""

    def test_daily_pnl_groups_by_utc_day_not_broker_day(self):
        broker_close = datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc)
        res = _analytics(
            [_deal(11.80, _ms(broker_close))],
            [_broker_lying_candle()],
        )
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        # UTC real = 2026-09-30 22:30 (offset 3; ±1h por el borde de
        # redondeo → 21:30/23:30, mismo día). Sin D-3 la clave sería
        # "2026-10-01" (día broker): la ventana del dashboard mediría
        # otra cosa que el límite diario de riesgo.
        assert list(data["daily_pnl"].keys()) == ["2026-09-30"]
        assert data["evolution"][-1]["time"].startswith("2026-09-30")
        # La P&L del cierre sigue contándose (la corrección es de dominio,
        # no de inclusión).
        assert data["daily_pnl"]["2026-09-30"] == 11.80

    def test_entry_deals_are_not_double_counted(self):
        """La apertura (entry=0) sigue fuera del conteo, corregido o no."""
        broker_close = datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc)
        res = _analytics(
            [_deal(0.0, _ms(broker_close), entry=0)],
            [_broker_lying_candle()],
        )
        data = res.json()
        assert data["daily_pnl"] == {}


class TestHonestFeedOffsetZero:
    """Feed que sella UTC honesto → desfase 0 → agrupación sin cambios."""

    def test_close_at_0030_utc_stays_on_the_same_day(self):
        honest_close = datetime(2026, 9, 30, 0, 30, tzinfo=timezone.utc)
        honest_candle = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        res = _analytics([_deal(5.0, _ms(honest_close))], [honest_candle])
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        # Con offset 3 esto sería "2026-09-29": el test detecta tanto un
        # feed honesto sin corregir como uno broker corregido de más.
        assert list(data["daily_pnl"].keys()) == ["2026-09-30"]


class TestNoSilentFallback:
    """Sin vela detectable no se agrupa en falso: el error se propaga."""

    def test_empty_candle_list_returns_error_not_wrong_grouping(self):
        broker_close = datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc)
        res = _analytics([_deal(11.80, _ms(broker_close))], [])
        data = res.json()
        assert "error" in data
        assert "daily_pnl" not in data

    def test_candle_http_error_returns_error(self):
        broker_close = datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc)
        res = _analytics(
            [_deal(11.80, _ms(broker_close))],
            [_broker_lying_candle()],
            candle_status=500,
        )
        data = res.json()
        assert "error" in data
        assert "daily_pnl" not in data


class TestNoDeals:
    """Sin deals no se necesita desfase: respuesta vacía, sin llamada extra."""

    def test_empty_deals_returns_empty_stats(self):
        res = _analytics([], [_broker_lying_candle()])
        assert res.status_code == 200
        data = res.json()
        assert "error" not in data
        assert data["daily_pnl"] == {}

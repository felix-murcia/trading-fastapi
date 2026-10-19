"""
T-31 (2026-10-19) — Normalización del sello de vela al UTC real.

BUG REAL (medido en vivo): el servidor MT5 va +3 h adelantado (precio
21:56Z vs backend 18:56Z) y sella las velas con SU reloj marcándolas "Z".
Consecuencias medidas:
  (1) la costura del feed MCP↔CSV (`resultadosBT/`, que es UTC puro)
      arrastraba un salto fantasma de 3 h;
  (2) el emparejamiento de los outcomes reales con las velas
      (`ml/trading_env_v2._index_real_outcomes`, marcas del EA en UTC)
      atribuía el feedback real a la vela ~3 h ANTES de la entrada ⇒ reward
      del PPO contaminado en cada retrain;
  (3) toda medición que mezclara sellos de vela con marcas del EA/DB
      quedaba desplazada 3 h (y "24 barras" ≠ 24 h por los huecos).

La corrección usa el auto-corrector YA PROBADO (services/market_time, P2-3):
el desfase se deduce de la vela más reciente (la formándose) ⇒ cero
hardcode, sigue solo si el broker cambia de huso.
"""
import datetime as dt

import pytest

from services.market_time import (
    broker_offset_hours_from_candles,
    correct_candle_epoch,
    detect_utc_offset_hours,
    shift_candle_times,
)


def _candle(ts_iso: str) -> dict:
    return {"time": ts_iso, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1}


class TestBrokerOffsetDetection:
    def test_offset_detected_from_forming_candle(self):
        """Vela formándose sellada +3 h ⇒ desfase 3 (idéntico al medido)."""
        now = dt.datetime(2026, 10, 19, 18, 56, tzinfo=dt.timezone.utc)
        forming = dt.datetime(2026, 10, 19, 21, 0, tzinfo=dt.timezone.utc)
        assert detect_utc_offset_hours(forming, now) == 3

    def test_honest_feed_gives_zero(self):
        """Feed que ya sella UTC real ⇒ desfase 0 (sin corrección)."""
        now = dt.datetime(2026, 10, 19, 18, 56, tzinfo=dt.timezone.utc)
        forming = dt.datetime(2026, 10, 19, 18, 0, tzinfo=dt.timezone.utc)
        assert detect_utc_offset_hours(forming, now) == 0

    def test_offset_from_candles_list(self):
        now = dt.datetime(2026, 10, 19, 18, 56, tzinfo=dt.timezone.utc)
        candles = [
            _candle("2026-10-19T19:00:00Z"),
            _candle("2026-10-19T21:00:00Z"),   # la formándose (sellada +3 h)
        ]
        assert broker_offset_hours_from_candles(candles, now) == 3

    def test_no_candles_no_offset(self):
        assert broker_offset_hours_from_candles([], dt.datetime.now(dt.timezone.utc)) == 0

    def test_historical_only_series_no_offset(self):
        """Serie que no contiene la vela formándose ⇒ sin reloj con el que
        comparar ⇒ offset 0 (no se corrige a ciegas)."""
        now = dt.datetime(2026, 10, 19, 18, 56, tzinfo=dt.timezone.utc)
        candles = [_candle("2026-10-01T10:00:00Z"), _candle("2026-10-01T11:00:00Z")]
        assert broker_offset_hours_from_candles(candles, now) == 0


class TestShiftCandles:
    def test_iso_times_shifted_preserving_format(self):
        candles = [_candle("2026-10-19T21:00:00Z")]
        out = shift_candle_times(candles, 3)
        assert out[0]["time"] == "2026-10-19T18:00:00Z", "el sello debe pasar a UTC real"
        assert candles[0]["time"] == "2026-10-19T21:00:00Z", "la entrada no se muta"

    def test_epoch_times_shifted(self):
        candles = [{"time": 1792443600, "close": 1.1}]
        out = shift_candle_times(candles, 3)
        assert out[0]["time"] == 1792443600 - 3 * 3600

    def test_zero_offset_is_noop(self):
        candles = [_candle("2026-10-19T21:00:00Z")]
        assert shift_candle_times(candles, 0) is candles

    def test_malformed_time_passes_through(self):
        candles = [_candle("no-es-una-fecha")]
        out = shift_candle_times(candles, 3)
        assert out[0]["time"] == "no-es-una-fecha"

    def test_correct_candle_epoch(self):
        assert correct_candle_epoch(1000.0, 3) == 1000.0 - 10800
        assert correct_candle_epoch(1000.0, 0) == 1000.0


class TestFeedSeamConsistency:
    """La costura MCP↔CSV: tras corregir, no puede haber salto de 3 h."""

    def test_seam_between_broker_feed_and_utc_csv(self):
        """Escenario real del 19-10: el CSV acaba a las 22:00 UTC y el bloque
        vivo del MCP empieza a las 23:00 UTC (sellado 02:00 del broker). Con
        la corrección, el bloque vivo empieza a las 23:00 UTC ⇒ sin salto."""
        csv_last = "2026-10-19T22:00:00Z"
        # El broker sella con su reloj (+3 h): 02:00 broker = 23:00 UTC,
        # 04:00 broker = 01:00 UTC (esta es la vela formándose).
        mcp_raw = [
            _candle("2026-10-20T02:00:00Z"),
            _candle("2026-10-20T04:00:00Z"),
        ]
        now = dt.datetime(2026, 10, 20, 1, 30, tzinfo=dt.timezone.utc)
        offset = broker_offset_hours_from_candles(mcp_raw, now)
        assert offset == 3
        corrected = shift_candle_times(mcp_raw, offset)
        first_live = dt.datetime.fromisoformat(corrected[0]["time"].replace("Z", "+00:00"))
        csv_end = dt.datetime.fromisoformat(csv_last.replace("Z", "+00:00"))
        gap_h = (first_live - csv_end).total_seconds() / 3600
        assert 0 < gap_h <= 24, (
            f"la costura debe ser continua (gap {gap_h:.1f} h) — sin corregir "
            f"serían 3 h de salto fantasma"
        )


@pytest.mark.parametrize("offset", [0, 1, 2, 3])
def test_shift_is_reversible_in_offset_math(offset):
    """La corrección es una traslación exacta: el sello final + el desfase
    vuelve al sello original (sin acumular error ni redondeo)."""
    epoch = 1792443600.0
    assert correct_candle_epoch(correct_candle_epoch(epoch, offset), -offset) == epoch

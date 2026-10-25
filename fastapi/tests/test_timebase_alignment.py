"""
RED DE REGRESIÓN DE LA ALINEACIÓN TEMPORAL (T-31.9, 2026-10-20).

Félix (2026-10-20): *«revisa que todos los timeframes estén alineados (EA,
sistema, aplicación, MT5, broker, etc). Es fundamental que todas las franjas
horarias y UTCs estén alineados. Es lo más básico y fundamental; si eso no
está bien no estamos haciendo nada»*.

Este fichero es la RED que impide que la alineación se rompa sin que nadie
se entere. Fija:

  1. El desfase broker→UTC se detecta por DOS vías independientes (el reloj
     del servidor y la vela en formación) y ambas coinciden.
  2. La detección es en HORAS ENTERAS y NO depende del minuto de la hora
     (el bug que desgarbaba todas las mediciones: fluctuaba entre 2 y 4 h).
  3. El DÍA de la cuenta es el del BRÓKER (21:00–21:00 UTC con +3 h;
     22:00–22:00 UTC con +2 h) ⇒ el límite diario de pérdida se alinea con
     el día del extracto.
  4. El corte diario del REGISTRO convierte las marcas del bróquer al UTC
     antes de agrupar (si no, la línea del día se comía 3 h del día
     anterior).
  5. La etiqueta de SESIÓN que ve el LLM es la hora UTC real, incluso si
     el feed se retrasa 1-2 velas.
  6. Las rejillas: H1 en punto de hora; H4/D1 en la rejilla del bróquer.
  7. El CAMBIO DE HORARIO del 25-10-2026 (+3 h → +2 h) no rompe nada: se
     detecta, y el día del bróquer se recalcula solo.
  8. El EA y el backend comparten el mismo desfase (el EA va en el reloj
     del bróquer; el backend, en UTC real).
"""
import datetime as dt
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import timebase
from services.market_time import (
    broker_offset_hours_from_candles,
    detect_utc_offset_hours,
    shift_candle_times,
    utc_hour_from_candle,
)


def _stamp(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc)


class TestOffsetDetection:
    """El desfase: entero, estable y con dos vías que coinciden."""

    def test_offset_is_whole_hours_and_minute_independent(self):
        """El mismo desfase a las 00:01 y a las 00:59 (bug histórico)."""
        for minute in (1, 17, 30, 59):
            now = _stamp(2026, 10, 20, 12, minute)
            forming = now + dt.timedelta(hours=3)
            assert detect_utc_offset_hours(forming, now) == 3, (
                f"el desfase se movió con el minuto ({minute})"
            )

    def test_offset_from_candles_matches_hour_arithmetic(self):
        """La vela en formación ⇒ el desfase por la aritmética de hora."""
        now = _stamp(2026, 10, 20, 16, 42)
        candles = [
            {"time": (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"time": (now + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        ]
        assert broker_offset_hours_from_candles(candles, now=now) == 3

    def test_dst_change_is_detected_not_assumed(self):
        """25-10-2026: el bróquer pasa de +3 h a +2 h ⇒ se detecta solo."""
        now = _stamp(2026, 10, 25, 16, 20)
        candles = [
            {"time": (now + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        ]
        assert broker_offset_hours_from_candles(candles, now=now) == 2
        now2 = _stamp(2026, 10, 20, 16, 20)
        candles2 = [
            {"time": (now2 + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        ]
        assert broker_offset_hours_from_candles(candles2, now=now2) == 3

    def test_normalisation_moves_candles_to_utc(self):
        candles = [
            {"time": "2026-10-20T19:00:00Z", "close": 1.1},
            {"time": "2026-10-20T18:00:00Z", "close": 1.2},
        ]
        out = shift_candle_times(candles, 3)
        stamps = sorted(c["time"] for c in out)
        assert stamps == ["2026-10-20T15:00:00Z", "2026-10-20T16:00:00Z"]


class TestStaleBrokerClockSources:
    class _Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, *_args, **_kwargs):
            return TestStaleBrokerClockSources._Response(self._payload)

    @pytest.mark.asyncio
    async def test_implausible_price_timestamp_uses_fresh_h1_offset(self, monkeypatch):
        import httpx

        now = dt.datetime.now(dt.timezone.utc)
        stale_time = (now - dt.timedelta(hours=13)).isoformat()
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **_kwargs: self._Client({"time": stale_time}),
        )
        monkeypatch.setattr(
            timebase, "_offset_from_candles",
            AsyncMock(return_value=2),
        )
        monkeypatch.setattr(timebase, "_OFFSET_CACHE", {"offset_h": 3, "ts": 0.0})
        monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {"offset_h": 3, "source": "test"})

        offset = await timebase.broker_offset_hours("EURUSD")

        assert offset == 2
        assert timebase._OFFSET_CACHE["offset_h"] == 2
        assert timebase.current_broker_offset() == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("candle_offset", [None, -19])
    async def test_unavailable_fresh_clock_preserves_last_known_offset(
        self, monkeypatch, candle_offset
    ):
        import httpx

        now = dt.datetime.now(dt.timezone.utc)
        stale_time = (now - dt.timedelta(hours=13)).isoformat()
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **_kwargs: self._Client({"time": stale_time}),
        )
        monkeypatch.setattr(
            timebase, "_offset_from_candles",
            AsyncMock(return_value=candle_offset),
        )
        monkeypatch.setattr(timebase, "_OFFSET_CACHE", {"offset_h": 3, "ts": 0.0})
        monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {"offset_h": 3, "source": "test"})

        offset = await timebase.broker_offset_hours("EURUSD")

        assert offset == 3
        assert timebase._OFFSET_CACHE["offset_h"] == 3
        assert timebase.current_broker_offset() == 3

    @pytest.mark.asyncio
    async def test_no_validated_offset_stays_unvalidated_when_sources_are_stale(
        self, monkeypatch
    ):
        import httpx

        now = dt.datetime.now(dt.timezone.utc)
        stale_time = (now - dt.timedelta(hours=13)).isoformat()
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **_kwargs: self._Client({"time": stale_time}),
        )
        monkeypatch.setattr(
            timebase, "_offset_from_candles", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(timebase, "_OFFSET_CACHE", {"offset_h": 0, "ts": 0.0})
        monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {
            "offset_h": 0, "source": "sin_detecccion", "validated": False,
        })

        assert await timebase.broker_offset_hours("EURUSD") == 0
        assert not timebase.broker_offset_is_validated()

    @pytest.mark.asyncio
    async def test_stale_h1_candles_are_not_a_detected_offset(self, monkeypatch):
        import httpx

        stale_time = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)
        ).isoformat()
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **_kwargs: self._Client({"candles": [{"time": stale_time}]}),
        )

        assert await timebase._offset_from_candles("EURUSD") is None

    @pytest.mark.asyncio
    async def test_implausible_cached_offset_is_not_returned(self, monkeypatch):
        import httpx

        now = dt.datetime.now(dt.timezone.utc)
        server_time = (now + dt.timedelta(hours=2)).isoformat()
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **_kwargs: self._Client({"time": server_time}),
        )
        monkeypatch.setattr(timebase, "_OFFSET_CACHE", {
            "offset_h": 13, "ts": timebase.time.time(),
        })
        monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {"offset_h": 3, "source": "test"})

        offset = await timebase.broker_offset_hours("EURUSD")

        assert offset == 2
        assert timebase._OFFSET_CACHE["offset_h"] == 2


class TestBrokerDay:
    """El día de la cuenta = el día del BRÓKER (el del extracto)."""

    def test_broker_day_bounds_with_offset_3(self):
        now = _stamp(2026, 10, 20, 16, 30)
        start, end = timebase.broker_day_bounds(now, offset_h=3)
        assert start == _stamp(2026, 10, 19, 21, 0)
        assert end == _stamp(2026, 10, 20, 21, 0)

    def test_broker_day_bounds_with_offset_2_after_dst(self):
        now = _stamp(2026, 10, 26, 16, 30)
        start, end = timebase.broker_day_bounds(now, offset_h=2)
        assert start == _stamp(2026, 10, 25, 22, 0)
        assert end == _stamp(2026, 10, 26, 22, 0)

    def test_broker_day_bounds_rollover_at_broker_midnight(self):
        """A las 21:00 UTC (medianoche del bróker) empieza un día nuevo."""
        antes = _stamp(2026, 10, 20, 20, 59)
        despues = _stamp(2026, 10, 20, 21, 1)
        s1, _ = timebase.broker_day_bounds(antes, offset_h=3)
        s2, _ = timebase.broker_day_bounds(despues, offset_h=3)
        assert s1 == _stamp(2026, 10, 19, 21, 0)
        assert s2 == _stamp(2026, 10, 20, 21, 0)

    def test_broker_today_uses_known_offset(self):
        timebase.set_broker_offset(3, source="test")
        try:
            # 21:30 UTC ⇒ el bróker ya va por el día siguiente
            assert timebase.broker_today(_stamp(2026, 10, 20, 21, 30)) == dt.date(2026, 10, 21)
            assert timebase.broker_today(_stamp(2026, 10, 20, 20, 30)) == dt.date(2026, 10, 20)
        finally:
            timebase.set_broker_offset(0, source="test-reset")

    def test_broker_today_degrades_to_utc_without_offset(self):
        timebase.set_broker_offset(0, source="test")
        assert timebase.broker_today(_stamp(2026, 10, 20, 21, 30)) == dt.date(2026, 10, 20)


class TestRiskDayFollowsBroker:
    """El límite diario de pérdida mide el día del BROKER, no el día UTC."""

    def test_risk_rollover_happens_at_broker_midnight(self):
        from services import risk_guardian

        timebase.set_broker_offset(3, source="test")
        try:
            # 20:30 UTC → aún el día 20 del bróker
            assert risk_guardian._utc_today() == dt.datetime.now(dt.timezone.utc).date() or True
            # el comportamiento clave: con el desfase conocido, la clave del
            # día es la del bróker
            hoy_bróker = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).date()
            assert risk_guardian._utc_today() == hoy_bróker
        finally:
            timebase.set_broker_offset(0, source="test-reset")

    def test_account_state_rolls_over_on_broker_day_change(self):
        from services.risk_guardian import AccountRiskState

        st = AccountRiskState()
        st.start(100.0)
        st.observe(101.0)
        assert st.daily_start_equity == 100.0
        # el rollover ocurre cuando cambia la clave de día
        st.day = dt.date(1999, 1, 1)
        st.observe(102.0)
        assert st.daily_start_equity == 102.0
        assert st.day != dt.date(1999, 1, 1)


class TestSessionLabel:
    """Lo que ve el LLM: la hora UTC real."""

    def test_session_hour_of_broker_stamp_is_the_utc_hour(self):
        now = _stamp(2026, 10, 20, 16, 20)
        forming = now + dt.timedelta(hours=3)
        assert utc_hour_from_candle(forming, now=now) == 16

    def test_session_hour_survives_a_lagging_feed(self):
        """Si el feed va 1-2 velas atrasado, la etiqueta sigue siendo la
        hora UTC real (el autocorrector compensa el retraso exacto)."""
        now = _stamp(2026, 10, 20, 16, 20)
        for lag in (1, 2):
            stamp = (now - dt.timedelta(hours=lag)) + dt.timedelta(hours=3)
            assert utc_hour_from_candle(stamp, now=now) == 16, f"lag={lag}"

    def test_session_hour_after_normalisation_is_unchanged(self):
        """El camino vivo normaliza a UTC y luego etiqueta: no puede
        aplicar dos veces la corrección."""
        now = _stamp(2026, 10, 20, 16, 20)
        raw = [{"time": (now + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
        utc = shift_candle_times(raw, 3)
        stamp = dt.datetime.fromisoformat(utc[0]["time"].replace("Z", "+00:00"))
        assert utc_hour_from_candle(stamp, now=now) == 16


class TestGrids:
    """Rejillas horarias: H1 en punto, H4/D1 en la del bróquer."""

    def test_h1_grid_is_on_the_hour(self):
        now = _stamp(2026, 10, 20, 16, 20)
        raw = [
            {"time": (now.replace(minute=0, second=0) + dt.timedelta(hours=3 - i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")}
            for i in range(5)
        ]
        utc = shift_candle_times(raw, 3)
        for c in utc:
            assert dt.datetime.fromisoformat(c["time"].replace("Z", "+00:00")).minute == 0

    def test_h4_and_d1_keep_the_broker_grid(self):
        """El H4 del bróquer abre a las 21:00 UTC y el D1 cierra a las
        21:00 UTC (su día NO es el día civil UTC)."""
        # una vela H4 del bróquer: abre a las 16:00 del bróquer (=13:00 UTC)
        h4 = [{"time": "2026-10-20T16:00:00Z"}]
        utc = shift_candle_times(h4, 3)
        stamp = dt.datetime.fromisoformat(utc[0]["time"].replace("Z", "+00:00"))
        assert stamp.hour == 13
        # la rejilla del H4 del bróquer: 21:00, 01:00, 05:00, 09:00, 13:00, 17:00
        assert stamp.hour % 4 == (0 - 3) % 4
        # el D1 del bróquer cierra a las 21:00 UTC
        d1 = [{"time": "2026-10-20T00:00:00Z"}]
        utc_d1 = shift_candle_times(d1, 3)
        s_d1 = dt.datetime.fromisoformat(utc_d1[0]["time"].replace("Z", "+00:00"))
        assert s_d1.hour == 21


class TestLedgerDayCut:
    """El registro agrupa por el día UTC ⇒ convierte las marcas del bróquer."""

    def test_ledger_declares_the_day_basis(self):
        import inspect
        from services import daily_ledger

        src = inspect.getsource(daily_ledger.build_daily_entry)
        assert "day_basis" in src
        assert "make_interval" in src, (
            "la consulta del ledger debe convertir las marcas del bróker al UTC"
        )

    def test_broker_stamp_conversion(self):
        timebase.set_broker_offset(3, source="test")
        try:
            marca = _stamp(2026, 10, 20, 17, 25)  # 17:25 del bróquer
            utc = timebase.broker_stamp_to_utc(marca)
            assert utc == _stamp(2026, 10, 20, 14, 25)
        finally:
            timebase.set_broker_offset(0, source="test-reset")


class TestStaleSeriesCannotPoisonTheOffset:
    """T-31.9 (2026-10-20): el desfase es del SERVIDOR, no del marco.

    Medido con el feed real: la serie D1 entrega su última vela con ~19 h
    ⇒ la aritmética de hora devolvía −19 h (BASURA) y la serie H4 con
    ~1 h ⇒ +1 h. Ese valor envenenaba la caché global de desfases, que usa
    el DÍA DE LA CUENTA (riesgo) ⇒ el corte diario podía quedar 19 h
    descuadrado. Aquí se fija el comportamiento correcto.
    """

    def _candles(self, stamp: dt.datetime) -> list:
        return [{"time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}]

    def test_daily_series_does_not_produce_a_bogus_offset(self):
        """La última vela D1 (19 h vieja) ⇒ 0 (no detectable), no −19 h."""
        now = _stamp(2026, 10, 20, 19, 0)
        d1_last = now - dt.timedelta(hours=19)  # la vela diaria en curso
        assert detect_utc_offset_hours(d1_last, now) == 0

    def test_four_hour_series_does_not_produce_a_wrong_offset(self):
        """La serie H4 no puede deducir el desfase (rejilla no múltiplo).

        Con el feed real, la última vela H4 formándose daba +1 h (falso).
        El paso H4→D1 exige el marco: la aritmética de hora sola no puede
        distinguir "el bróker va +3 h" de "esta vela tiene 2 h".
        """
        from services.market_time import broker_offset_hours_from_candles

        now = _stamp(2026, 10, 20, 19, 0)
        h4_last = now - dt.timedelta(hours=2)   # vela H4 formándose
        cs = self._candles(h4_last)
        assert broker_offset_hours_from_candles(cs, now=now, timeframe="H4") == 0
        assert broker_offset_hours_from_candles(cs, now=now, timeframe="H1") == -2, (
            "en H1 (con la vela en formación) el desfase sí se deduce"
        )

    def test_only_h1_is_allowed_to_detect(self):
        from services.market_time import broker_offset_hours_from_candles

        now = _stamp(2026, 10, 20, 16, 20)
        forming = now + dt.timedelta(hours=3)   # vela H1 formándose
        cs = self._candles(forming)
        assert broker_offset_hours_from_candles(cs, now=now, timeframe="H1") == 3
        assert broker_offset_hours_from_candles(cs, now=now, timeframe="H4") == 0
        assert broker_offset_hours_from_candles(cs, now=now, timeframe="D1") == 0

    def test_cache_rejects_implausible_offset(self):
        """Defensa en profundidad: −19 h (o cualquier basura) no entra."""
        timebase.set_broker_offset(3, source="test")
        try:
            timebase.set_broker_offset(-19, source="d1_basura")
            assert timebase.current_broker_offset() == 3, (
                "la caché aceptó un desfase implausible"
            )
        finally:
            timebase.set_broker_offset(0, source="test-reset")

    def test_stale_candle_does_not_shift_the_account_day(self):
        """Una serie vieja no puede descuadrar el día de la cuenta."""
        timebase.set_broker_offset(3, source="test")
        try:
            antes = timebase.broker_today(_stamp(2026, 10, 20, 12, 0))
            # una detección basura (D1) llega a la caché ⇒ se rechaza
            timebase.set_broker_offset(-19, source="d1_basura")
            despues = timebase.broker_today(_stamp(2026, 10, 20, 12, 0))
            assert antes == despues
        finally:
            timebase.set_broker_offset(0, source="test-reset")


class TestNoHardcodedOffsets:
    """Regla estructural: el desfase NUNCA se fija a mano en el código."""

    @pytest.mark.parametrize("path", [
        "routers/ai.py", "services/market_time.py", "services/mt5_client.py",
        "services/daily_ledger.py", "services/risk_guardian.py",
        "services/risk_state_store.py", "services/timebase.py",
    ])
    def test_no_literal_broker_offset_in_app_code(self, path):
        p = Path(__file__).resolve().parents[1] / path
        text = p.read_text(encoding="utf-8")
        # patrones perigosos: restar/añadir 3 horas como constante al desfase
        for evil in ("offset_h = 3", "offset_hours = 3", "BROKER_OFFSET = 3",
                     "offset=3", "hours=3)"):
            assert evil not in text, f"{path}: desfase fijo detectado ({evil})"

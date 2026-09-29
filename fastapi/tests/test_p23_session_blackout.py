"""
P2-3: calendario económico con blackout para el PPO + etiquetas de sesión.

- `is_news_blackout` (mismo mecanismo que el SMC) corta `/predict` ANTES de
  cualquier llamada a MT5: ventana ±news_blackout_minutes alrededor de un
  evento High/Medium de la divisa → HOLD con reason visible (nunca una orden).
- `_classify_session` trabaja sobre la hora UTC VERDADERA (el feed sella la
  hora del broker con "Z"): la tarde de NY no se etiqueta "Asia" ni la
  madrugada "Weekend/Closed" (entre semana el mercado abre 24h).
- `utc_hour_from_candle` detecta el desfase del servidor por ciclo
  (auto-corrector ante DST o si el feed arregla el sellado) y no inventa
  husos para fixtures históricas/sintéticas (sin tz → hora tal cual).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config import settings
from services.market_time import detect_utc_offset_hours, utc_hour_from_candle
from services.news_filter import is_news_blackout
from services.qwen_client import _classify_session


class TestClassifySessionUTC:
    def test_hour_boundaries_utc(self):
        assert _classify_session(0) == "Asia"
        assert _classify_session(6) == "Asia"
        assert _classify_session(7) == "London"
        assert _classify_session(11) == "London"
        assert _classify_session(12) == "London/NY Overlap"
        assert _classify_session(15) == "London/NY Overlap"
        assert _classify_session(16) == "New York"
        assert _classify_session(20) == "New York"
        assert _classify_session(21) == "Sydney"
        assert _classify_session(23) == "Sydney"

    def test_regression_labels_wrong_before_p23(self):
        # La versión anterior trataba la hora del broker como UTC y rotulaba:
        # 17-23 → "Asia" (es la tarde de Nueva York) y 23-7 → "Weekend/Closed"
        # (entre semana Sydney/Tokio, no fin de semana).
        assert _classify_session(18) == "New York"
        assert _classify_session(3) == "Asia"
        # "NY" plano pasa a nombrar la ventana real (solape Londres/NY).
        assert _classify_session(13) == "London/NY Overlap"
        # Ninguna hora de la semana cae en "Closed".
        assert all(_classify_session(h) != "Weekend/Closed" for h in range(24))


class TestUtcHourFromCandle:
    # 09:57 UTC reales; feed broker GMT+3 sella "12:00Z" (vela formándose).
    NOW = datetime(2026, 9, 29, 9, 57, tzinfo=timezone.utc)

    def test_broker_server_stamp_corrected(self):
        candle = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        assert detect_utc_offset_hours(candle, now=self.NOW) == 3
        assert utc_hour_from_candle(candle, now=self.NOW) == 9

    def test_honest_utc_stamp_untouched(self):
        # Si el feed pasa a sellar UTC real, el desfase detectado es 0.
        candle = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
        assert detect_utc_offset_hours(candle, now=self.NOW) == 0
        assert utc_hour_from_candle(candle, now=self.NOW) == 9

    def test_proper_timezone_converted_without_inventing_offset(self):
        candle = datetime(2026, 9, 29, 11, 0, tzinfo=timezone(timedelta(hours=2)))
        assert utc_hour_from_candle(candle, now=self.NOW) == 9

    def test_naive_historical_stamp_used_as_is(self):
        # Fixtures/backtests: sin tz no hay reloj de broker — hora tal cual.
        candle = datetime(2024, 1, 1, 18, 30)
        assert detect_utc_offset_hours(candle, now=self.NOW) == 0
        assert utc_hour_from_candle(candle, now=self.NOW) == 18

    def test_old_aware_stamp_outside_window_no_offset(self):
        candle = datetime(2024, 1, 1, 18, 30, tzinfo=timezone.utc)
        assert detect_utc_offset_hours(candle, now=self.NOW) == 0
        assert utc_hour_from_candle(candle, now=self.NOW) == 18

    def test_broker_hour_rolls_over_midnight(self):
        # 01:00 broker GMT+3 → 22:00 UTC del día anterior.
        candle = datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
        now = datetime(2026, 9, 29, 22, 10, tzinfo=timezone.utc)
        assert utc_hour_from_candle(candle, now=now) == 22


class TestIsNewsBlackoutWindow:
    """Ventana ±news_blackout_minutes: el mecanismo que ahora also protege al PPO."""

    @pytest.fixture(autouse=True)
    def _pin_window(self, monkeypatch):
        monkeypatch.setattr(settings, "news_filter_enabled", True)
        monkeypatch.setattr(settings, "news_blackout_minutes", 15)

    @staticmethod
    def _event(currency: str, delta: timedelta, title: str = "CPI y/y") -> dict:
        return {"title": title, "currency": currency,
                "time_utc": datetime.now(timezone.utc) + delta}

    @pytest.mark.asyncio
    async def test_event_for_symbol_currency_inside_window(self, monkeypatch):
        monkeypatch.setattr(
            "services.news_filter._fetch_calendar",
            AsyncMock(return_value=[self._event("USD", timedelta(minutes=10), "NFP")]),
        )
        blacked_out, event = await is_news_blackout("EURUSD")
        assert blacked_out is True
        assert event == "NFP"

    @pytest.mark.asyncio
    async def test_other_currency_does_not_block(self, monkeypatch):
        monkeypatch.setattr(
            "services.news_filter._fetch_calendar",
            AsyncMock(return_value=[self._event("GBP", timedelta(minutes=0))]),
        )
        assert await is_news_blackout("EURUSD") == (False, None)

    @pytest.mark.asyncio
    async def test_outside_window_does_not_block(self, monkeypatch):
        monkeypatch.setattr(
            "services.news_filter._fetch_calendar",
            AsyncMock(return_value=[self._event("USD", timedelta(minutes=60))]),
        )
        assert await is_news_blackout("EURUSD") == (False, None)

    @pytest.mark.asyncio
    async def test_disabled_filter_never_fetches(self, monkeypatch):
        monkeypatch.setattr(settings, "news_filter_enabled", False)
        fetch = AsyncMock(side_effect=AssertionError("no debe consultar el calendario"))
        monkeypatch.setattr("services.news_filter._fetch_calendar", fetch)
        assert await is_news_blackout("EURUSD") == (False, None)
        fetch.assert_not_awaited()


class TestBlackoutWiringContract:
    """Colocación en routers/ai.py: blackout antes de MT5 y hora vía conversor UTC."""

    @staticmethod
    def _src() -> str:
        return (Path(__file__).parents[1] / "routers" / "ai.py").read_text()

    def test_blackout_checked_before_mt5_fetch(self):
        src = self._src()
        assert src.index("is_news_blackout(sym)") < src.index("market/candles/latest")

    def test_session_hour_comes_from_utc_converter(self):
        src = self._src()
        assert "hour=utc_hour_from_candle(" in src
        assert "int(pd.to_datetime(raw_df['time'].iloc[-1]).hour)" not in src

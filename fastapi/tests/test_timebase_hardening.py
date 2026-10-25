"""P1.1a — hardening del reloj del bróker (TDD, 2026-10-25; actualizado B6.2).

Contrato que debe cumplir `broker_offset_hours(symbol, market_port)`:

  · una cotización de más de 5 min no es una fuente de reloj;
  · una vela H1 de más de 2 h (mercado abierto / feed vivo) no lo es;
  · quote y H1 deben coincidir en el offset (dominios compatibles);
  · un fallo de persistencia no valida, no cachea y no persiste;
  · si no hay fuente admisible, se conserva el último offset conocido y
    `validated` no se convierte en verdadero por accidente.

Estos tests NO fijan el offset ni el calendario DST: solo el contrato de
aceptación/rechazo de fuentes. Usan un `MarketDataPort` ficticio en lugar de
parchear httpx (B6.2: timebase no consulta al broker directamente).
"""
import datetime as dt
from unittest.mock import AsyncMock

import pytest

from services import broker_clock_store, timebase


class _FakeMarketPort:
    """MarketDataPort ficticio: devuelve price/candles configurados por test."""

    def __init__(self, price_payload=None, candles=None):
        self.price_payload = price_payload or {}
        self.candles = candles or []
        self.calls = []

    async def get_price(self, symbol):
        self.calls.append(("price", symbol))
        return dict(self.price_payload)

    async def get_candles(self, symbol, timeframe, count):
        self.calls.append(("candles", symbol, timeframe, count))
        return list(self.candles)


def _price_payload(now: dt.datetime, offset_h: int, age_minutes: int = 0) -> dict:
    """Sello del bróker: `offset_h` adelantado y `age_minutes` de retraso."""
    stamp = now + dt.timedelta(hours=offset_h) - dt.timedelta(minutes=age_minutes)
    return {"time": stamp.isoformat(), "bid": 1.1, "ask": 1.1002}


def _candle_payload(now: dt.datetime, offset_h: int) -> dict:
    """Vela H1 formándose con sello broker (floor(hora real) + offset)."""
    stamp = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=offset_h)
    return {"time": stamp.isoformat(), "open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1}


@pytest.fixture(autouse=True)
def _clean_clock(monkeypatch):
    monkeypatch.setattr(timebase, "_OFFSET_CACHE", {"offset_h": 0, "ts": 0.0})
    monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {
        "offset_h": 0,
        "source": "sin_detecccion",
        "validated": False,
        "persisted": False,
    })


@pytest.mark.asyncio
async def test_quote_older_than_five_minutes_is_not_a_clock_source(monkeypatch):
    """Una cotización de 30 min con offset aparente plausible NO valida."""
    now = dt.datetime.now(dt.timezone.utc)
    port = _FakeMarketPort(price_payload=_price_payload(now, offset_h=3, age_minutes=30),
                           candles=[])  # sin vela fresca ⇒ solo la cotización vieja
    persist = AsyncMock()
    monkeypatch.setattr(broker_clock_store, "persist_broker_offset", persist)

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 0, "la cotización vieja se usó como reloj"
    assert not timebase.broker_offset_is_validated()
    assert timebase._OFFSET_CACHE == {"offset_h": 0, "ts": 0.0}
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_quote_and_h1_disagree_do_not_validate(monkeypatch):
    """Dos fuentes con offsets distintos son incompatibles: no se elige una."""
    now = dt.datetime.now(dt.timezone.utc)
    port = _FakeMarketPort(price_payload=_price_payload(now, offset_h=3),
                           candles=[_candle_payload(now, 2)])
    persist = AsyncMock()
    monkeypatch.setattr(broker_clock_store, "persist_broker_offset", persist)

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 0
    assert not timebase.broker_offset_is_validated()
    assert timebase._OFFSET_CACHE == {"offset_h": 0, "ts": 0.0}
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_quote_and_h1_agree_validate_and_persist(monkeypatch):
    """Control positivo: fuentes frescas y coincidentes sí validan."""
    now = dt.datetime.now(dt.timezone.utc)
    port = _FakeMarketPort(price_payload=_price_payload(now, offset_h=3),
                           candles=[_candle_payload(now, 3)])

    async def _persist(offset_h, source):
        timebase.mark_broker_offset_persisted(offset_h)

    persist = AsyncMock(side_effect=_persist)
    monkeypatch.setattr(broker_clock_store, "persist_broker_offset", persist)

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 3
    assert timebase.broker_offset_is_validated()
    assert timebase.broker_offset_is_persisted()
    assert timebase.broker_clock_ready()
    assert timebase._OFFSET_CACHE["offset_h"] == 3
    persist.assert_awaited_once()
    assert persist.await_args.args[0] == 3


@pytest.mark.asyncio
async def test_h1_older_than_two_hours_is_rejected():
    """Frontera explícita: 2 h 05 min de antigüedad no es una medición."""
    now = dt.datetime.now(dt.timezone.utc)
    stale = (now - dt.timedelta(hours=2, minutes=5)).isoformat()
    port = _FakeMarketPort(price_payload={"time": now.isoformat()},
                           candles=[{"time": stale}])

    assert timebase._offset_from_candles([{"time": stale}], now) is None


@pytest.mark.asyncio
async def test_persistence_failure_does_not_validate_or_cache(monkeypatch):
    """Un fallo de DB no puede dejar el offset validado ni en caché."""
    now = dt.datetime.now(dt.timezone.utc)
    port = _FakeMarketPort(price_payload=_price_payload(now, offset_h=3),
                           candles=[_candle_payload(now, 3)])
    monkeypatch.setattr(
        broker_clock_store,
        "persist_broker_offset",
        AsyncMock(side_effect=RuntimeError("db caída")),
    )

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 0
    assert not timebase.broker_offset_is_validated()
    assert not timebase.broker_offset_is_persisted()
    assert timebase._OFFSET_CACHE == {"offset_h": 0, "ts": 0.0}


@pytest.mark.asyncio
async def test_persistence_failure_keeps_previous_validated_offset(monkeypatch):
    """Si ya había un offset válido, un fallo de DB no lo sustituye."""
    now = dt.datetime.now(dt.timezone.utc)
    timebase.set_broker_offset(3, source="endpoint_precio")
    timebase.mark_broker_offset_persisted(3)
    monkeypatch.setattr(timebase, "_OFFSET_CACHE", {"offset_h": 3, "ts": 0.0})
    port = _FakeMarketPort(price_payload=_price_payload(now, offset_h=2),
                           candles=[_candle_payload(now, 2)])
    monkeypatch.setattr(
        broker_clock_store,
        "persist_broker_offset",
        AsyncMock(side_effect=RuntimeError("db caída")),
    )

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 3
    assert timebase.current_broker_offset() == 3
    assert timebase.broker_offset_source() == "endpoint_precio"
    assert timebase._OFFSET_CACHE == {"offset_h": 3, "ts": 0.0}


@pytest.mark.asyncio
async def test_market_closed_stale_sources_do_not_raise_or_validate(monkeypatch):
    """Mercado cerrado: fuentes viejas ⇒ se conserva el conocido, sin error."""
    now = dt.datetime.now(dt.timezone.utc)
    port = _FakeMarketPort(
        price_payload=_price_payload(now, offset_h=3, age_minutes=40 * 60),
        candles=[_candle_payload(now - dt.timedelta(hours=3), 0)],
    )
    persist = AsyncMock()
    monkeypatch.setattr(broker_clock_store, "persist_broker_offset", persist)

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 0
    assert not timebase.broker_offset_is_validated()
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_market_port_without_candles_preserves_known_offset(monkeypatch):
    """Si el puerto no devuelve velas, se conserva el offset conocido."""
    now = dt.datetime.now(dt.timezone.utc)
    timebase.set_broker_offset(3, source="velas_h1")
    timebase.mark_broker_offset_persisted(3)
    monkeypatch.setattr(timebase, "_OFFSET_CACHE", {"offset_h": 3, "ts": 0.0})
    port = _FakeMarketPort(price_payload=_price_payload(now, offset_h=3), candles=[])

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 3


@pytest.mark.asyncio
async def test_market_port_without_price_uses_candle_offset(monkeypatch):
    """Sin precio pero con H1 válido, se usa el offset de las velas."""
    now = dt.datetime.now(dt.timezone.utc)
    port = _FakeMarketPort(price_payload={}, candles=[_candle_payload(now, 3)])

    async def _persist(offset_h, source):
        timebase.mark_broker_offset_persisted(offset_h)

    persist = AsyncMock(side_effect=_persist)
    monkeypatch.setattr(broker_clock_store, "persist_broker_offset", persist)

    offset = await timebase.broker_offset_hours("EURUSD", port)

    assert offset == 3

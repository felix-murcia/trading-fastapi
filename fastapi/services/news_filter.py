"""
Filtro de noticias fundamentales.

Descarga el calendario económico semanal (Forex Factory via faireconomy mirror)
y bloquea operaciones en una ventana configurable alrededor de noticias de alto impacto.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from config import settings

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_CACHE_TTL = 4 * 3600

CURRENCY_TO_SYMBOLS: dict[str, list[str]] = {
    "USD": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD", "USDCNH", "USDSEK", "XAUUSD"],
    "EUR": ["EURUSD"],
    "GBP": ["GBPUSD"],
    "AUD": ["AUDUSD"],
    "NZD": ["NZDUSD"],
    "JPY": ["USDJPY"],
    "CHF": ["USDCHF"],
    "CAD": ["USDCAD"],
    "CNH": ["USDCNH"],
    "SEK": ["USDSEK"],
    "XAU": ["XAUUSD"],
}

_cache: list[dict] = []
_cache_ts: float = 0


async def _fetch_calendar() -> list[dict]:
    global _cache, _cache_ts

    if time.time() - _cache_ts < _CACHE_TTL and _cache:
        return _cache

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(CALENDAR_URL)
            r.raise_for_status()
            events = r.json()

        high_impact = []
        for ev in events:
            if ev.get("impact") not in ("High", "Medium"):
                continue
            try:
                raw = ev["date"]
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
                high_impact.append({
                    "title": ev.get("title", ""),
                    "currency": ev.get("country", ""),
                    "time_utc": dt.astimezone(timezone.utc),
                })
            except (ValueError, KeyError) as exc:
                logger.warning("[NEWS] Skipping malformed event: %s — %s", ev, exc)

        _cache = high_impact
        _cache_ts = time.time()
        logger.info("[NEWS] Loaded %d high-impact events for this week", len(high_impact))
        return high_impact

    except Exception as exc:
        logger.error("[NEWS] Failed to fetch calendar: %s", exc)
        return _cache


def _symbol_currencies(symbol: str) -> set[str]:
    currencies = set()
    for curr, symbols in CURRENCY_TO_SYMBOLS.items():
        if symbol in symbols:
            currencies.add(curr)
    return currencies


def _current_h1_candle(now: datetime) -> tuple[datetime, datetime]:
    """Devuelve (candle_start, candle_end) de la vela H1 que contiene 'now'."""
    start = now.replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=1)


async def is_news_blackout(symbol: str) -> tuple[bool, str | None]:
    """True + event title si hay una noticia de alto impacto en la vela H1 actual.

    Lógica: si la noticia cae en [candle_start, candle_end), toda la vela queda
    bloqueada. Ejemplo: noticia a las 15:45 → bloquea desde las 15:00 hasta las 16:00.
    """
    if not settings.news_filter_enabled:
        return False, None

    events = await _fetch_calendar()
    now = datetime.now(timezone.utc)
    candle_start, candle_end = _current_h1_candle(now)
    currencies = _symbol_currencies(symbol)

    for ev in events:
        if ev["currency"] not in currencies:
            continue
        if candle_start <= ev["time_utc"] < candle_end:
            return True, ev["title"]

    return False, None


async def get_upcoming_news(symbols: list[str], lookahead_minutes: int | None = None) -> list[dict]:
    """Eventos de alto impacto en la vela H1 actual que afectan a los símbolos dados.

    lookahead_minutes se ignora — la unidad de tiempo es la vela H1 completa.
    """
    events = await _fetch_calendar()
    now = datetime.now(timezone.utc)
    candle_start, candle_end = _current_h1_candle(now)

    result = []
    for ev in events:
        if not (candle_start <= ev["time_utc"] < candle_end):
            continue
        affected = CURRENCY_TO_SYMBOLS.get(ev["currency"], [])
        matching = [s for s in symbols if s in affected]
        if not matching:
            continue
        delta = ev["time_utc"] - now
        result.append({
            "title": ev["title"],
            "currency": ev["currency"],
            "time_utc": ev["time_utc"].isoformat(),
            "minutes_until": round(delta.total_seconds() / 60, 1),
            "affected_symbols": matching,
        })

    return result

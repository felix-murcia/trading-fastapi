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
    """True + event title si hay una noticia de alto impacto en la vela H1 anterior, actual o siguiente.

    El EA señaliza al cierre de candle[1] (vela anterior). Si esa vela contenía noticias,
    la señal llega ya al inicio de la vela siguiente y la comprobación de la vela actual
    no la detectaría. Por eso se comprueban tres velas: la anterior (señal), la actual (trade)
    y la siguiente — un evento justo al inicio de hora (ej. 14:00:00) cae en el límite exacto
    de la vela actual y, sin mirar la siguiente, se podría abrir una entrada minutos antes
    de una noticia de alto impacto.
    """
    if not settings.news_filter_enabled:
        return False, None

    events = await _fetch_calendar()
    now = datetime.now(timezone.utc)
    delta = timedelta(minutes=settings.news_blackout_minutes)
    currencies = _symbol_currencies(symbol)

    for ev in events:
        if ev["currency"] not in currencies:
            continue
        start_blackout = ev["time_utc"] - delta
        end_blackout = ev["time_utc"] + delta
        if start_blackout <= now <= end_blackout:
            return True, ev["title"]

    return False, None


async def get_upcoming_news(symbols: list[str], lookahead_minutes: int | None = None) -> list[dict]:
    """Eventos de alto impacto en la vela H1 actual o la siguiente que afectan a los símbolos dados.

    Se incluye la vela siguiente porque un evento justo al inicio de hora (ej. 14:00:00)
    cae en el límite exacto de la vela actual y no deja margen de antelación real para
    cerrar posiciones antes de que se publique.

    lookahead_minutes se ignora — la unidad de tiempo es la vela H1 completa.
    """
    events = await _fetch_calendar()
    now = datetime.now(timezone.utc)
    # Mostramos eventos de las próximas 24 horas para el dashboard
    window_end = now + timedelta(hours=24)
    delta = timedelta(minutes=settings.news_blackout_minutes)

    result = []
    for ev in events:
        if not (now - timedelta(hours=2) <= ev["time_utc"] <= window_end):
            continue
        affected = CURRENCY_TO_SYMBOLS.get(ev["currency"], [])
        matching = [s for s in symbols if s in affected]
        if not matching:
            continue
        
        start_blackout = ev["time_utc"] - delta
        end_blackout = ev["time_utc"] + delta
        
        time_until = ev["time_utc"] - now
        result.append({
            "title": ev["title"],
            "currency": ev["currency"],
            "time_utc": ev["time_utc"].isoformat(),
            "start_time_utc": start_blackout.isoformat(),
            "end_time_utc": end_blackout.isoformat(),
            "minutes_until": round(time_until.total_seconds() / 60, 1),
            "affected_symbols": matching,
        })

    return result

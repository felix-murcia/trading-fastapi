"""
Cliente HTTP para el MetaTrader MCP Server.

Reemplaza mt5_socket.py (TCP raw) manteniendo la misma interfaz pública
para que el resto del código no cambie.

MCP Server: http://<MT5_HTTP_URL>/api/v1/...
"""

import datetime as dt

import httpx
import logging
from config import settings

logger = logging.getLogger(__name__)

SYMBOLS = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "XAUUSD"]


async def _get(path: str, params: dict | None = None) -> any:
    async with httpx.AsyncClient(timeout=settings.mt5_http_timeout) as c:
        r = await c.get(settings.mt5_http_url + path, params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict) -> any:
    async with httpx.AsyncClient(timeout=settings.mt5_http_timeout) as c:
        r = await c.post(settings.mt5_http_url + path, json=body)
        r.raise_for_status()
        return r.json()


async def _delete(path: str) -> any:
    async with httpx.AsyncClient(timeout=settings.mt5_http_timeout) as c:
        r = await c.delete(settings.mt5_http_url + path)
        r.raise_for_status()
        return r.json()


# ── Interfaz pública (misma que mt5_socket.py) ─────────────────────────────────

async def get_account() -> dict:
    data = await _get("/api/v1/account/info")
    return {
        "equity":   data.get("equity",   0.0),
        "balance":  data.get("balance",  0.0),
        "currency": data.get("currency", "USD"),
    }


async def get_prices() -> dict:
    result = {}
    for sym in SYMBOLS:
        data = await _get(f"/api/v1/market/price/{sym}")
        result[sym] = float(data.get("bid", 0.0))
    return result


async def get_price(symbol: str) -> dict:
    """Payload completo del endpoint de precio (incluye `time` del servidor).

    Usado por `timebase.broker_offset_hours` para medir el offset del bróker
    sin consultar httpx directamente desde la base de tiempos.
    """
    data = await _get(f"/api/v1/market/price/{symbol}")
    return data if isinstance(data, dict) else {}


async def get_spread(symbol: str) -> float:
    data = await _get(f"/api/v1/market/price/{symbol}")
    return float(data.get("ask", 0.0)) - float(data.get("bid", 0.0))


async def get_candles_with_offset(symbol: str, timeframe: str, count: int):
    """Velas normalizadas a UTC real + el desfase conocido del bróker.

    T-31 (2026-10-20): el `mt5_time` del audit lo manda el EA y está en el
    RELOJ DEL BROKER (+3 h medidos), NO en UTC real. Las velas del MCP
    también venían en reloj de broker. Al normalizar SÓLO las velas, la
    medición quedaba desalineada 3 h.

    Aquí se devuelven AMBAS cosas: las velas ya en UTC real y el desfase
    aplicado, para que el consumidor alinee sus marcas (mt5_time − offset).

    B6.1 (2026-10-25): esta función NO detecta el desfase — eso lo hace
    `services.timebase.broker_offset_hours()` a través del puerto de mercado.
    Aquí solo se aplica el offset ya conocido (`timebase.current_broker_offset()`);
    si aún no se ha detectado, el desfase es 0 (sin corrección).
    """
    import httpx
    from config import settings
    from services.market_time import correct_candle_epoch
    from services import timebase

    async with httpx.AsyncClient(timeout=settings.mt5_http_timeout) as c:
        r = await c.get(
            settings.mt5_http_url + "/api/v1/market/candles/latest",
            params={"symbol_name": symbol, "timeframe": timeframe, "count": count},
        )
        r.raise_for_status()
        data = r.json()
    raw = data if isinstance(data, list) else data.get("candles", [])

    def _to_ts(t) -> float:
        if isinstance(t, (int, float)):
            return float(t)
        return dt.datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()

    offset_h = timebase.current_broker_offset()
    candles = sorted(
        (
            {
                "time": correct_candle_epoch(_to_ts(x.get("time", 0)), offset_h),
                "open": float(x.get("open", 0)),
                "high": float(x.get("high", 0)),
                "low": float(x.get("low", 0)),
                "close": float(x.get("close", 0)),
                "volume": float(x.get("tick_volume", x.get("volume", 0))),
            }
            for x in raw
        ),
        key=lambda c: c["time"],
    )
    return candles, offset_h


async def get_candles(symbol: str, timeframe: str, count: int) -> list[dict]:
    data = await _get("/api/v1/market/candles/latest", params={
        "symbol_name": symbol, "timeframe": timeframe, "count": count,
    })
    candles = data if isinstance(data, list) else data.get("candles", [])
    return await normalize_candles(candles, symbol, timeframe)


async def get_deals() -> list[dict]:
    """Historial de cierres (deals) del bróker.

    Usado por el endpoint de analítica SMC para el PnL diario.
    """
    data = await _get("/api/v1/history/deals")
    return data if isinstance(data, list) else []


async def normalize_candles(candles: list, symbol: str = "EURUSD",
                            timeframe: str = "H1") -> list[dict]:
    """Velas → dicts con el sello en UTC REAL y orden ascendente.

    T-31 (2026-10-19): el feed sella las velas con el reloj del BROKER
    (+3 h medidos) marking them "Z" ⇒ cualquier emparejamiento con marcas
    del EA/DB (UTC real) quedaba desplazado 3 h. Se corrige aplicando el
    desfase ya conocido (`timebase.current_broker_offset()`); si aún no se
    ha detectado, el desfase es 0 (sin corrección, B6.1).
    """
    from services.market_time import correct_candle_epoch
    from services import timebase

    def _to_ts(t) -> int:
        if isinstance(t, (int, float)):
            return int(t)
        from datetime import datetime
        return int(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())

    offset_h = timebase.current_broker_offset()
    if offset_h:
        logger.warning(
            "[MT5] Sello de velas con desfase broker→UTC de %+d h → normalizado a UTC real",
            offset_h,
        )
    # P0: el MCP devuelve las velas DESCENDENTES (más nueva primero) — la
    # convención de todo el sistema es ascendente (iloc[-1]=actual, tail=recientes).
    return sorted((
        {
            "time":   int(correct_candle_epoch(_to_ts(c.get("time", 0)), offset_h)),
            "open":   float(c.get("open",  0)),
            "high":   float(c.get("high",  0)),
            "low":    float(c.get("low",   0)),
            "close":  float(c.get("close", 0)),
            "volume": float(c.get("tick_volume", c.get("volume", 0))),
        }
        for c in candles
    ), key=lambda c: c["time"])


async def get_positions() -> dict:
    open_pos = await _get("/api/v1/positions")
    pending  = await _get("/api/v1/order/pending")
    return {
        "open":    _normalize_positions(open_pos),
        "pending": _normalize_positions(pending),
    }


async def place_order(symbol: str, order_type: str, volume: float,
                      price: float, sl: float, tp: float,
                      comment: str = "") -> dict:
    body = {
        "symbol":      symbol,
        "type":        order_type,
        "volume":      volume,
        "stop_loss":   sl,
        "take_profit": tp,
    }
    result = await _post("/api/v1/order/market", body)
    data = result.get("data", [])
    ticket = data[2] if isinstance(data, list) and len(data) > 2 else result.get("id", result.get("ticket", 0))
    return {"ticket": ticket}


async def place_order_paper(symbol: str, order_type: str, volume: float,
                            price: float, sl: float, tp: float,
                            comment: str = "") -> dict:
    """Simula una orden sin ejecutarla en MT5. Para Paper Trading."""
    import random
    fake_ticket = random.randint(900000, 999999)
    logger.warning("[PAPER-MODE] Orden simulada: %s %s %.2f @ %.5f | SL=%.5f TP=%.5f | ticket=%d",
                   order_type, symbol, volume, price, sl, tp, fake_ticket)
    return {"ticket": fake_ticket, "paper": True, "symbol": symbol,
            "volume": volume, "entry": price, "sl": sl, "tp": tp,
            "comment": comment}


async def get_order(ticket: int) -> dict:
    return await _get(f"/api/v1/order/pending/{ticket}")


async def cancel_order(ticket: int) -> dict:
    return await _delete(f"/api/v1/order/pending/{ticket}")


async def get_candle_open(symbol: str, timeframe: str = "H1") -> float:
    """Devuelve el precio de apertura de la vela actual."""
    candles = await get_candles(symbol, timeframe, 1)
    if not candles:
        raise ValueError(f"No se pudo obtener vela actual para {symbol}")
    return candles[0]["open"]


# P2-4b: `close_positions_by_symbol` (cerrar TODO el símbolo) fue eliminado —
# los cierres pasan por `close_position(ticket)` con ledger de propiedad
# (services/position_ownership.py); nada cierra posiciones ajenas.


async def close_position(ticket: int) -> dict:
    """P2-4b: cierra UNA posición por ticket (cierre selectivo por propiedad)."""
    return await _delete(f"/api/v1/positions/{ticket}")


# ── Normalización ──────────────────────────────────────────────────────────────

def _normalize_positions(raw: list) -> list[dict]:
    """Mapea campos del MCP server al esquema interno."""
    out = []
    for p in (raw or []):
        out.append({
            "ticket":  p.get("id",          p.get("ticket", 0)),
            "symbol":  p.get("symbol",       ""),
            "type":    p.get("type",         ""),
            "volume":  float(p.get("volume", 0)),
            "price":   float(p.get("open",   p.get("price_open", p.get("price", 0)))),
            "sl":      float(p.get("stop_loss",   p.get("sl", 0))),
            "tp":      float(p.get("take_profit",  p.get("tp", 0))),
            "profit":  float(p.get("profit", 0)),
            "state":   p.get("state", ""),
        })
    return out

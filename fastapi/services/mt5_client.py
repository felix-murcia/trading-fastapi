"""
Cliente HTTP para el MetaTrader MCP Server.

Reemplaza mt5_socket.py (TCP raw) manteniendo la misma interfaz pública
para que el resto del código no cambie.

MCP Server: http://<MT5_HTTP_URL>/api/v1/...
"""

import httpx
import logging
from config import settings

logger = logging.getLogger(__name__)

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"]


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


async def get_candles(symbol: str, timeframe: str, count: int) -> list[dict]:
    data = await _get("/api/v1/market/candles/latest", params={
        "symbol_name": symbol, "timeframe": timeframe, "count": count,
    })
    candles = data if isinstance(data, list) else data.get("candles", [])
    def _to_ts(t) -> int:
        if isinstance(t, (int, float)):
            return int(t)
        from datetime import datetime, timezone
        try:
            return int(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0

    return [
        {
            "time":   _to_ts(c.get("time", 0)),
            "open":   float(c.get("open",  0)),
            "high":   float(c.get("high",  0)),
            "low":    float(c.get("low",   0)),
            "close":  float(c.get("close", 0)),
            "volume": float(c.get("tick_volume", c.get("volume", 0))),
        }
        for c in candles
    ]


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
        "symbol":     symbol,
        "order_type": order_type,
        "volume":     volume,
        "price":      price,
        "stop_loss":  sl,
        "take_profit": tp,
        "comment":    comment,
    }
    result = await _post("/api/v1/order/pending", body)
    return {"ticket": result.get("id", result.get("ticket", 0))}


async def get_order(ticket: int) -> dict:
    return await _get(f"/api/v1/order/pending/{ticket}")


async def cancel_order(ticket: int) -> dict:
    return await _delete(f"/api/v1/order/pending/{ticket}")


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

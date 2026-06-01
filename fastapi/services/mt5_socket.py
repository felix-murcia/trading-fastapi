"""
Cliente TCP al EA de MetaTrader 5.

Protocolo: JSON delimitado por newline.
  Request:  {"cmd": "...", ...args}\n
  Response: {"ok": true, "data": {...}}\n  |  {"ok": false, "error": "..."}\n

El EA es el servidor (escucha en MT5_SOCKET_HOST:MT5_SOCKET_PORT).
Esta clase es el cliente (conecta en startup, reconecta si cae).

Comandos soportados (los implementa el EA en MQL5):
  get_account     → { equity, balance, currency }
  get_prices      → { EURUSD, GBPUSD, USDJPY, USDCHF }  (bid prices)
  get_candles     → { symbol, timeframe, count } → [{ time, open, high, low, close, volume }]
  get_positions   → { open: [...], pending: [...] }
  place_order     → { symbol, type, price, sl, tp, volume, hmac_sig } → { ticket }
  get_order       → { ticket } → { ticket, symbol, type, price, sl, tp, volume, state }
  cancel_order    → { ticket } → { cancelled: true }
"""

import asyncio
import json
import logging
from config import settings

logger = logging.getLogger(__name__)

_reader: asyncio.StreamReader | None = None
_writer: asyncio.StreamWriter | None = None
_lock = asyncio.Lock()


async def connect() -> None:
    global _reader, _writer
    try:
        _reader, _writer = await asyncio.wait_for(
            asyncio.open_connection(settings.mt5_socket_host, settings.mt5_socket_port),
            timeout=settings.mt5_socket_timeout,
        )
        logger.info("MT5 socket conectado: %s:%d", settings.mt5_socket_host, settings.mt5_socket_port)
    except Exception as e:
        logger.error("MT5 socket: fallo al conectar: %s", e)
        _reader = None
        _writer = None
        raise


async def _reconnect() -> None:
    global _reader, _writer
    if _writer:
        try:
            _writer.close()
            await _writer.wait_closed()
        except Exception:
            pass
    _reader = None
    _writer = None
    await connect()


async def send(cmd: str, **kwargs) -> dict:
    """Envía un comando al EA y devuelve el campo `data` de la respuesta."""
    async with _lock:
        if _writer is None or _writer.is_closing():
            await _reconnect()

        payload = json.dumps({"cmd": cmd, **kwargs}, separators=(",", ":")) + "\n"
        try:
            _writer.write(payload.encode())
            await asyncio.wait_for(_writer.drain(), timeout=settings.mt5_socket_timeout)
            raw = await asyncio.wait_for(
                _reader.readline(), timeout=settings.mt5_socket_timeout
            )
        except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError) as e:
            logger.warning("MT5 socket: error en envío (%s), reconectando", e)
            await _reconnect()
            raise

        response = json.loads(raw.decode().strip())
        if not response.get("ok"):
            raise RuntimeError(f"MT5 error: {response.get('error', 'unknown')}")
        return response["data"]


async def disconnect() -> None:
    global _writer
    if _writer:
        _writer.close()
        try:
            await _writer.wait_closed()
        except Exception:
            pass
    _writer = None


# ── Helpers de alto nivel ──────────────────────────────────────────────────────

async def get_account() -> dict:
    return await send("get_account")


async def get_prices() -> dict:
    return await send("get_prices")


async def get_candles(symbol: str, timeframe: str, count: int) -> list[dict]:
    data = await send("get_candles", symbol=symbol, timeframe=timeframe, count=count)
    return data["candles"]


async def get_positions() -> dict:
    return await send("get_positions")


async def place_order(payload: dict) -> dict:
    return await send("place_order", **payload)


async def get_order(ticket: int) -> dict:
    return await send("get_order", ticket=ticket)


async def cancel_order(ticket: int) -> dict:
    return await send("cancel_order", ticket=ticket)

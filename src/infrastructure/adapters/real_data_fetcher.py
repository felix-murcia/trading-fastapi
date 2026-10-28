"""RealDataFetcher: adaptador real del puerto DataFetcher.

Obtiene datos de entrenamiento:
  - fetch_trade_outcomes: desde PostgreSQL (asyncpg) tabla trade_outcomes.
  - fetch_candles: desde MT5 MCP vía httpx (timeframe H1 por defecto).

En tests se hace monkeypatch de ``db.connection.get_pool`` y de
``httpx.AsyncClient.get`` para no tocar servicios reales.

Implementa DataFetcher por typing estructural:
  - fetch_trade_outcomes(symbol, since=None) -> list[TradeOutcome]
  - fetch_candles(symbol, count, timeframe="H1") -> list
"""

import httpx
from datetime import datetime

from src.domain.ports import DataFetcher
from src.domain.rules.trade_outcome import TradeOutcome
from config import settings


class RealDataFetcher(DataFetcher):
    def __init__(self, mt5_http_url: str | None = None, pool=None):
        self._mt5_http_url = mt5_http_url or settings.mt5_http_url
        # `pool` se inyecta en tests para no depender de asyncpg real.
        self._pool = pool

    async def _pool_conn(self):
        from db.connection import get_pool

        return self._pool or get_pool()

    # ── fetch_trade_outcomes ────────────────────────────────────────────

    async def fetch_trade_outcomes(
        self, symbol: str, since: float | None = None
    ) -> list:
        """Devuelve trade_outcomes para `symbol` como lista de dicts.

        Cada dict tiene las claves de TradeOutcome para poder construirlo.
        """
        pool = await self._pool_conn()
        query = """
            SELECT symbol, entry_time, exit_time, pnl, pnl_pct, direction,
                   sl_hit, tp_hit, exit_reason
            FROM trade_outcomes
            WHERE symbol = $1
              AND entry_time > TIMESTAMPTZ '2000-01-01'
              AND exit_time > entry_time
        """
        params = [symbol]
        if since is not None:
            query += " AND exit_time > $2"
            params.append(datetime.fromtimestamp(since))
        query += " ORDER BY exit_time"

        rows = await pool.fetch(query, *params)
        return [
            {
                "symbol": str(row["symbol"]),
                "entry_time": row["entry_time"].timestamp(),
                "exit_time": row["exit_time"].timestamp(),
                "pnl": float(row["pnl"]),
                "pnl_pct": float(row["pnl_pct"]),
                "direction": str(row["direction"]).upper(),
                "sl_hit": bool(row["sl_hit"]),
                "tp_hit": bool(row["tp_hit"]),
                "exit_reason": str(row["exit_reason"]),
            }
            for row in rows
        ]

    # ── fetch_candles ───────────────────────────────────────────────────

    async def fetch_candles(
        self, symbol: str, count: int, timeframe: str = "H1"
    ) -> list:
        """Obtiene velas históricas de MT5 MCP (async via httpx)."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.get(
                f"{self._mt5_http_url}/api/v1/market/candles/latest",
                params={
                    "symbol_name": symbol,
                    "timeframe": timeframe,
                    "count": count,
                },
            )
            res.raise_for_status()
            data = res.json()
        candles = data if isinstance(data, list) else data.get("candles", [])
        return sorted(candles, key=lambda c: c.get("time", 0))

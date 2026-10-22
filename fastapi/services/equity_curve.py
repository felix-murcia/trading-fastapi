"""
CURVA DE EQUITY MARK-TO-MARKET — el DD deja de ser una cifra narrada.

Por qué (2026-10-21): el DD del 30,2 % publicado el 20-10 era
*mark-to-market* (incluía el flotante de las posiciones abiertas) y no tenía
serie almacenada que lo reprodujera; el reconstruido sólo con cierres daba
15,49 %. La diferencia es justo el flotante que se ve en el valle y no en el
cierre. Mientras el valle no se guarde, el número conservador no se puede
auditar.

Solución estructural: una foto cada 60 s de la cuenta (equity, balance y
flotante) en `equity_snapshots`. Con esa serie, `get_dd()` recalcula el
drawdown desde el pico con las marcas propias del sistema. No decide nada:
sólo mide. El trading no depende de este bucle.

Nota: la serie empieza a guardarse AHORA. No reconstruye el pasado; el DD
del 20-10 sigue siendo una cifra narrada y así se declara en el README.
"""
import asyncio
import datetime as dt
import logging

import httpx

from config import settings
from db.connection import get_pool
from services.error_taxonomy import DB_ERRORS

logger = logging.getLogger(__name__)

EQUITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id       BIGSERIAL PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    equity   NUMERIC(12,2) NOT NULL,
    balance  NUMERIC(12,2) NOT NULL,
    floating NUMERIC(12,2) NOT NULL,
    source   TEXT NOT NULL DEFAULT 'mt5_account_info'
);

CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ts
    ON equity_snapshots(ts);
"""


async def init_equity_table() -> None:
    """Crea la tabla si no existe (el schema.sql sólo corre en volumen nuevo)."""
    pool = get_pool()
    await pool.execute(EQUITY_SCHEMA)
    logger.info("[EQUITY] Tabla equity_snapshots lista")


async def record_snapshot() -> dict:
    """Lee la cuenta de MT5 y guarda una foto. Propaga el fallo al bucle."""
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(settings.mt5_http_url + "/api/v1/account/info")
        r.raise_for_status()
        data = r.json()

    equity = float(data.get("equity", data.get("balance", 0.0)))
    balance = float(data.get("balance", 0.0))
    if equity <= 0:
        # Una cuenta sin equity no es una foto válida: no se inventa un 0.
        raise ValueError(f"account/info sin equity válido: {data!r}")

    floating = equity - balance
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO equity_snapshots (equity, balance, floating, source)
        VALUES ($1, $2, $3, $4)
        RETURNING ts, equity, balance, floating
        """,
        equity,
        balance,
        floating,
        "mt5_account_info",
    )
    return dict(row) if row else {
        "ts": dt.datetime.now(dt.timezone.utc),
        "equity": equity,
        "balance": balance,
        "floating": floating,
    }


def compute_dd(equities: list[float]) -> dict:
    """Drawdown máximo desde el pico, con marcas propias (no con cierres).

    Devuelve el pico y el valle del PEOR tramo, no el pico global y el valle
    global: un valle anterior a un pico posterior no es drawdown de ese pico.
    """
    peak = None
    peak_at_max = None
    trough_at_max = None
    max_dd = 0.0
    for value in equities:
        eq = float(value)
        if peak is None or eq > peak:
            peak = eq
        if peak and peak > 0:
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
                peak_at_max = peak
                trough_at_max = eq
    return {
        "n": len(equities),
        "max_dd_pct": round(max_dd, 4),
        "peak_eur": round(peak_at_max, 2) if peak_at_max is not None else None,
        "trough_eur": round(trough_at_max, 2) if trough_at_max is not None else None,
    }


async def get_dd(since: dt.datetime | None = None) -> dict:
    """DD de la serie guardada (opcionalmente desde `since`). Sólo lectura."""
    pool = get_pool()
    if since is not None:
        rows = await pool.fetch(
            "SELECT equity FROM equity_snapshots WHERE ts >= $1 ORDER BY ts", since
        )
    else:
        rows = await pool.fetch("SELECT equity FROM equity_snapshots ORDER BY ts")
    return compute_dd([row["equity"] for row in rows])


async def _equity_curve_loop(interval_s: int = 60) -> None:
    """Una foto por minuto; el bucle nunca muere y el fallo se registra."""
    await asyncio.sleep(120)  # no competir con el arranque (baseline de riesgo)
    while True:
        try:
            await record_snapshot()
        except DB_ERRORS as exc:  # noqa: BLE001 — el registrador nunca muere
            logger.warning("[EQUITY] no se pudo guardar la foto: %s", exc)
        await asyncio.sleep(interval_s)

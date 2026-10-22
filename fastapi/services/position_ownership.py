r"""
P2-4b: propiedad de posiciones — el cierre selectivo por ledger de tickets.

¿De quién es una posición abierta en un símbolo? El MCP no expone magic ni
comment en /positions (auditado contra su openapi), así que el magic no puede
decidirlo (magic=0 además es compartido con trades manuales). La fuente de
verdad es NUESTRO ledger `orders`: el motor SMC retirado (2026-10-22) dejó sus
órdenes con `cycle_id LIKE 'smc\_%'` y su ticket en `orders.mt5_order_id`;
ese ledger se sigue leyendo para no tocar nunca las posiciones del EA.

Por eso ningún cierre toca ya `DELETE /positions/symbol/{symbol}` entero:
- El EA (magic 90001) abre por `/v1/orders/prepare` con cycle_id distinto — su
  posición es OTRO mundo y solo él (o su SL/TP) la cierra.
- Las rutas SMC (cierre manual, noticias) cierran SOLO los tickets del
  ledger. "Mundos totalmente independientes" también en el cierre:
  una estrategia no puede borrar la posición de otra.

`close_positions_owned` no registra trades: qué cerrar/persistir es decisión
de cada caller (el endpoint /close sí alimenta auto-retrain).
"""
import logging

from db.connection import get_pool
from services import mt5_client

logger = logging.getLogger(__name__)

SMC_PREFIX = "smc_"


def _ledger_prefix_pattern(prefix: str) -> str:
    r"""Patrón LIKE con el prefijo escapado — `smc_` no puede virar wildcard
    (`_` = cualquier carácter en LIKE): 'smc\_%' = literal 'smc_' + resto."""
    escaped = prefix.replace("\\", "\\\\").replace("_", "\\_")
    return escaped + "%"


async def owned_positions(
    symbol: str, candidates: list[dict], *, cycle_prefix: str = SMC_PREFIX
) -> tuple[list[dict], list[dict]]:
    """Particiona `candidates` (posiciones abiertas de `symbol`) en
    (propias, ajenas) según el ledger `orders`.

    Sin candidatos no hay nada que cerrar → no consulta la BD.
    """
    if not candidates:
        return [], []
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT mt5_order_id FROM orders
           WHERE symbol = $1
             AND cycle_id LIKE $2 ESCAPE '\\'
             AND status IN ('placed', 'filled')
             AND mt5_order_id IS NOT NULL AND mt5_order_id <> ''""",
        symbol, _ledger_prefix_pattern(cycle_prefix),
    )
    mine = {str(r["mt5_order_id"]) for r in rows}
    owned = [p for p in candidates if str(p.get("ticket")) in mine]
    foreign = [p for p in candidates if str(p.get("ticket")) not in mine]
    if foreign:
        logger.warning(
            "[OWNERSHIP] %s: %d posición(es) ajenas NO se cierran (otra estrategia): tickets=%s",
            symbol, len(foreign), [p.get("ticket") for p in foreign],
        )
    return owned, foreign


async def close_positions_owned(
    symbol: str, candidates: list[dict], *, cycle_prefix: str = SMC_PREFIX
) -> dict:
    """Cierra SOLO las posiciones de `candidates` propiedad de esta estrategia.

    Devuelve:
      {"closed": [tickets], "foreign": [tickets], "failed": [{"ticket","error"}]}

    - Un ticket que no cierra NO aborta el resto (se loguea cada fallo).
    - Sin silencios: todo fallo queda en `failed` y en log con el error.
    """
    owned, foreign = await owned_positions(
        symbol, candidates, cycle_prefix=cycle_prefix
    )
    closed: list[int] = []
    failed: list[dict] = []
    for p in owned:
        ticket = p.get("ticket")
        try:
            await mt5_client.close_position(ticket)
            closed.append(ticket)
        except Exception as exc:
            failed.append({"ticket": ticket, "error": f"{type(exc).__name__}: {exc}"})
            logger.error("[OWNERSHIP] No se pudo cerrar %s ticket=%s: %s",
                         symbol, ticket, exc)
    if owned:
        logger.info(
            "[OWNERSHIP] %s: cerradas %d/%d propias (ajenas=%d, fallos=%d)",
            symbol, len(closed), len(owned), len(foreign), len(failed),
        )
    return {
        "closed": closed,
        "foreign": [p.get("ticket") for p in foreign],
        "failed": failed,
    }

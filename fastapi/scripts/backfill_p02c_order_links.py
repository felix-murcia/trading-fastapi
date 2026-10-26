#!/usr/bin/env python3
"""
P0.2c — Backfill de enlaces order_db_id en trade_outcomes.

Vincula cada outcome sin order_db_id a su orden exacta en `orders` usando:
  outcome.position_id  -> orders.mt5_position_id
  outcome.symbol       -> orders.symbol
  outcome.direction    -> orders.type  (LONG→BUY, SHORT→SELL)

Reglas de aceptación (P0.2c):
  · 0 joins temporales: solo enlaces exactos por posición+símbolo+dirección
  · Si hay exactamente una coincidencia: se actualiza order_db_id
  · Si hay cero o múltiples: no se toca, razón documentada
  · Filas legacy excluidas con motivo documentado
  · No silent failures: errores se propagan, nunca se imputan valores por defecto

Uso:
    python3 scripts/backfill_p02c_order_links.py   (dentro del contenedor /app)
"""

import asyncio
import sys
from typing import Any

sys.path.insert(0, "/app")

from db.connection import init_pool, close_pool, get_pool  # noqa: E402

SYMBOLS = {"EURUSD", "GBPUSD", "USDCHF"}


def _positive_id(value: Any) -> int | None:
    """Devuelve el id positivo o None si es inválido (bool, 0, negativo, str no numérico, None)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        iv = int(value)
        return iv if iv > 0 else None
    return None


def _direction_to_order_type(direction: str | None) -> str | None:
    """Canonicaliza la dirección del outcome al type de la tabla orders."""
    if direction is None:
        return None
    d = str(direction).strip().upper()
    if d == "LONG":
        return "BUY"
    if d == "SHORT":
        return "SELL"
    if d in ("BUY", "SELL"):
        return d
    return None


def _match_outcome_to_order(outcome: dict[str, Any], orders: list[dict[str, Any]]) -> tuple[int | None, str]:
    """
    Intenta enlazar un outcome a una orden exacta.

    Devuelve (order_id, reason) donde:
      - order_id es el id de la orden si hay exactamente una coincidencia exacta
      - reason documenta por qué se enlazó o por qué no
    """
    position_id = _positive_id(outcome.get("position_id"))
    if position_id is None:
        return None, "missing_or_invalid_position_id"

    symbol = outcome.get("symbol")
    if symbol not in SYMBOLS:
        return None, "outcome_symbol_outside_universe"

    order_type = _direction_to_order_type(outcome.get("direction"))
    if order_type is None:
        return None, "outcome_direction_invalid"

    matches = [
        o for o in orders
        if _positive_id(o.get("mt5_position_id")) == position_id
        and o.get("symbol") == symbol
        and o.get("type") == order_type
    ]

    if len(matches) == 0:
        return None, "no_order_for_position_symbol_direction"
    if len(matches) > 1:
        return None, "multiple_orders_for_position_symbol_direction"
    return matches[0]["id"], "exact_position_symbol_direction_match"


def _summarize_backfill(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrega los resultados del backfill en un resumen contable."""
    total = len(results)
    linked = sum(1 for r in results if r.get("linked"))
    not_linked = total - linked
    by_reason: dict[str, int] = {}
    for r in results:
        reason = r.get("reason", "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "total_outcomes_without_fk": total,
        "linked": linked,
        "not_linked": not_linked,
        "by_reason": by_reason,
    }


async def backfill_order_links(pool) -> dict[str, Any]:
    """
    Ejecuta el backfill de order_db_id en trade_outcomes.

    1. Lee todos los outcomes con order_db_id IS NULL.
    2. Lee todas las órdenes de la tabla orders.
    3. Para cada outcome sin FK, intenta matchear exactamente (posición+símbolo+tipo).
    4. Si hay exactamente una coincidencia, UPDATE trade_outcomes SET order_db_id = <order.id>.
    5. Devuelve el resumen de enlaces realizados y no realizados.

    No modifica outcomes que ya tienen order_db_id.
    No usa joins temporales.
    """
    # Outcomes sin order_db_id
    outcomes_rows = await pool.fetch(
        """
        SELECT id, symbol, direction, entry_time, exit_time, pnl,
               deal_ticket, position_id, order_db_id
        FROM trade_outcomes
        WHERE order_db_id IS NULL
        ORDER BY id
        """
    )
    outcomes = [dict(r) for r in outcomes_rows]

    # Todas las órdenes (candidatas para enlazar)
    orders_rows = await pool.fetch(
        """
        SELECT id, symbol, type, mt5_position_id
        FROM orders
        """
    )
    orders = [dict(r) for r in orders_rows]

    results: list[dict[str, Any]] = []

    for outcome in outcomes:
        outcome_id = outcome["id"]
        order_id, reason = _match_outcome_to_order(outcome, orders)

        if order_id is not None:
            await pool.execute(
                """
                UPDATE trade_outcomes
                SET order_db_id = $1::bigint
                WHERE id = $2::bigint AND order_db_id IS NULL
                """,
                order_id,
                outcome_id,
            )
            results.append({
                "outcome_id": outcome_id,
                "linked": True,
                "order_id": order_id,
                "reason": reason,
            })
        else:
            results.append({
                "outcome_id": outcome_id,
                "linked": False,
                "order_id": None,
                "reason": reason,
            })

    return _summarize_backfill(results)


async def main() -> int:
    await init_pool()
    pool = get_pool()
    try:
        summary = await backfill_order_links(pool)
        print(f"[P0.2c] Backfill completado:")
        print(f"  total sin FK: {summary['total_outcomes_without_fk']}")
        print(f"  enlazados:    {summary['linked']}")
        print(f"  no enlazados: {summary['not_linked']}")
        print(f"  por razón:    {summary['by_reason']}")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

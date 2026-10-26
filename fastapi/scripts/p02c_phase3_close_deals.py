#!/usr/bin/env python3
"""
P0.2c Fase 3: Vincular los 3 outcomes con position_id pero sin orden,
y poblar position_id para los 121 outcomes huérfanos usando deals de cierre de MT5.

Estrategia:
  1. Obtener TODOS los deals de MT5 (entrada y cierre)
  2. Para los 3 outcomes con position_id: buscar deal de cierre con ese position_id,
     obtener order ticket, buscar orden por mt5_order_id
  3. Para los 121 outcomes sin position_id: buscar deal de cierre por deal_ticket,
     obtener position_id, luego buscar orden por mt5_order_id
  4. Después, ejecutar backfill P0.2c para vincular order_db_id

Uso:
    python3 scripts/p02c_phase3_close_deals.py   (dentro del contenedor /app)
"""

import asyncio
import sys
from typing import Any

sys.path.insert(0, "/app")

from core.container import build_registry_from_settings
from db.connection import init_pool, close_pool, get_pool
from services.error_taxonomy import LOOP_ERRORS

SYMBOLS = ("EURUSD", "GBPUSD", "USDCHF")


def _parse_positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            iv = int(value)
            return iv if iv > 0 else None
        except (ValueError, TypeError):
            return None
    return None


async def main() -> int:
    await init_pool()
    pool = get_pool()
    registry = build_registry_from_settings()

    # ── Paso 1: Obtener TODOS los deals de MT5 ──
    all_deals: dict[str, list[dict]] = {}
    for sym in SYMBOLS:
        ctx = registry.get(sym)
        if ctx is None or ctx.market is None:
            print(f"[P0.2c-F3] ERROR: sin puerto para {sym}")
            await close_pool()
            return 1
        try:
            deals = await ctx.market.get_deals()
        except LOOP_ERRORS as exc:
            print(f"[P0.2c-F3] ERROR obteniendo deals de {sym}: {exc}")
            await close_pool()
            return 1
        all_deals[sym] = deals
        entry_deals = [d for d in deals if d.get("entry") == 0]
        close_deals = [d for d in deals if d.get("entry") in (1, 2)]
        print(f"[P0.2c-F3] {sym}: {len(deals)} deals totales, {len(entry_deals)} entrada, {len(close_deals)} cierre")

    # ── Paso 2: Los 3 outcomes con position_id pero sin orden ──
    outcomes_with_pos = await pool.fetch(
        """
        SELECT id, symbol, direction, position_id, deal_ticket, entry_time, exit_time, pnl
        FROM trade_outcomes
        WHERE order_db_id IS NULL
          AND position_id IS NOT NULL
          AND position_id > 0
        ORDER BY id
        """
    )
    print(f"\n[P0.2c-F3] Outcomes con position_id pero sin orden: {len(outcomes_with_pos)}")

    for r in outcomes_with_pos:
        outcome = dict(r)
        outcome_id = outcome["id"]
        sym = outcome["symbol"]
        pos_id = outcome["position_id"]
        deal_ticket = outcome.get("deal_ticket")

        print(f"\n[P0.2c-F3] Outcome {outcome_id}: {sym} position_id={pos_id} deal_ticket={deal_ticket}")

        # Buscar deal de cierre con este position_id
        deals = all_deals.get(sym, [])
        matching_close_deals = [
            d for d in deals
            if d.get("entry") in (1, 2)
            and _parse_positive_id(d.get("position_id")) == pos_id
        ]

        if not matching_close_deals:
            print(f"  SIN deal de cierre con position_id={pos_id}")
            continue

        print(f"  {len(matching_close_deals)} deal(es) de cierre con position_id={pos_id}")

        for deal in matching_close_deals:
            order_ticket = _parse_positive_id(deal.get("order"))
            if order_ticket is None:
                print(f"  Deal {deal.get('ticket')} sin order ticket válido")
                continue
            print(f"  Deal de cierre ticket={deal.get('ticket')} order={order_ticket} price={deal.get('price')} entry={deal.get('entry')}")

            # Buscar orden por mt5_order_id
            order_row = await pool.fetchrow(
                """
                SELECT id, symbol, type, mt5_order_id, mt5_position_id, status
                FROM orders
                WHERE mt5_order_id = $1::text
                  AND symbol = $2
                  AND status = 'filled'
                """,
                str(order_ticket),
                sym,
            )
            if order_row:
                order = dict(order_row)
                print(f"  → Orden encontrada: id={order['id']} mt5_order_id={order['mt5_order_id']} mt5_position_id={order['mt5_position_id']}")
                if order["mt5_position_id"] is None:
                    # Poblar mt5_position_id desde el deal
                    await pool.execute(
                        """
                        UPDATE orders
                        SET mt5_position_id = $1
                        WHERE id = $2 AND mt5_position_id IS NULL
                        """,
                        pos_id,
                        order["id"],
                    )
                    print(f"  → Poblando mt5_position_id={pos_id} en orden {order['id']}")
            else:
                print(f"  → No hay orden con mt5_order_id={order_ticket}")

    # ── Paso 3: Los 121 outcomes sin position_id ──
    outcomes_without_pos = await pool.fetch(
        """
        SELECT id, symbol, direction, deal_ticket, entry_time, exit_time, pnl
        FROM trade_outcomes
        WHERE order_db_id IS NULL
          AND (position_id IS NULL OR position_id = 0)
        ORDER BY id
        """
    )
    print(f"\n[P0.2c-F3] Outcomes sin position_id: {len(outcomes_without_pos)}")

    for r in outcomes_without_pos:
        outcome = dict(r)
        outcome_id = outcome["id"]
        sym = outcome["symbol"]
        deal_ticket = outcome.get("deal_ticket")

        if deal_ticket is None:
            print(f"  Outcome {outcome_id}: sin deal_ticket, sin position_id — no vinculable")
            continue

        # Buscar deal por deal_ticket
        deals = all_deals.get(sym, [])
        matching_deals = [
            d for d in deals
            if _parse_positive_id(d.get("ticket")) == _parse_positive_id(deal_ticket)
        ]

        if not matching_deals:
            print(f"  Outcome {outcome_id}: deal_ticket={deal_ticket} no encontrado en deals MT5")
            continue

        deal = matching_deals[0]
        pos_id = _parse_positive_id(deal.get("position_id"))
        order_ticket = _parse_positive_id(deal.get("order"))

        print(f"  Outcome {outcome_id}: deal_ticket={deal_ticket} → position_id={pos_id} order={order_ticket} entry={deal.get('entry')}")

        if pos_id is None:
            print(f"    → Sin position_id en deal, no se puede poblar")
            continue

        # Buscar orden por mt5_order_id
        if order_ticket is not None:
            order_row = await pool.fetchrow(
                """
                SELECT id, symbol, type, mt5_order_id, mt5_position_id, status
                FROM orders
                WHERE mt5_order_id = $1::text
                  AND symbol = $2
                  AND status = 'filled'
                """,
                str(order_ticket),
                sym,
            )
            if order_row:
                order = dict(order_row)
                print(f"    → Orden: id={order['id']} mt5_position_id={order['mt5_position_id']}")
                if order["mt5_position_id"] is None:
                    await pool.execute(
                        """
                        UPDATE orders
                        SET mt5_position_id = $1
                        WHERE id = $2 AND mt5_position_id IS NULL
                        """,
                        pos_id,
                        order["id"],
                    )
                    print(f"    → Poblando mt5_position_id={pos_id} en orden {order['id']}")
            else:
                print(f"    → No hay orden con mt5_order_id={order_ticket}")
        else:
            print(f"    → Deal sin order ticket válido")

    await close_pool()
    print("\n[P0.2c-F3] Fase 3 completada. Ejecutar backfill P0.2c para vincular order_db_id.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

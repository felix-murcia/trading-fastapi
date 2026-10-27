#!/usr/bin/env python3
"""
P0.2c Fase 3b — Vinculación final de outcomes pendientes usando deals de cierre del MCP de MT5.

Flujo completo (protocolo P0.2c: 0 temporary joins, match exacto por posición+símbolo+dirección):
  1. Obtener TODOS los deals de MT5 vía get_deals() (/api/v1/history/deals)
  2. Para los 3 outcomes con position_id pero sin orden:
     - Buscar deal de cierre con ese position_id → obtener order ticket
     - Verificar que existe orden con ese mt5_order_id (misma symbol)
     - Poblar mt5_position_id en esa orden si falta
  3. Para los 121 outcomes sin position_id (huérfanos):
     - Buscar deal de cierre por deal_ticket → obtener position_id
     - Poblar position_id en el outcome desde el deal
     - Poblar mt5_position_id en la orden correspondiente si existe
  4. Ejecutar backfill P0.2c completo: matchear outcome.position_id → orders.mt5_position_id
     + outcome.symbol → orders.symbol + outcome.direction → orders.type
     (exacto, 1 sola coincidencia)
  5. UPDATE trade_outcomes.order_db_id para cada match exacto único

Reglas de aceptación:
  · Match exacto por posición+símbolo+dirección (ningún join temporal ni heurístico)
  · Si hay exactamente una coincidencia: se actualiza order_db_id
  · Si hay cero o múltiples: no se toca, razón documentada
  · No silent failures: errores se propagan

Uso:
    python3 scripts/p02c_phase3b_close_deals_final.py   (dentro del contenedor /app)
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
    """Devuelve el id positivo o None si es inválido."""
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


def _direction_to_order_type(direction: str | None) -> str | None:
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


async def main() -> int:
    await init_pool()
    pool = get_pool()
    registry = build_registry_from_settings()

    # ── Paso 1: Obtener TODOS los deals de MT5 ──
    print("[P0.2c-F3b] Obteniendo deals de cierre del MCP de MT5...")
    all_close_deals: dict[str, list[dict]] = {}
    for sym in SYMBOLS:
        ctx = registry.get(sym)
        if ctx is None or ctx.market is None:
            print(f"[P0.2c-F3b] ERROR: sin puerto de mercado para {sym}")
            await close_pool()
            return 1
        try:
            deals = await ctx.market.get_deals()
        except LOOP_ERRORS as exc:
            print(f"[P0.2c-F3b] ERROR obteniendo deals de {sym}: {exc}")
            await close_pool()
            return 1
        close_deals = [d for d in deals if d.get("entry") in (1, 2)]
        all_close_deals[sym] = close_deals
        print(f"  {sym}: {len(deals)} deals totales, {len(close_deals)} cierre")

    # ── Paso 2: Los 3 outcomes con position_id pero sin orden ──
    outcomes_with_pos = await pool.fetch(
        """
        SELECT id, symbol, direction, position_id, deal_ticket
        FROM trade_outcomes
        WHERE order_db_id IS NULL
          AND position_id IS NOT NULL
          AND position_id > 0
        ORDER BY id
        """
    )
    print(f"\n[P0.2c-F3b] Outcomes con position_id pero sin order_db_id: {len(outcomes_with_pos)}")

    for r in outcomes_with_pos:
        outcome = dict(r)
        outcome_id = outcome["id"]
        sym = outcome["symbol"]
        pos_id = outcome["position_id"]

        print(f"\n[P0.2c-F3b] Outcome {outcome_id}: {sym} position_id={pos_id}")

        # Buscar deal de cierre con este position_id
        deals = all_close_deals.get(sym, [])
        matching_close_deals = [
            d for d in deals
            if _parse_positive_id(d.get("position_id")) == pos_id
        ]

        if not matching_close_deals:
            print(f"  SIN deal de cierre con position_id={pos_id} (no vinculable vía MCP)")
            continue

        deal = matching_close_deals[0]
        order_ticket = _parse_positive_id(deal.get("order"))
        print(f"  Deal de cierre ticket={deal.get('ticket')} order={order_ticket} entry={deal.get('entry')} price={deal.get('price')}")

        if order_ticket is None:
            print(f"  Deal sin order ticket válido")
            continue

        # Verificar que existe orden con ese mt5_order_id (misma symbol, filled)
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

            # Poblar mt5_position_id en la orden si falta
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
                print(f"  → Poblando mt5_position_id={pos_id} en orden {order['id']}")
        else:
            print(f"  → No hay orden con mt5_order_id={order_ticket} para {sym}")

    # ── Paso 3: Los outcomes sin position_id (huérfanos) ──
    outcomes_without_pos = await pool.fetch(
        """
        SELECT id, symbol, direction, deal_ticket
        FROM trade_outcomes
        WHERE order_db_id IS NULL
          AND (position_id IS NULL OR position_id = 0)
        ORDER BY id
        """
    )
    print(f"\n[P0.2c-F3b] Outcomes sin position_id (huérfanos): {len(outcomes_without_pos)}")

    for r in outcomes_without_pos:
        outcome = dict(r)
        outcome_id = outcome["id"]
        sym = outcome["symbol"]
        deal_ticket = outcome.get("deal_ticket")

        if deal_ticket is None:
            print(f"  Outcome {outcome_id}: sin deal_ticket, sin position_id — NO vinculable")
            continue

        # Buscar deal por deal_ticket
        deals = all_close_deals.get(sym, [])
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

        # Poblar position_id en el outcome desde el deal
        if pos_id is not None:
            await pool.execute(
                """
                UPDATE trade_outcomes
                SET position_id = $1
                WHERE id = $2 AND position_id IS NULL
                """,
                pos_id,
                outcome_id,
            )
            print(f"  → Poblando position_id={pos_id} en outcome {outcome_id}")
        else:
            print(f"    → Sin position_id en deal, outcome sigue huérfano")
            continue

        # Poblar mt5_position_id en la orden si existe y falta
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

    # ── Paso 4: Backfill P0.2c completo (match exacto posición+símbolo+dirección) ──
    print("\n[P0.2c-F3b] Ejecutando backfill P0.2c (match exacto posición+símbolo+dirección)...")

    # Re-leer outcomes sin order_db_id (ahora algunos pueden tener position_id poblado)
    outcomes_rows = await pool.fetch(
        """
        SELECT id, symbol, direction, position_id
        FROM trade_outcomes
        WHERE order_db_id IS NULL
        ORDER BY id
        """
    )
    outcomes = [dict(r) for r in outcomes_rows]

    # Todas las órdenes candidatas
    orders_rows = await pool.fetch(
        """
        SELECT id, symbol, type, mt5_position_id
        FROM orders
        WHERE mt5_position_id IS NOT NULL
        """
    )
    orders = [dict(r) for r in orders_rows]

    linked = 0
    not_linked = 0
    reasons: dict[str, int] = {}

    for outcome in outcomes:
        outcome_id = outcome["id"]
        position_id = _parse_positive_id(outcome.get("position_id"))
        symbol = outcome.get("symbol")
        order_type = _direction_to_order_type(outcome.get("direction"))

        if position_id is None or symbol not in SYMBOLS or order_type is None:
            not_linked += 1
            reasons["missing_invalid_position_id_or_direction"] = reasons.get("missing_invalid_position_id_or_direction", 0) + 1
            continue

        matches = [
            o for o in orders
            if _parse_positive_id(o.get("mt5_position_id")) == position_id
            and o.get("symbol") == symbol
            and o.get("type") == order_type
        ]

        if len(matches) == 0:
            not_linked += 1
            reasons["no_order_for_position_symbol_direction"] = reasons.get("no_order_for_position_symbol_direction", 0) + 1
            continue

        if len(matches) > 1:
            not_linked += 1
            reasons["multiple_orders_for_position_symbol_direction"] = reasons.get("multiple_orders_for_position_symbol_direction", 0) + 1
            continue

        # Match exacto único
        order_id = matches[0]["id"]
        await pool.execute(
            """
            UPDATE trade_outcomes
            SET order_db_id = $1::bigint
            WHERE id = $2::bigint AND order_db_id IS NULL
            """,
            order_id,
            outcome_id,
        )
        linked += 1
        reasons["exact_position_symbol_direction_match"] = reasons.get("exact_position_symbol_direction_match", 0) + 1
        print(f"  → Outcome {outcome_id} ({symbol} {order_type} pos={position_id}) enlazado a orden {order_id}")

    # ── Paso 5: Resumen final ──
    total_after = await pool.fetchval(
        "SELECT COUNT(*) FROM trade_outcomes WHERE order_db_id IS NULL"
    )
    total_with = await pool.fetchval(
        "SELECT COUNT(*) FROM trade_outcomes WHERE order_db_id IS NOT NULL"
    )

    print("\n" + "=" * 60)
    print("[P0.2c-F3b] RESUMEN FINAL")
    print(f"  Outcomes con order_db_id:   {total_with}")
    print(f"  Outcomes sin order_db_id:   {total_after}")
    print(f"  Enlazados en esta ejecución: {linked}")
    print(f"  No enlazados:                {not_linked}")
    print(f"  Por razón:                   {reasons}")
    print("=" * 60)

    await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

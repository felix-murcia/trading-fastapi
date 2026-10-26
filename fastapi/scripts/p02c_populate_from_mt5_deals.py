#!/usr/bin/env python3
"""
P0.2c Fase 2: Poblar orders.mt5_order_id y orders.mt5_position_id desde deals de MT5.

Estrategia:
  1. Obtener todos los deals del MT5 MCP server (get_deals → /api/v1/history/deals)
  2. Filtrar deals de entrada (entry=0, DEAL_ENTRY_IN) por símbolo
  3. Para cada deal, buscar match EXACTO y ÚNICO con una orden en DB:
     - symbol == deal.symbol
     - type == BUY/SELL (según deal.type: 0=BUY, 1=SELL)
     - entry price: |deal.price - order.entry| <= PRICE_TOLERANCE (slippage mínimo)
     - volume == deal.volume (exacto)
     - mt5_order_id IS NULL (aún no poblado)
     - status = 'filled'
  4. Si match único y mt5_order_id/position_id válidos: poblar orders
  5. Si 0 o >1 matches: no tocar, razón documentada
  6. No silent failures: errores se propagan

Después de poblar, ejecutar backfill_p02c_order_links.py para vincular
trade_outcomes.order_db_id → orders.id vía mt5_position_id.

Uso:
    python3 scripts/p02c_populate_from_mt5_deals.py   (dentro del contenedor /app)
"""

import asyncio
import sys
from decimal import Decimal
from typing import Any

sys.path.insert(0, "/app")

from core.container import build_registry_from_settings
from db.connection import init_pool, close_pool, get_pool
from services.error_taxonomy import LOOP_ERRORS

# Tolerancia de precio para slippage (EURUSD: 0.00005 = medio pip)
PRICE_TOLERANCE = Decimal("0.00005")

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


def _deal_type_to_order_type(deal_type: Any) -> str | None:
    """Mapea deal.type de MT5 a orders.type."""
    if deal_type == 0:
        return "BUY"
    if deal_type == 1:
        return "SELL"
    return None


def _price_match(deal_price: Any, order_entry: Any) -> bool:
    """Compara precios con tolerancia para slippage."""
    try:
        dp = Decimal(str(deal_price))
        oe = Decimal(str(order_entry))
        return abs(dp - oe) <= PRICE_TOLERANCE
    except Exception:
        return False


def _volume_match(deal_volume: Any, order_volume: Any) -> bool:
    """Compara volúmenes exactamente."""
    try:
        return Decimal(str(deal_volume)) == Decimal(str(order_volume))
    except Exception:
        return False


async def main() -> int:
    await init_pool()
    pool = get_pool()

    # Registro de puertos de mercado
    registry = build_registry_from_settings()

    # ── Paso 1: Obtener deals de MT5 y filtrar entradas (entry=0) ──
    all_entry_deals: dict[str, list[dict]] = {}
    for sym in SYMBOLS:
        ctx = registry.get(sym)
        if ctx is None or ctx.market is None:
            print(f"[P0.2c-F2] ERROR: sin puerto de mercado para {sym}")
            await close_pool()
            return 1

        try:
            deals = await ctx.market.get_deals()
        except LOOP_ERRORS as exc:
            print(f"[P0.2c-F2] ERROR obteniendo deals de {sym}: {exc}")
            await close_pool()
            return 1

        entry_deals = [
            d for d in deals
            if d.get("symbol") == sym and d.get("entry") == 0
        ]
        all_entry_deals[sym] = entry_deals
        print(f"[P0.2c-F2] {sym}: {len(deals)} deals totales, {len(entry_deals)} de entrada (entry=0)")

    # ── Paso 2: Leer órdenes filled sin mt5_order_id ──
    orders_rows = await pool.fetch(
        """
        SELECT id, cycle_id, symbol, type, entry, volume, status, mt5_order_id, mt5_position_id
        FROM orders
        WHERE status = 'filled'
          AND mt5_order_id IS NULL
        ORDER BY id
        """
    )
    orders = [dict(r) for r in orders_rows]
    print(f"\n[P0.2c-F2] Órdenes filled sin mt5_order_id: {len(orders)}")

    # ── Paso 3: Matching y actualización ──
    updated = 0
    failed_no_match = 0
    failed_multi_match = 0
    failed_no_valid_ids = 0

    for order in orders:
        sym = order["symbol"]
        deals = all_entry_deals.get(sym, [])

        matches = []
        for deal in deals:
            deal_type = _deal_type_to_order_type(deal.get("type"))
            if deal_type != order["type"]:
                continue

            if not _price_match(deal.get("price"), order["entry"]):
                continue

            if not _volume_match(deal.get("volume"), order["volume"]):
                continue

            matches.append(deal)

        if len(matches) == 0:
            failed_no_match += 1
            print(f"[P0.2c-F2] NO_MATCH   order id={order['id']} {sym} {order['type']} entry={order['entry']} vol={order['volume']}")
            continue

        if len(matches) > 1:
            failed_multi_match += 1
            print(f"[P0.2c-F2] AMBIGUO  order id={order['id']} {sym}: {len(matches)} deals coincidentes (sin tocar)")
            continue

        # Match único
        deal = matches[0]
        mt5_order_id = _parse_positive_id(deal.get("order"))
        position_id = _parse_positive_id(deal.get("position_id"))

        if mt5_order_id is None or position_id is None:
            failed_no_valid_ids += 1
            print(f"[P0.2c-F2] INVALID_IDS order id={order['id']} {sym}: order={deal.get('order')} position_id={deal.get('position_id')}")
            continue

        try:
            await pool.execute(
                """
                UPDATE orders
                SET mt5_order_id = $1::text,
                    mt5_position_id = $2
                WHERE id = $3
                  AND mt5_order_id IS NULL
                """,
                str(mt5_order_id),
                position_id,
                order["id"],
            )
            updated += 1
            print(f"[P0.2c-F2] OK       order id={order['id']} {sym}: mt5_order_id={mt5_order_id} mt5_position_id={position_id}")
        except Exception as exc:
            print(f"[P0.2c-F2] ERROR    order id={order['id']}: {exc}")
            raise

    await close_pool()
    print(f"\n[P0.2c-F2] Completado:")
    print(f"  actualizados:    {updated}")
    print(f"  sin match:       {failed_no_match}")
    print(f"  ambiguos:        {failed_multi_match}")
    print(f"  sin IDs válidos: {failed_no_valid_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

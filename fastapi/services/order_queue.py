"""
Order Queue Service - Cola de órdenes offline/deferred.

Cuando MT5 está desconectado o las órdenes fallan, se guardan aquí.
Un background task las reprocesa cuando MT5 vuelve a estar disponible.
"""

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from db.connection import get_pool

logger = logging.getLogger(__name__)


class OrderQueueStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    PLACED = "placed"


async def enqueue_order(
    symbol: str,
    order_type: str,
    volume: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    comment: str = "",
    cycle_id: Optional[str] = None,
) -> int:
    """Guarda una orden en la cola para procesamiento diferido."""
    if cycle_id is None:
        cycle_id = str(uuid.uuid4())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO deferred_orders
                (cycle_id, symbol, order_type, volume, entry_price, stop_loss, take_profit, comment, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            cycle_id, symbol, order_type, volume, entry_price, stop_loss, take_profit, comment, OrderQueueStatus.PENDING.value,
        )
    logger.warning("[ORDER-QUEUE] Orden diferida guardada: %s %s %.2f @ %.5f (cycle=%s)", order_type, symbol, volume, entry_price, cycle_id)
    return row["id"]


async def get_pending_orders(limit: int = 50) -> list[dict]:
    """Obtiene órdenes pendientes ordenadas por antigüedad."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM deferred_orders
            WHERE status IN ('pending', 'failed')
              AND retry_count < 5
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def mark_order_processing(order_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE deferred_orders SET status = $1, updated_at = NOW() WHERE id = $2",
            OrderQueueStatus.PROCESSING.value, order_id,
        )


async def mark_order_placed(order_id: int, mt5_order_id: str = "") -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE deferred_orders SET status = $1, updated_at = NOW() WHERE id = $2",
            OrderQueueStatus.PLACED.value, order_id,
        )
    logger.info("[ORDER-QUEUE] Orden %d procesada exitosamente (MT5 ticket: %s)", order_id, mt5_order_id)


async def mark_order_failed(order_id: int, error: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE deferred_orders
            SET status = $1, retry_count = retry_count + 1, last_error = $2, updated_at = NOW()
            WHERE id = $3
            """,
            OrderQueueStatus.FAILED.value, error[:500], order_id,
        )
    logger.warning("[ORDER-QUEUE] Orden %d fallida: %s", order_id, error[:200])


async def purge_old_orders(hours: int = 168) -> int:
    """Elimina órdenes antiguas ya resueltas o con demasiados intentos."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM deferred_orders
            WHERE (status = 'placed' AND updated_at < NOW() - INTERVAL '%s hours')
               OR (status = 'failed' AND retry_count >= 5 AND updated_at < NOW() - INTERVAL '%s hours')
            """,
            hours, hours,
        )
    # result es "DELETE n"
    count = int(result.split()[-1]) if result != "DELETE 0" else 0
    if count > 0:
        logger.info("[ORDER-QUEUE] Limpiadas %d órdenes antiguas", count)
    return count

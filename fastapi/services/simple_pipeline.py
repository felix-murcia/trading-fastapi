"""
Flujo simple: señal del indicador → orden. Sin LLM.

Flujo:
1. Símbolo soportado
2. Precio presente en la señal
3. Cooldown en memoria: máximo 1 intento por símbolo cada signal_cooldown_minutes
4. Obtener apertura de vela H1 actual → calcular SL/TP anclados al inicio de vela
5. Cerrar posición abierta en el símbolo si la hay (señal contraria = flip)
6. Validación geométrica + registro en DB
7. Enviar orden a MT5
"""

import json
import logging
import time

from db.connection import get_pool
from models.orders import OrderPrepareRequest
from services import mt5_client, order_manager, position_sizing
from config import settings

logger = logging.getLogger(__name__)

# Cooldown en memoria: {symbol: timestamp_ultimo_intento}
_last_attempt: dict[str, float] = {}


async def _audit(cycle_id: str, event: str, data: dict) -> None:
    try:
        pool = get_pool()
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
            cycle_id, event, json.dumps(data),
        )
    except Exception as exc:
        logger.warning("[SIMPLE] audit_log failed: %s", exc)


async def process_signal(
    symbol: str, direction: str, signal_id: str | None, price: float | None,
) -> dict:
    """Procesa una señal buy/sell. Devuelve dict con el resultado (nunca lanza)."""
    # Incluir símbolo en el cycle_id: el Crystal reutiliza el mismo timestamp en distintos pares
    cycle_id = f"smc_{symbol}_{signal_id}" if signal_id else f"smc_{symbol}_{int(time.time())}"

    try:
        # 1. Símbolo soportado
        if not position_sizing.is_supported(symbol):
            return {"action": "skip", "reason": f"symbol_not_supported:{symbol}"}

        # 2. Precio presente en la señal
        if not price or price <= 0:
            await _audit(cycle_id, "simple_skip", {"reason": "no_price_in_signal", "symbol": symbol})
            return {"action": "skip", "reason": "no_price_in_signal"}

        # 3. Cooldown en memoria (cubre intentos rechazados, fallidos y exitosos)
        cooldown_secs = settings.signal_cooldown_minutes * 60
        last = _last_attempt.get(symbol, 0)
        if time.time() - last < cooldown_secs:
            return {"action": "skip", "reason": "cooldown_active"}
        _last_attempt[symbol] = time.time()

        # 4. Apertura de vela H1 actual → SL al inicio de vela, TP el doble
        candle_open = await mt5_client.get_candle_open(symbol, timeframe="H1")
        entry, sl, tp, volume = position_sizing.derive_order_from_candle_open(
            direction, symbol, price, candle_open
        )

        # 5. Cerrar posición abierta en el símbolo si la hay (flip de señal)
        pool = get_pool()
        try:
            positions = await mt5_client.get_positions()
            open_pos = [p for p in positions.get("open", []) if p["symbol"] == symbol]
            if open_pos:
                await mt5_client.close_positions_by_symbol(symbol)
                await _audit(cycle_id, "simple_position_closed", {
                    "symbol": symbol, "reason": "signal_flip", "direction": direction,
                    "closed_tickets": [p["ticket"] for p in open_pos],
                })
                logger.info("[SIMPLE] CLOSED %d position(s) on %s before %s entry",
                            len(open_pos), symbol, direction)
        except Exception as exc:
            logger.warning("[SIMPLE] close existing position failed %s: %s", symbol, exc)

        # 6. Validación + registro en DB
        prep = await order_manager.prepare(OrderPrepareRequest(
            cycle_id=cycle_id, symbol=symbol, type=direction.upper(),
            entry=entry, sl=sl, tp=tp, volume=volume,
        ))
        if not prep.approved:
            await _audit(cycle_id, "simple_rejected", {
                "symbol": symbol, "direction": direction, "reason": prep.rejection_reason,
            })
            return {"action": "skip", "reason": prep.rejection_reason}

        # 7. Enviar orden a MT5
        try:
            result = await mt5_client.place_order(
                symbol=symbol, order_type=direction.upper(), volume=volume,
                price=entry, sl=sl, tp=tp, comment=cycle_id,
            )
        except Exception as exc:
            await pool.execute(
                "UPDATE orders SET status='rejected' WHERE cycle_id=$1", cycle_id
            )
            await _audit(cycle_id, "simple_mt5_error", {"symbol": symbol, "error": str(exc)})
            return {"action": "error", "reason": f"mt5_rejected:{exc}"}

        ticket = str(result.get("ticket", ""))
        await pool.execute(
            "UPDATE orders SET mt5_order_id=$2, status='placed' WHERE cycle_id=$1",
            cycle_id, ticket,
        )
        await _audit(cycle_id, "simple_order_placed", {
            "symbol": symbol, "direction": direction, "entry": entry,
            "sl": sl, "tp": tp, "volume": volume, "ticket": ticket,
            "candle_open": candle_open,
        })
        logger.info("[SIMPLE] ORDER PLACED %s %s entry=%.5f sl=%.5f tp=%.5f vol=%.2f ticket=%s",
                    symbol, direction, entry, sl, tp, volume, ticket)
        return {"action": direction, "ticket": ticket, "entry": entry, "sl": sl, "tp": tp, "volume": volume}

    except Exception as exc:
        logger.error("[SIMPLE] pipeline failed %s %s: %s", symbol, direction, exc)
        await _audit(cycle_id, "simple_pipeline_error", {"symbol": symbol, "error": str(exc)})
        return {"action": "error", "reason": str(exc)}

"""
Última línea de defensa antes de enviar una orden a MT5.

Responsabilidades:
- Validación geométrica (BUY: sl < entry < tp, SELL: tp < entry < sl)
- Precio dentro del rango del símbolo
- SL mínimo y máximo en pips
- Detectar orden duplicada (mismo symbol + entry +/- 1 pip)
- Registrar en DB como pending ANTES de enviar
"""
import logging
logger = logging.getLogger(__name__)

import hashlib
import hmac
import json

from models.orders import OrderPrepareRequest, OrderPrepareResponse, AuditLogRequest, AuditLogResponse
from db.connection import get_pool
from config import settings
from services.order_validator import (
    validate_price_range,
    validate_geometry,
    validate_sl_pips,
    validate_volume,
    check_duplicate,
    OrderValidationError,
)


async def _audit(cycle_id: str, event: str, data: dict) -> None:
    try:
        pool = get_pool()
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
            cycle_id, event, json.dumps(data),
        )
    except Exception as exc:
        logger.warning("[ORDER-MANAGER] audit_log failed: %s", exc)


def _reject(reason: str) -> OrderPrepareResponse:
    return OrderPrepareResponse(approved=False, order_payload={}, rejection_reason=reason)


async def mark_order_filled(symbol: str, order_type: str) -> int | None:
    """O-1: transiciona la orden viva más reciente de (symbol, type) a 'filled'.

    El webhook /api/v1/ai/trade/filled confirma la ejecución (apertura o
    cierre), pero `orders.status` se quedaba en 'pending' para siempre →
    `check_duplicate` rechazaba entradas legítimas contra órdenes ya cerradas
    (6 `duplicate_order` falsos en la ventana auditada; el guard solo debe
    matchear órdenes VIVAS).

    Match: la más reciente `pending`/`placed` de (symbol, type), sin ventana
    temporal — `entry_time` de los reportes es reloj broker (UTC+3), inútil
    contra `created_at` UTC, y el guard de posición del EA garantiza ≤1 orden
    viva por símbolo entre prepare y fill. Las filas SMC (`smc_*`, pipeline
    distinto, su propio ciclo de vida) quedan excluidas por `cycle_id`.

    Idempotente: el reporte de cierre tras el de apertura (o un reintento del
    outbox) no matchea nada — 'filled' ya no es vivo → devuelve None.
    'filled' es terminal en `orders`: la verdad del cierre vive en
    `trade_outcomes` (el CHECK `orders_status_check` no incluye 'closed';
    decisión documentada, ver plan O-1).

    Devuelve el id transicionado, o None si no había orden viva que casar.
    """
    pool = get_pool()
    row = await pool.fetchrow(
        "UPDATE orders SET status='filled' WHERE id = ("
        "SELECT id FROM orders "
        "WHERE symbol=$1 AND type=$2 AND status IN ('pending','placed') "
        "AND cycle_id NOT LIKE 'smc\\_%' "
        "ORDER BY created_at DESC LIMIT 1) "
        "RETURNING id, cycle_id",
        symbol, order_type,
    )
    if row is None:
        logger.info(
            "[ORDER-MANAGER] mark_order_filled: sin orden viva que casar (%s %s) — idempotente",
            symbol, order_type,
        )
        return None
    logger.info(
        "[ORDER-MANAGER] orden %s → filled (%s %s, cycle=%s)",
        row["id"], symbol, order_type, row["cycle_id"],
    )
    await _audit(row["cycle_id"], "order_filled", {
        "order_db_id": row["id"], "symbol": symbol, "type": order_type,
    })
    return row["id"]


async def prepare(req: OrderPrepareRequest) -> OrderPrepareResponse:
    pool = get_pool()

    # 1. Idempotencia: ¿este cycle_id ya tiene una orden?
    existing = await pool.fetchrow(
        "SELECT id FROM orders WHERE cycle_id=$1 AND status IN ('pending','placed','filled')",
        req.cycle_id,
    )
    if existing:
        await _audit(req.cycle_id, "order_rejected", {"reason": "cycle_already_has_order", "symbol": req.symbol})
        return _reject("cycle_already_has_order")

    # 1b. Gate de mundo (P3-1d): la orden debe pertenecer a un par configurado
    # y habilitado — MISMA admisión que /predict, ahora en la última línea de
    # defensa antes de MT5. La asignación de equity del par viaja con su config.
    from core.container import build_registry_from_settings
    pair_ctx = build_registry_from_settings().get(req.symbol)
    if pair_ctx is None:
        await _audit(req.cycle_id, "order_rejected", {"reason": "pair_not_configured", "symbol": req.symbol})
        return _reject("pair_not_configured")
    if not pair_ctx.config.enabled:
        await _audit(req.cycle_id, "order_rejected", {"reason": "pair_disabled", "symbol": req.symbol})
        return _reject("pair_disabled")

    # 1c. Risk Guardian: ¿se puede abrir nueva posición en ESTE par?
    from services.risk_guardian import check_risk
    from services.risk_state_store import flush_risk_state
    from services.mt5_client import get_account, get_positions
    try:
        acct = await get_account()
        positions_data = await get_positions()
        # P3-1d: el cupo de posiciones es POR PAR — solo cuentan las de este
        # mundo; un par jamás consume la capacidad de otro.
        open_all = [
            p for p in positions_data.get("open", [])
            if isinstance(p, dict)
        ]
        open_count = sum(1 for p in open_all if p.get("symbol") == req.symbol)
        # P2-4: exposición AGREGADA de la cuenta (todas las estrategias y
        # pares) — un límite que ninguna estrategia puede rebasar sola.
        total_open = len(open_all)
        can_open, reason = check_risk(
            acct["equity"], open_count,
            total_open_positions=total_open, symbol=req.symbol,
        )
        # P2-4: persiste baseline/racha si este ciclo los modificó (sin dirty
        # no hay I/O). Flush también cuando el guardián bloquea: la alerta
        # emitida (cooldown) es estado que debe sobrevivir a un reinicio.
        await flush_risk_state(symbol=req.symbol)
        if not can_open:
            await _audit(req.cycle_id, "order_rejected", {"reason": f"risk_guardian_blocked:{reason}", "symbol": req.symbol})
            return _reject(f"risk_guardian_blocked: {reason}")
    except Exception as rg_err:
        logger.error("[ORDER-MANAGER] RiskGuardian check falló: %s — bloqueando orden", rg_err)
        await _audit(req.cycle_id, "order_rejected", {"reason": f"risk_guardian_error:{rg_err}", "symbol": req.symbol})
        return _reject(f"risk_guardian_error: {rg_err}")

    # 2-6. Validaciones (extraídas a order_validator)
    try:
        validate_price_range(req.symbol, req.entry)
        validate_geometry(req)
        validate_sl_pips(req.symbol, req.entry, req.sl)
        validate_volume(req.volume)
        await check_duplicate(pool, req.symbol, req.entry)
    except OrderValidationError as exc:
        await _audit(req.cycle_id, "order_rejected", {"reason": exc.reason, "symbol": req.symbol})
        return _reject(exc.reason)

    # 6b. Presupuesto del par (P3-1d): techo de size = max_volume × asignación,
    # redondeado ABAJO a precisión de lote (no se opera 0.165). Con EURUSD al
    # 100% el techo es el global de siempre — sin cambio de comportamiento.
    ceiling = (
        int(settings.max_volume * pair_ctx.config.equity_allocation_pct / 100.0 * 100 + 1e-9) / 100
    )
    if req.volume > ceiling:
        reason = f"volume_exceeds_pair_allocation(max={ceiling:.2f})"
        await _audit(req.cycle_id, "order_rejected", {"reason": reason, "symbol": req.symbol})
        return _reject(reason)

    # 7. Registrar en DB como pending
    order_id = await pool.fetchval(
        "INSERT INTO orders(cycle_id, symbol, type, entry, sl, tp, volume, status) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,'pending') RETURNING id",
        req.cycle_id, req.symbol, req.type,
        req.entry, req.sl, req.tp, req.volume
    )

    await _audit(req.cycle_id, "order_approved", {
        "symbol": req.symbol, "type": req.type, "entry": req.entry,
        "sl": req.sl, "tp": req.tp, "volume": req.volume, "order_db_id": order_id,
    })

    # 8. Firmar payload
    to_sign = {
        "cycle_id":    req.cycle_id,
        "order_db_id": order_id,
        "price":       req.entry,
        "sl":          req.sl,
        "symbol":      req.symbol,
        "tp":          req.tp,
        "type":        req.type,
        "volume":      req.volume,
    }
    canonical = json.dumps(to_sign, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(settings.hmac_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

    payload = {**to_sign, "_canonical": canonical, "_sig": sig}
    return OrderPrepareResponse(approved=True, order_payload=payload, rejection_reason="")


async def audit_log(req: AuditLogRequest) -> AuditLogResponse:
    pool = get_pool()
    row_id = await pool.fetchval(
        "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3) RETURNING id",
        req.cycle_id, req.event, json.dumps(req.data),
    )
    return AuditLogResponse(id=row_id)

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

    # 1b. Risk Guardian: ¿se puede abrir nueva posición?
    from services.risk_guardian import check_risk
    from services.mt5_client import get_account, get_positions
    try:
        acct = await get_account()
        positions_data = await get_positions()
        open_count = len(positions_data.get("open", []))
        can_open, reason = check_risk(acct["equity"], open_count)
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

"""
Última línea de defensa antes de enviar una orden a MT5.

Responsabilidades:
- Validación geométrica (BUY: sl < entry < tp, SELL: tp < entry < sl)
- Precio dentro del rango del símbolo
- SL mínimo y máximo en pips
- Detectar orden duplicada (mismo symbol + entry ± 1 pip)
- Registrar en DB como pending ANTES de enviar
"""

import hashlib
import hmac
import json

from models.orders import OrderPrepareRequest, OrderPrepareResponse, AuditLogRequest, AuditLogResponse
from db.connection import get_pool
from config import settings


VALID_RANGES = {
    "EURUSD": (1.05, 1.20),
    "GBPUSD": (1.20, 1.45),
    "USDJPY": (130.0, 175.0),
    "USDCHF": (0.75, 1.05),
    "XAUUSD": (2000.0, 8000.0),
}

PIP_SIZE = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "USDCHF": 0.0001,
    "XAUUSD": 0.10,
}

SL_LIMITS: dict[str, tuple[float, float]] = {
    "USDJPY": (5.0,  300.0),
    "XAUUSD": (10.0, 2000.0),
}
_SL_DEFAULT = (3.0, 300.0)


def _pip(symbol: str) -> float:
    return PIP_SIZE.get(symbol, 0.0001)


def _pips(symbol: str, a: float, b: float) -> float:
    return abs(a - b) / _pip(symbol)


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
        return _reject("cycle_already_has_order")

    # 2. Rango de precio
    lo, hi = VALID_RANGES.get(req.symbol, (0, 99999))
    if not (lo <= req.entry <= hi):
        return _reject(f"entry_out_of_range:{req.entry}")

    # 3. Geometría
    if req.type == "BUY":
        if not (req.sl < req.entry < req.tp):
            return _reject("invalid_geometry_buy")
    elif req.type == "SELL":
        if not (req.tp < req.entry < req.sl):
            return _reject("invalid_geometry_sell")
    else:
        return _reject(f"unknown_order_type:{req.type}")

    # 4. SL en pips
    sl_pips = _pips(req.symbol, req.entry, req.sl)
    min_pips, max_pips = SL_LIMITS.get(req.symbol, _SL_DEFAULT)
    if sl_pips < min_pips:
        return _reject(f"sl_too_tight:{round(sl_pips,1)}pips")
    if sl_pips > max_pips:
        return _reject(f"sl_too_wide:{round(sl_pips,1)}pips")

    # 5. Volumen
    if not (settings.min_volume <= req.volume <= settings.max_volume):
        return _reject(f"volume_out_of_range:{req.volume}")

    # 6. Duplicado: mismo símbolo + entry ± 1 pip
    tolerance = _pip(req.symbol)
    dup = await pool.fetchrow(
        """SELECT id FROM orders
           WHERE symbol=$1
             AND status IN ('pending','placed')
             AND ABS(entry - $2) <= $3""",
        req.symbol, req.entry, tolerance,
    )
    if dup:
        return _reject(f"duplicate_order:{req.symbol}@{req.entry}")

    # 7. Registrar en DB como pending
    order_id = await pool.fetchval(
        """INSERT INTO orders(cycle_id, symbol, type, entry, sl, tp, volume, status)
           VALUES($1,$2,$3,$4,$5,$6,$7,'pending') RETURNING id""",
        req.cycle_id, req.symbol, req.type,
        req.entry, req.sl, req.tp, req.volume,
    )

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

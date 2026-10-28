"""Subrouter: salud del pipeline y observabilidad (bajo riesgo).

Endpoints:
  GET /pipeline/health  — health check integral del sistema
  GET /cross-audit      — auditoría cruzada de métricas publicadas
  GET /ledger           — ledger de fiabilidad por fuente

Los paths públicos NO cambian: se incluyen en main.py con prefix="/api/v1/ai",
por lo que siguen disponibles en /api/v1/ai/pipeline/health, etc.
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from services.alerting import AlertLevel, send_alert
from services.auto_retrain import get_state as _get_retrain_state
from services.error_taxonomy import LOOP_ERRORS
from services.qwen_client import get_qwen_state

from ._shared import _get_registry, _TRAINING_SYMBOL
from db.connection import get_pool

router = APIRouter()

logger = logging.getLogger(__name__)


class HealthCheckResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "critical"
    components: dict  # {name: {"ok": bool, "error": str|null}}


@router.get("/pipeline/health", response_model=HealthCheckResponse)
async def pipeline_health() -> HealthCheckResponse:
    """
    Verifica la salud de todo el sistema de trading.
    Cada componente que falle se reporta explícitamente.
    """
    components = {}
    issues = []

    # 1. Database
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    trade_count = await pool.fetchval("SELECT COUNT(*) FROM trade_outcomes")
    components["database"] = {"ok": True, "trade_count": trade_count, "error": None}

    # 2. MT5 connectivity
    mt5_ok = False
    try:
        registry = _get_registry()
        ctx_health = registry.get("EURUSD")
        if ctx_health is None:
            enabled = registry.enabled()
            if not enabled:
                raise ValueError("PAIRS: ningún par habilitado para el health check")
            ctx_health = enabled[0]
        account = await ctx_health.market.get_account()
        mt5_ok = "balance" in account or "equity" in account
    except LOOP_ERRORS as exc:
        logger.warning("[pipeline_health] MT5 connectivity error: %s", exc)
    components["mt5"] = {"ok": mt5_ok, "error": None if mt5_ok else "MT5 no disponible"}
    if not mt5_ok:
        raise RuntimeError("MT5 connectivity failed")

    # 3. PPO model file exists
    MODEL_PATH = "/app/ml/ppo_trading_bot_v3.zip"
    model_exists = os.path.exists(MODEL_PATH)
    components["ppo_model"] = {"ok": model_exists, "error": None if model_exists else "Model file not found"}
    if not model_exists:
        raise RuntimeError("PPO model missing at /app/ml/ppo_trading_bot_v3.zip")

    # 4. Retrain state
    state = _get_retrain_state()
    components["retrain"] = {
        "ok": True,
        "filled_count": state.filled_count,
        "in_progress": state.retrain_in_progress,
        "last_retrain": state.last_retrain_time,
        "error": None,
    }
    if state.filled_count == 0:
        issues.append("No trades recorded yet (EA may not be sending webhooks)")

    # 5. Qwen LLM — estado degradado (P1-6). Mientras falle, /predict usa
    #    safe defaults (quality=0 → veto fail-closed BUY/SELL → HOLD).
    qs = get_qwen_state()
    qwen_ok = qs.consecutive_failures == 0
    components["qwen"] = {
        "ok": qwen_ok,
        "calls_ok": qs.calls_ok,
        "calls_failed": qs.calls_failed,
        "consecutive_failures": qs.consecutive_failures,
        "last_ok_at": qs.last_ok_at,
        "last_fail_at": qs.last_fail_at,
        "last_error": qs.last_error,
        "error": None if qwen_ok else (qs.last_error or "Qwen degradado"),
    }
    if not qwen_ok:
        issues.append(
            f"Qwen degradado ({qs.consecutive_failures} fallos seguidos): "
            f"{qs.last_error} — predict opera con safe defaults (veto a compra/venta)"
        )

    # Determine overall status
    critical_failures = sum(1 for c in components.values() if not c["ok"])
    if critical_failures > 0:
        critical_component_failed = any(
            not components[name]["ok"] for name in ("database", "ppo_model")
        )
        status = "critical" if critical_component_failed else "degraded"
    else:
        status = "healthy"

    if status != "healthy":
        send_alert(
            AlertLevel.ERROR,
            "PIPELINE-HEALTH",
            f"Pipeline status: {status}. Issues: {'; '.join(issues)}",
            context={"components": components, "status": status},
        )

    return HealthCheckResponse(status=status, components=components)


@router.get("/cross-audit")
async def cross_audit_endpoint(symbol: Optional[str] = Query(default=None)):
    """AUDITORÍA CRUZADA (P2) — recomputa INDEPENDIENTEMENTE los números
    publicados y compara. Sólo lectura: no muta el trading.
    """
    from services.cross_audit import run_cross_audit

    sym = (symbol or _TRAINING_SYMBOL).upper()
    return await run_cross_audit(sym)


@router.get("/ledger")
async def ledger_endpoint(symbol: Optional[str] = Query(default=None)):
    """LEDGER DE FIABILIDAD (L4) — el estado del mundo con la vara correcta.

    Por cada fuente: métricas de TRADING OOS, track record vivo, skill de
    clasificación con n y CI, y su ADMISIÓN con el motivo. Sin token: es un
    informe de estado, no un disparador (no muta nada).
    """
    from services.ledger import build_ledger

    sym = (symbol or _TRAINING_SYMBOL).upper()
    return await build_ledger(sym)

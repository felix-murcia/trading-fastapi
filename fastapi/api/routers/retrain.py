"""Subrouter: auto-retrain (riesgo medio — usa services/ directamente).

Endpoints:
  POST /retrain/force        — fuerza retrain inmediato
  POST /retrain/promote-candidate — promueve candidato ya medido
  POST /retrain/approve      — aprueba retrain pendiente
  GET  /retrain/status       — estado del auto-retrain

Los paths públicos NO cambian: prefix="/api/v1/ai" en main.py.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from services.auto_retrain import (
    approve_retrain as _approve_retrain,
    force_retrain as _force_retrain,
    get_state as _get_retrain_state,
)
from services.candidate_promotion import CandidatePromotionError, promote_measured_candidate
from services.structured_logging import log_to_audit

from .._shared import _TRAINING_SYMBOL, _sole_enabled_symbol
from .deps import verify_token

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/retrain/force")
async def force_retrain_endpoint(
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(verify_token),
):
    """
    Fuerza un retrain inmediato del modelo PPO (parámetro opcional `symbol`;
    por defecto TRAINING_SYMBOL — P3-3: pipeline por par).
    Útil para when market regime cambia (e.g., central bank intervention).
    """
    sym = symbol or _TRAINING_SYMBOL
    state = _get_retrain_state(sym)
    result = await _force_retrain(symbol=symbol)
    await log_to_audit(
        "retrain_forced",
        {
            "symbol": sym,
            "status": result.get("status"),
            "filled_count": state.filled_count,
            "in_progress": state.retrain_in_progress,
            "last_retrain_time": state.last_retrain_time,
            "outcomes_count": len(state.outcomes),
        },
    )
    return {"status": result["status"], "filled_count": state.filled_count, "in_progress": state.retrain_in_progress}


@router.post("/retrain/promote-candidate")
async def promote_candidate_endpoint(
    exp_id: int = Query(...),
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(verify_token),
):
    """Promueve un CANDIDATO YA MEDIDO (artefactos de un experimento cerrado).

    Orden Félix 2026-10-19: *«quiero que cuando el candidato sea
    drásticamente mejor que el modelo vivo, se promocione el drásticamente
    mejor»*.

    NO se salta la regla: el informe del experimento se vuelve a evaluar con
    `services/promotion_rule.py` (mismos 8 criterios congelados) y sólo se
    promueve si la regla lo concede. Devuelve el detalle de cada criterio
    para que la decisión sea auditable.
    """
    from services.candidate_promotion import CandidatePromotionError, promote_measured_candidate

    sym = (symbol or _TRAINING_SYMBOL).upper()
    try:
        result = await promote_measured_candidate(sym, exp_id)
    except CandidatePromotionError as exc:
        logger.error("[PROMO-CAND] exp%d NO promoted: %s", exp_id, exc)
        await log_to_audit("candidate_promotion_failed", {"symbol": sym, "exp_id": exp_id, "error": str(exc)})
        return {"promoted": False, "exp_id": exp_id, "error": str(exc)}
    await log_to_audit(
        "candidate_promoted" if result.get("promoted") else "candidate_promotion_rejected",
        {
            "symbol": sym,
            "exp_id": exp_id,
            "promoted": result.get("promoted"),
            "reason": (result.get("promotion_path") or {}).get("reason"),
            "path": (result.get("promotion_path") or {}).get("path"),
            "checks": ((result.get("promotion_path") or {}).get("drastic") or {}).get("checks"),
            "model_sha256": result.get("model_sha256"),
        },
    )
    return result


@router.post("/retrain/approve")
async def approve_retrain_endpoint(
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(verify_token),
):
    """Aprueba el retrain pendiente de `symbol` (umbral alcanzado o calendario
    con retrain_auto_approve=false). `symbol` opcional → TRAINING_SYMBOL.

    No salta el gate de promoción: ese se aplica dentro de _do_retrain.
    """
    result = await _approve_retrain(symbol=symbol)
    await log_to_audit(
        "retrain_approved",
        {
            "symbol": symbol or _TRAINING_SYMBOL,
            "status": result.get("status"),
            "filled_count": result.get("filled_count"),
            "min_trades_for_retrain": result.get("min_trades_for_retrain"),
        },
    )
    return result


@router.get("/retrain/status")
async def retrain_status(_: None = Depends(verify_token)):
    """Estado actual del auto-retrain."""
    state = _get_retrain_state()
    return {
        "filled_count": state.filled_count,
        "retrain_in_progress": state.retrain_in_progress,
        "last_retrain_time": state.last_retrain_time,
        "outcomes_count": len(state.outcomes),
    }

"""
Endpoint de simulación para pruebas E2E.

Ejecuta la lógica completa del ciclo (analysis → risk → order/prepare)
con datos controlados. NO escribe en la DB, NO envía órdenes a MT5.
Solo disponible con el token interno.
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from models.analysis import TechnicalData
from models.risk import LLMSignal
from models.orders import OrderPrepareRequest
from services import pair_analyzer, risk_evaluator, order_manager, mt5_client
from .deps import verify_token


class SimulateRequest(BaseModel):
    equity: float = 10000.0
    llm_signals: list[LLMSignal] = [
        LLMSignal(agent="technical",   signal="buy", confidence=0.85, parse_error=False),
        LLMSignal(agent="fundamental", signal="buy", confidence=0.82, parse_error=False),
        LLMSignal(agent="sentiment",   signal="neutral", confidence=0.40, parse_error=False),
    ]


class SimulateResponse(BaseModel):
    cycle_id: str
    best_pair: str
    price: float
    action: str
    entry: float
    sl: float
    tp: float
    volume: float
    confidence: float
    risk_reason: str
    order_approved: bool
    order_reason: str
    dry_run: bool = True


router = APIRouter(prefix="/v1/test", tags=["test"])


@router.post("/simulate", response_model=SimulateResponse)
async def simulate_cycle(
    req: SimulateRequest,
    _: None = Depends(verify_token),
) -> SimulateResponse:
    cycle_id = f"TEST_{uuid.uuid4().hex[:12]}"

    # 1. Obtener precios reales de MT5
    try:
        prices = await mt5_client.get_prices()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"mt5_prices_unavailable: {e}")

    # 2. Análisis técnico (determinista, sin LLM)
    from models.analysis import PairsAnalysisRequest
    analysis = await pair_analyzer.analyze(PairsAnalysisRequest(cycle_id=cycle_id, prices=prices))

    # 3. Risk evaluate — ajustar soporte/resistencia para garantizar SL dentro del rango válido
    from services.order_manager import PIP_SIZE as _PIP, SL_LIMITS, _SL_DEFAULT
    tech = analysis.technical
    pip = _PIP.get(analysis.best_pair, 0.0001)
    sl_min, sl_max = SL_LIMITS.get(analysis.best_pair, _SL_DEFAULT)
    target_sl_pips = sl_min * 6   # 6× el mínimo — SL holgado pero dentro del rango
    tech.support    = round(tech.current_price - target_sl_pips * pip, 5)
    tech.resistance = round(tech.current_price + target_sl_pips * pip, 5)
    tech.atr        = round(target_sl_pips * pip * 0.5, 5)

    from models.risk import RiskEvaluateRequest
    risk_req = RiskEvaluateRequest(
        cycle_id=cycle_id,
        best_pair=analysis.best_pair,
        price=analysis.price,
        technical=tech,
        llm_signals=req.llm_signals,
        positions=[],
    )
    risk_result = risk_evaluator.evaluate(risk_req, req.equity)

    if risk_result.action == "skip":
        return SimulateResponse(
            cycle_id=cycle_id,
            best_pair=analysis.best_pair,
            price=analysis.price,
            action="skip",
            entry=risk_result.entry,
            sl=risk_result.sl,
            tp=risk_result.tp,
            volume=0,
            confidence=risk_result.confidence,
            risk_reason=risk_result.reason,
            order_approved=False,
            order_reason="skipped_by_risk",
        )

    # 4. Order prepare — DRY RUN (validaciones geométricas sin escritura en DB)
    prep_req = OrderPrepareRequest(
        cycle_id=cycle_id,
        symbol=analysis.best_pair,
        type=risk_result.action.upper(),
        entry=risk_result.entry,
        sl=risk_result.sl,
        tp=risk_result.tp,
        volume=risk_result.volume,
    )
    approved, rejection_reason = order_manager.validate_only(prep_req)

    return SimulateResponse(
        cycle_id=cycle_id,
        best_pair=analysis.best_pair,
        price=analysis.price,
        action=risk_result.action,
        entry=risk_result.entry,
        sl=risk_result.sl,
        tp=risk_result.tp,
        volume=risk_result.volume,
        confidence=risk_result.confidence,
        risk_reason=risk_result.reason,
        order_approved=approved,
        order_reason=rejection_reason or "ok",
    )

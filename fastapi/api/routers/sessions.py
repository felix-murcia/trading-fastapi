"""Subrouter: sesiones de trading (bajo riesgo).

Endpoints:
  POST /ea/decision — webhook que el EA llama al recibir una decisión

Los paths públicos NO cambian: prefix="/api/v1/ai" en main.py.
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from services.structured_logging import log_to_audit

from config import settings
from ._shared import _TRAINING_SYMBOL
from routers.deps import verify_token

router = APIRouter()


class EADecisionRequest(BaseModel):
    cycle_id: str
    symbol: str
    received_decision: str
    action: str
    order_ticket: Optional[str] = None
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    reason: Optional[str] = None
    latency_ms: Optional[float] = None
    raw_response: Optional[str] = None
    mt5_time: Optional[float] = None  # epoch desde MT5; si falta, se usa time.time() como fallback


@router.post("/ea/decision")
async def ea_decision_webhook(req: EADecisionRequest, _: None = Depends(verify_token)):
    if settings.USE_NEW_RECORD_EA_DECISION:
        # Camino nuevo: caso de uso aislado en src/application/ (paridad byte a byte con legacy)
        from src.application.use_cases.record_ea_decision import (
            RecordEADecision, RecordEADecisionInput,
        )
        uc = RecordEADecision(
            audit=None, clock=None,
        )
        _result = uc.execute(RecordEADecisionInput(
            cycle_id=req.cycle_id,
            symbol=req.symbol,
            received_decision=req.received_decision,
            action=req.action,
            order_ticket=req.order_ticket,
            volume=req.volume,
            sl=req.sl,
            tp=req.tp,
            reason=req.reason,
            latency_ms=req.latency_ms,
            raw_response=req.raw_response,
            mt5_time=req.mt5_time,
        ))
        return {"status": "recorded" if _result.audited else "error"}
    else:
        # Camino legacy (byte-idéntico al comportamiento previo)
        pass
    mt5_ts = req.mt5_time if req.mt5_time is not None else time.time()
    await log_to_audit(
        "ea_decision",
        {
            "symbol": req.symbol,
            "received_decision": req.received_decision,
            "action": req.action,
            "order_ticket": req.order_ticket,
            "volume": req.volume,
            "sl": req.sl,
            "tp": req.tp,
            "reason": req.reason,
            "latency_ms": req.latency_ms,
            "raw_response": req.raw_response,
            "mt5_time": mt5_ts,
        },
        cycle_id=req.cycle_id,
    )
    return {"status": "recorded"}

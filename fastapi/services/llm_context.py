"""Preparación de contexto completo para LLM agents con trazabilidad."""

import json
from datetime import datetime
from models.llm import LLMContextRequest, LLMContextResponse
from db.connection import get_pool


async def prepare_context(req: LLMContextRequest) -> LLMContextResponse:
    """
    Agrega contexto completo para LLM agents y loguea la trazabilidad.

    Verifica que todos los datos críticos estén presentes.
    Loguea exactamente qué contexto se envía a los agents.
    """
    pool = get_pool()

    # Validar que contexto crítico está presente
    critical_checks = {
        "cycle_id": bool(req.cycle_id),
        "prices_count": len(req.prices),
        "news_count": len(req.news),
        "calendar_count": len(req.calendar),
        "has_best_pair": bool(req.pair_analysis.best_pair),
        "has_technical": bool(req.pair_analysis.technical.rsi),
    }

    all_valid = all(critical_checks.values())

    # Loguear contexto enviado a agents
    context_log = {
        "cycle_id": req.cycle_id,
        "timestamp": datetime.utcnow().isoformat(),
        "critical_checks": critical_checks,
        "all_valid": all_valid,
        "best_pair": req.pair_analysis.best_pair,
        "smc_direction": req.pair_analysis.smc_direction,
        "news_headlines": [n.headline[:50] for n in req.news[:3]],
        "calendar_events": [c.event[:30] for c in req.calendar[:3]],
        "positions_open": len(req.positions),
        "prices_available": list(req.prices.keys()),
    }

    print(f"[LLM_CONTEXT] Preparing context for agents: {json.dumps(context_log, indent=2)}")

    # Registrar en DB para auditoría
    try:
        await pool.execute(
            """INSERT INTO audit_log(cycle_id, event_type, event_data, created_at)
               VALUES($1, $2, $3, NOW())""",
            req.cycle_id,
            "llm_context_prepared",
            json.dumps(context_log)
        )
    except Exception as e:
        print(f"[WARNING] Failed to log LLM context: {e}")

    # Construir response
    return LLMContextResponse(
        cycle_id=req.cycle_id,
        prices=req.prices,
        news=req.news,
        calendar=req.calendar,
        positions=req.positions,
        best_pair=req.pair_analysis.best_pair,
        price=req.pair_analysis.price,
        scores=req.pair_analysis.scores,
        technical=req.pair_analysis.technical,
        smc_active=req.pair_analysis.smc_active,
        smc_direction=req.pair_analysis.smc_direction,
    )

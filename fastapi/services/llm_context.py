"""Preparación de contexto completo para LLM agents con trazabilidad."""

import json
from datetime import datetime
from models.llm import LLMContextRequest, LLMContextResponse
from db.connection import get_pool


# Idiosincrasia de cada par: divisas relevantes, drivers y hints para cada agente
PAIR_CONTEXT: dict[str, dict] = {
    "EURUSD": {
        "type": "forex_major",
        "relevant_currencies": ["EUR", "USD"],
        "drivers": "BCE vs FED. Divergencia de política monetaria entre zona euro y EEUU.",
        "fundamental_hint": (
            "BCE hawkish o datos Eurozona fuertes (PIB, empleo, IPC) → comprar. "
            "FED hawkish o datos EEUU fuertes (NFP, IPC, PIB) → vender."
        ),
        "sentiment_hint": (
            "Risk-on favorece ligeramente al EUR. "
            "Risk-off o crisis geopolítica → dólar como refugio → vender."
        ),
    },
    "GBPUSD": {
        "type": "forex_major",
        "relevant_currencies": ["GBP", "USD"],
        "drivers": "BOE vs FED. Datos UK (PIB, inflación, empleo) y política monetaria.",
        "fundamental_hint": (
            "BOE hawkish o datos UK fuertes → comprar. "
            "BOE dovish, recesión UK, PIB débil → vender. "
            "FED hawkish o NFP fuerte → vender."
        ),
        "sentiment_hint": (
            "GBP sensible a riesgo político UK. "
            "Risk-off → USD se fortalece → vender GBPUSD."
        ),
    },
    "USDJPY": {
        "type": "forex_jpy",
        "relevant_currencies": ["USD", "JPY"],
        "drivers": "Yields EEUU vs política BOJ ultralaxa. El JPY es divisa refugio.",
        "fundamental_hint": (
            "Yields USA suben, FED hawkish → comprar. "
            "BOJ hawkish o intervención verbal → vender. "
            "Risk-off → JPY se fortalece → vender USDJPY."
        ),
        "sentiment_hint": (
            "Geopolítica, recesión o crisis financiera → JPY refugio → vender USDJPY. "
            "Risk-on, apetito de riesgo → carry trade activo → comprar USDJPY."
        ),
    },
    "USDCHF": {
        "type": "forex_chf",
        "relevant_currencies": ["USD", "CHF"],
        "drivers": "FED vs SNB. CHF es divisa refugio. Relación inversa con EURUSD.",
        "fundamental_hint": (
            "FED hawkish o datos EEUU fuertes → comprar. "
            "SNB hawkish o intervención → vender. "
            "Risk-off → CHF refugio → vender USDCHF."
        ),
        "sentiment_hint": (
            "Tensión geopolítica, miedo a recesión → CHF se aprecia → vender USDCHF. "
            "Risk-on, calma geopolítica → CHF se debilita → comprar USDCHF."
        ),
    },
    "XAUUSD": {
        "type": "commodity_gold",
        "relevant_currencies": ["USD"],
        "drivers": (
            "Relación INVERSA con el USD. "
            "Impulsado por: yields reales, inflación, demanda refugio, compras bancos centrales."
        ),
        "fundamental_hint": (
            "USD débil, yields reales bajos, inflación alta → comprar oro. "
            "USD fuerte, FED hawkish, yields suben → vender oro. "
            "IMPORTANTE: la relación USD-oro es INVERSA."
        ),
        "sentiment_hint": (
            "Tensión geopolítica, guerra, incertidumbre → oro como refugio → comprar. "
            "Acuerdos de paz, risk-on, datos económicos positivos → vender oro. "
            "El oro NO se comporta como una divisa: sube con el miedo, baja con la calma."
        ),
    },
}


def _filter_by_pair(items: list, relevant_currencies: list[str]) -> list:
    """Filtra noticias/calendario a las divisas relevantes del par. Fallback a todos si vacío."""
    filtered = [
        i for i in items
        if (getattr(i, "currency", "") or "").upper() in relevant_currencies
        or not (getattr(i, "currency", "") or "").strip()
    ]
    return filtered if filtered else items


async def prepare_context(req: LLMContextRequest) -> LLMContextResponse:
    pool = get_pool()

    best_pair = req.pair_analysis.best_pair
    pair_meta = PAIR_CONTEXT.get(best_pair, {})
    relevant = pair_meta.get("relevant_currencies", [])

    # Filtrar news y calendar al par seleccionado
    filtered_news     = _filter_by_pair(req.news, relevant)
    filtered_calendar = _filter_by_pair(req.calendar, relevant)

    critical_checks = {
        "cycle_id":      bool(req.cycle_id),
        "prices_count":  len(req.prices),
        "news_count":    len(filtered_news),
        "calendar_count": len(filtered_calendar),
        "has_best_pair": bool(best_pair),
        "has_technical": bool(req.pair_analysis.technical.rsi),
    }

    context_log = {
        "cycle_id":        req.cycle_id,
        "timestamp":       datetime.utcnow().isoformat(),
        "critical_checks": critical_checks,
        "best_pair":       best_pair,
        "pair_type":       pair_meta.get("type", "unknown"),
        "relevant_currencies": relevant,
        "news_total":      len(req.news),
        "news_filtered":   len(filtered_news),
        "calendar_total":  len(req.calendar),
        "calendar_filtered": len(filtered_calendar),
        "news_headlines":  [n.headline[:50] for n in filtered_news[:3]],
        "calendar_events": [c.event[:30] for c in filtered_calendar[:3]],
    }

    print(f"[LLM_CONTEXT] {json.dumps(context_log, indent=2)}")

    try:
        await pool.execute(
            """INSERT INTO audit_log(cycle_id, event_type, event_data, created_at)
               VALUES($1, $2, $3, NOW())""",
            req.cycle_id, "llm_context_prepared", json.dumps(context_log)
        )
    except Exception as e:
        print(f"[WARNING] Failed to log LLM context: {e}")

    return LLMContextResponse(
        cycle_id=req.cycle_id,
        prices=req.prices,
        news=filtered_news,
        calendar=filtered_calendar,
        positions=req.positions,
        best_pair=best_pair,
        price=req.pair_analysis.price,
        scores=req.pair_analysis.scores,
        technical=req.pair_analysis.technical,
        smc_active=req.pair_analysis.smc_active,
        smc_direction=req.pair_analysis.smc_direction,
        pair_context=pair_meta,
    )

"""
Router para enriquecimiento de datos de mercado.

Endpoint: POST /v1/enrichment/market-data
Convierte datos crudos de n8n en datos estructurados para LLM agents.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.data_enrichment import enrich_news, enrich_calendar, validate_before_agents
from .deps import verify_token


class MarketDataEnrichmentRequest(BaseModel):
    """Datos crudos de WF-02 (Call Market Data)."""
    prices: dict
    positions: list = []
    news: list = []  # Raw news items
    calendar: list = []  # Raw calendar events


class EnrichmentResponse(BaseModel):
    """Datos enriquecidos listos para LLM agents."""
    prices: dict
    positions: list
    news: list  # Enriquecido
    calendar: list  # Enriquecido
    validation: dict  # {is_valid: bool, errors: [str]}


router = APIRouter(prefix="/v1/enrichment", tags=["enrichment"])


@router.post("/market-data", response_model=EnrichmentResponse)
async def enrich_market_data(
    req: MarketDataEnrichmentRequest,
    _: None = Depends(verify_token),
) -> EnrichmentResponse:
    """
    Enriquece datos de mercado crudos para consumo de LLM agents.

    Proceso:
    1. Enriquecer news: extraer currency, mejorar impact
    2. Enriquecer calendar: extraer time, date, currency
    3. Validar que no haya campos vacíos
    4. Retornar datos estructurados + estado de validación
    """
    try:
        # Enriquecimiento
        enriched_news = enrich_news(req.news)
        enriched_calendar = enrich_calendar(req.calendar)

        # Composición
        enriched_data = {
            "prices": req.prices,
            "positions": req.positions,
            "news": enriched_news,
            "calendar": enriched_calendar,
        }

        # Validación
        is_valid, errors = validate_before_agents(enriched_data)

        return EnrichmentResponse(
            prices=enriched_data["prices"],
            positions=enriched_data["positions"],
            news=enriched_data["news"],
            calendar=enriched_data["calendar"],
            validation={"is_valid": is_valid, "errors": errors},
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"enrichment_error: {str(e)}"
        )

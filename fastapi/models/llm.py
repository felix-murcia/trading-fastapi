from pydantic import BaseModel
from models.context import NewsItem, CalendarEvent, Position


class TechnicalAnalysis(BaseModel):
    """Análisis técnico del par seleccionado."""
    current_price: float
    rsi: float
    sma9: float
    sma20: float
    sma50: float
    sma200: float
    atr: float
    atr_avg20: float
    support: float
    resistance: float
    trend: str
    candles_recent: list[dict] = []


class AnalysisScore(BaseModel):
    """Score SMC de un par."""
    symbol: str
    score: float
    strength_base: float
    strength_quote: float


class PairAnalysis(BaseModel):
    """Análisis técnico del mejor par."""
    best_pair: str
    price: float
    scores: list[AnalysisScore]
    technical: TechnicalAnalysis
    smc_active: bool
    smc_direction: str


class LLMContextRequest(BaseModel):
    """Request para preparar contexto completo para LLM agents."""
    cycle_id: str
    prices: dict[str, float]
    news: list[NewsItem]
    calendar: list[CalendarEvent]
    positions: list[Position]
    pair_analysis: PairAnalysis


class LLMContextResponse(BaseModel):
    """Contexto completo para LLM agents."""
    cycle_id: str
    prices: dict[str, float]
    news: list[NewsItem]
    calendar: list[CalendarEvent]
    positions: list[Position]
    best_pair: str
    price: float
    scores: list[AnalysisScore]
    technical: TechnicalAnalysis
    smc_active: bool
    smc_direction: str
    pair_context: dict = {}

from pydantic import BaseModel
from models.risk import LLMSignal


class DebateRequest(BaseModel):
    cycle_id: str
    best_pair: str
    price: float
    rsi: float
    trend: str
    atr: float
    signals: list[LLMSignal]
    news_summary: str = ""
    calendar_summary: str = ""
    drivers: str = ""
    fundamental_hint: str = ""
    sentiment_hint: str = ""


class DebateResponse(BaseModel):
    signal: str        # "buy" | "sell" | "neutral"
    confidence: float
    reasoning: str
    bull_argument: str
    bear_argument: str
    debate_used: bool = True

from pydantic import BaseModel
from models.analysis import TechnicalData
from models.context import Position


class LLMSignal(BaseModel):
    agent: str          # "technical" | "fundamental" | "sentiment"
    signal: str         # "buy" | "sell" | "neutral"
    confidence: float   # 0.0 – 1.0
    parse_error: bool = False


class RiskEvaluateRequest(BaseModel):
    cycle_id: str
    best_pair: str
    price: float
    technical: TechnicalData
    llm_signals: list[LLMSignal]
    positions: list[Position] = []


class RiskEvaluateResponse(BaseModel):
    action: str         # "buy" | "sell" | "skip"
    entry: float
    sl: float
    tp: float
    volume: float
    confidence: float
    reason: str

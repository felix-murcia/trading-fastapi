from pydantic import BaseModel


class Candle(BaseModel):
    time: int       # Unix timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class TechnicalData(BaseModel):
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
    trend: str      # "bullish" | "bearish" | "range"
    candles_recent: list[Candle]


class PairScore(BaseModel):
    symbol: str
    score: float
    strength_base: float
    strength_quote: float


class PairsAnalysisRequest(BaseModel):
    cycle_id: str
    prices: dict[str, float]           # { "EURUSD": 1.1234, ... } — candles se obtienen internamente desde MT5


class PairsAnalysisResponse(BaseModel):
    best_pair: str
    price: float
    scores: list[PairScore]
    technical: TechnicalData

from pydantic import BaseModel


class NewsItem(BaseModel):
    headline: str = ""
    currency: str = ""
    impact: str = "low"
    summary: str = ""
    sentiment: str = ""
    source_url: str = ""
    timestamp: str = ""
    minutes_to_event: int | None = None


class CalendarEvent(BaseModel):
    event: str = ""
    currency: str = ""
    impact: str = "low"
    date: str = ""
    time: str = ""
    utc_offset: str = ""
    forecast: str = ""
    actual: str = ""
    previous: str = ""
    source_url: str = ""
    timestamp: str = ""


class Position(BaseModel):
    symbol: str
    type: str     # BUY | SELL | BUY_LIMIT | SELL_LIMIT | etc.
    volume: float
    price: float
    ticket: int | None = None


class ContextValidateRequest(BaseModel):
    cycle_id: str
    prices: dict[str, float]           # { "EURUSD": 1.1234, ... }
    news: list[NewsItem] = []
    calendar: list[CalendarEvent] = []
    positions: list[Position] = []


class ContextValidateResponse(BaseModel):
    valid: bool
    reason: str
    session: str    # "london" | "new_york" | "overlap" | "closed"
    risk_flags: list[str]

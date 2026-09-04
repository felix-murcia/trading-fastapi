from pydantic import BaseModel


class OrderPrepareRequest(BaseModel):
    cycle_id: str
    symbol: str
    type: str       # "BUY" | "SELL"
    entry: float
    sl: float
    tp: float
    volume: float


class OrderPrepareResponse(BaseModel):
    approved: bool
    order_payload: dict
    rejection_reason: str


class AuditLogRequest(BaseModel):
    cycle_id: str
    event: str
    data: dict = {}


class AuditLogResponse(BaseModel):
    id: int

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
    order_payload: dict     # firmado con HMAC, listo para MT5
    rejection_reason: str


class OrderConfirmRequest(BaseModel):
    cycle_id: str
    mt5_order_id: str
    status: str     # "placed" | "filled" | "rejected" | "unconfirmed"
    fill_price: float | None = None


class OrderConfirmResponse(BaseModel):
    logged: bool


class AuditLogRequest(BaseModel):
    cycle_id: str
    event: str
    data: dict = {}


class AuditLogResponse(BaseModel):
    id: int


class OrdersCleanupRequest(BaseModel):
    max_age_hours: int = 48


class OrdersCleanupResponse(BaseModel):
    cancelled: list[int]
    errors: list[str]

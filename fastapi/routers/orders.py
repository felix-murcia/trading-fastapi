from fastapi import APIRouter, Depends
from models.orders import (
    AuditLogRequest,
    AuditLogResponse,
    OrderPrepareRequest,
    OrderPrepareResponse,
)
from services import order_manager
from .deps import verify_token

router = APIRouter(prefix="/v1", tags=["orders"])


@router.post("/orders/prepare", response_model=OrderPrepareResponse)
async def prepare_order(req: OrderPrepareRequest, _: None = Depends(verify_token)):
    """Gate de órdenes (P0-3): idempotencia por cycle_id, Risk Guardian,
    validadores geométricos, registro pending y payload firmado (HMAC).
    El EA solo ejecuta trade.Buy/Sell si `approved == true`."""
    return await order_manager.prepare(req)


@router.post("/audit/log", response_model=AuditLogResponse)
async def log_audit(
    req: AuditLogRequest,
    _: None = Depends(verify_token),
) -> AuditLogResponse:
    return await order_manager.audit_log(req)

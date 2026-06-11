from fastapi import APIRouter, Depends
from models.orders import AuditLogRequest, AuditLogResponse
from services import order_manager
from .deps import verify_token

router = APIRouter(prefix="/v1", tags=["orders"])


@router.post("/audit/log", response_model=AuditLogResponse)
async def log_audit(
    req: AuditLogRequest,
    _: None = Depends(verify_token),
) -> AuditLogResponse:
    return await order_manager.audit_log(req)

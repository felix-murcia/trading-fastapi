from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from models.orders import (
    OrderPrepareRequest, OrderPrepareResponse,
    OrderConfirmRequest, OrderConfirmResponse,
    AuditLogRequest, AuditLogResponse,
    OrdersCleanupRequest, OrdersCleanupResponse,
)
from services import order_manager, mt5_client
from db.connection import get_pool
from .deps import verify_token


class OrderExecuteRequest(BaseModel):
    cycle_id: str


class OrderExecuteResponse(BaseModel):
    mt5_order_id: str
    status: str
    fill_price: float | None = None

router = APIRouter(prefix="/v1", tags=["orders"])


@router.post("/order/prepare", response_model=OrderPrepareResponse)
async def prepare_order(
    req: OrderPrepareRequest,
    _: None = Depends(verify_token),
) -> OrderPrepareResponse:
    return await order_manager.prepare(req)


@router.post("/order/confirm", response_model=OrderConfirmResponse)
async def confirm_order(
    req: OrderConfirmRequest,
    _: None = Depends(verify_token),
) -> OrderConfirmResponse:
    return await order_manager.confirm(req)


@router.post("/audit/log", response_model=AuditLogResponse)
async def log_audit(
    req: AuditLogRequest,
    _: None = Depends(verify_token),
) -> AuditLogResponse:
    return await order_manager.audit_log(req)


@router.post("/order/execute", response_model=OrderExecuteResponse)
async def execute_order(
    req: OrderExecuteRequest,
    _: None = Depends(verify_token),
) -> OrderExecuteResponse:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, symbol, type, entry, sl, tp, volume "
        "FROM orders WHERE cycle_id=$1 AND status='pending'",
        req.cycle_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="no_pending_order_for_cycle")

    try:
        result = await mt5_client.place_order(
            symbol=row["symbol"],
            order_type=row["type"],
            volume=float(row["volume"]),
            price=float(row["entry"]),
            sl=float(row["sl"]),
            tp=float(row["tp"]),
            comment=req.cycle_id,
        )
    except Exception as e:
        await pool.execute(
            "UPDATE orders SET status='rejected' WHERE id=$1", row["id"]
        )
        raise HTTPException(status_code=502, detail=f"mt5_rejected: {e}")

    ticket = str(result.get("ticket", ""))
    await pool.execute(
        "UPDATE orders SET mt5_order_id=$2, status='placed' WHERE id=$1",
        row["id"], ticket,
    )
    return OrderExecuteResponse(mt5_order_id=ticket, status="placed")


@router.post("/orders/cleanup", response_model=OrdersCleanupResponse)
async def cleanup_orders(
    req: OrdersCleanupRequest,
    _: None = Depends(verify_token),
) -> OrdersCleanupResponse:
    return await order_manager.cleanup(req)

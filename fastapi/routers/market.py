from fastapi import APIRouter, Depends, HTTPException
from services import mt5_client
from .deps import verify_token

router = APIRouter(prefix="/v1/market", tags=["market"])


@router.get("/prices")
async def get_prices(_: None = Depends(verify_token)) -> dict:
    try:
        return await mt5_client.get_prices()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"mt5_client_error: {e}")


@router.get("/positions")
async def get_positions(_: None = Depends(verify_token)) -> dict:
    try:
        return await mt5_client.get_positions()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"mt5_client_error: {e}")

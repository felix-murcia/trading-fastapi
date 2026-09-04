"""
Router: /api/v1/trade-journal
_GET /insights?symbol=EURUSD&limit=10_
"""
from fastapi import APIRouter, Header, HTTPException, Query

from services.trade_journal import get_recent_insights
from config import settings

router = APIRouter(prefix="/api/v1/trade-journal", tags=["journal"])


@router.get("/insights")
async def list_insights(
    x_internal_token: str = Header(...),
    symbol: str = Query("EURUSD"),
    limit: int = Query(10, ge=1, le=100),
):
    """Retrieves recent trade insights from the journal database."""
    if x_internal_token != settings.internal_token:
        raise HTTPException(status_code=401, detail="Invalid internal token")
    rows = await get_recent_insights(symbol=symbol, limit=limit)
    return {"symbol": symbol, "count": len(rows), "insights": rows}

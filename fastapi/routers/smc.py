from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db.connection import get_pool
from .deps import verify_token

router = APIRouter(prefix="/v1/smc", tags=["smc"])


class SMCSignalIn(BaseModel):
    symbol: str
    entry_zone: bool
    direction: str | None = None   # "buy" | "sell"
    zone_high: float | None = None
    zone_low: float | None = None
    timeframe: str | None = None
    source: str = "brain_smc_ultimate"


class SMCSignalOut(BaseModel):
    symbol: str
    entry_zone: bool
    direction: str | None
    zone_high: float | None
    zone_low: float | None
    timeframe: str | None
    source: str
    received_at: str


@router.post("/signal", response_model=SMCSignalOut)
async def upsert_signal(
    req: SMCSignalIn,
    _: None = Depends(verify_token),
) -> SMCSignalOut:
    pool = get_pool()
    row = await pool.fetchrow(
        """INSERT INTO smc_signals
               (symbol, entry_zone, direction, zone_high, zone_low, timeframe, source, received_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
           ON CONFLICT (symbol) DO UPDATE SET
               entry_zone  = EXCLUDED.entry_zone,
               direction   = EXCLUDED.direction,
               zone_high   = EXCLUDED.zone_high,
               zone_low    = EXCLUDED.zone_low,
               timeframe   = EXCLUDED.timeframe,
               source      = EXCLUDED.source,
               received_at = NOW()
           RETURNING *""",
        req.symbol, req.entry_zone, req.direction,
        req.zone_high, req.zone_low, req.timeframe, req.source,
    )
    return SMCSignalOut(**{**dict(row), "received_at": row["received_at"].isoformat()})


@router.get("/signal", response_model=SMCSignalOut)
async def get_signal(
    symbol: str,
    _: None = Depends(verify_token),
) -> SMCSignalOut:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM smc_signals WHERE symbol=$1", symbol
    )
    if not row:
        # Sin señal registrada → entry_zone=False por defecto
        return SMCSignalOut(
            symbol=symbol, entry_zone=False, direction=None,
            zone_high=None, zone_low=None, timeframe=None,
            source="none", received_at="",
        )
    return SMCSignalOut(**{**dict(row), "received_at": row["received_at"].isoformat()})

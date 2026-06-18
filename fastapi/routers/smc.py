import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db.connection import get_pool
from services import simple_pipeline, mt5_client
from config import settings
from .deps import verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/smc", tags=["smc"])


class SMCSignalIn(BaseModel):
    symbol: str
    entry_zone: bool
    direction: str | None = None   # "buy" | "sell"
    zone_high: float | None = None
    zone_low: float | None = None
    timeframe: str | None = None
    source: str = "brain_smc_ultimate"
    signal_id: str | None = None   # nombre del objeto del gráfico (p.ej. QT_L_B_1781172300)


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

    # Flujo simple: señal → orden directa (sin LLM)
    # Las protecciones (idempotencia por signal_id, cooldown, validaciones)
    # están dentro de process_signal — nunca lanza, no rompe el POST del EA.
    if (
        settings.simple_pipeline_enabled
        and req.entry_zone
        and req.direction in ("buy", "sell")
    ):
        await simple_pipeline.process_signal(
            req.symbol, req.direction, req.signal_id, req.zone_high,
            signal_price=req.zone_low,
        )

    return SMCSignalOut(**{**dict(row), "received_at": row["received_at"].isoformat()})


class CloseRequest(BaseModel):
    symbol: str
    reason: str = "ema_cross"


@router.post("/close")
async def close_position(req: CloseRequest, _: None = Depends(verify_token)):
    try:
        positions = await mt5_client.get_positions()
        open_pos = [p for p in positions.get("open", []) if p["symbol"] == req.symbol]
        if not open_pos:
            return {"action": "skip", "reason": "no_open_position"}
        await mt5_client.close_positions_by_symbol(req.symbol)
        pool = get_pool()
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
            f"close_{req.symbol}_{req.reason}",
            "position_closed_ema",
            json.dumps({"symbol": req.symbol, "reason": req.reason,
                        "closed_tickets": [p["ticket"] for p in open_pos]}),
        )
        logger.info("[CLOSE] %s closed %d position(s) reason=%s",
                    req.symbol, len(open_pos), req.reason)
        return {"action": "closed", "tickets": [p["ticket"] for p in open_pos]}
    except Exception as exc:
        logger.error("[CLOSE] failed %s: %s", req.symbol, exc, exc_info=True)
        return {"action": "error", "reason": str(exc)}


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

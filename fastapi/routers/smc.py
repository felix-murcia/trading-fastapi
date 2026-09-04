import json
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db.connection import get_pool
from services import simple_pipeline, mt5_client, news_filter
from services.auto_retrain import record_trade_filled
from config import settings
from .deps import verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/smc", tags=["smc"])


class SMCSignalIn(BaseModel):
    symbol: str
    entry_zone: bool
    direction: str | None = None   # "buy" | "sell"
    price: float | None = None
    zone_high: float | None = None
    zone_low: float | None = None
    sl_anchor: float | None = None
    tp_anchor: float | None = None
    timeframe: str | None = None
    source: str = "brain_smc_ultimate"
    signal_id: str | None = None   # nombre del objeto del gráfico (p.ej. QT_L_B_1781172300)
    use_news_filter: bool = True


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
            req.symbol, req.direction, req.signal_id, req.price or req.zone_high,
            signal_price=req.zone_low,
            sl_anchor=req.sl_anchor,
            tp_anchor=req.tp_anchor,
            use_news_filter=req.use_news_filter,
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

        # Recopilar datos de cada posición antes de cerrarla (para auto-retrain)
        for p in open_pos:
            try:
                await record_trade_filled(
                    symbol=req.symbol,
                    entry_time=p.get("time_open", 0) or time.time(),
                    exit_time=time.time(),
                    pnl=p.get("profit", 0.0),
                    pnl_pct=p.get("profit", 0.0) / 1000.0,  # Aproximado
                    direction="LONG" if p.get("type", "").lower() in ("buy", "long") else "SHORT",
                    sl_hit=False,
                    tp_hit=False,
                    exit_reason=req.reason,
                )
            except Exception as rf_exc:
                logger.warning("[CLOSE] record_trade_filled falló: %s", rf_exc)

        await mt5_client.close_positions_by_symbol(req.symbol)
        pool = get_pool()
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
            f"close_{req.symbol}_{req.reason}",
            "position_closed",
            json.dumps({"symbol": req.symbol, "reason": req.reason,
                        "closed_tickets": [p["ticket"] for p in open_pos]}),
        )
        logger.info("[CLOSE] %s closed %d position(s) reason=%s",
                    req.symbol, len(open_pos), req.reason)
        return {"action": "closed", "tickets": [p["ticket"] for p in open_pos]}
    except Exception as exc:
        logger.error("[CLOSE] failed %s: %s", req.symbol, exc, exc_info=True)
        return {"action": "error", "reason": str(exc)}


class NewsCheckRequest(BaseModel):
    symbol: str

@router.post("/news-check")
async def news_check(req: NewsCheckRequest, _: None = Depends(verify_token)):
    """Cierre proactivo de posiciones antes de noticias de alto impacto.
    El EA llama a este endpoint cada ~5 min."""
    if not settings.news_filter_enabled:
        return {"action": "skip", "reason": "news_filter_disabled"}

    try:
        positions = await mt5_client.get_positions()
        is_open = any(p["symbol"] == req.symbol for p in positions.get("open", []))
        if not is_open:
            return {"action": "skip", "reason": "no_open_positions"}
        open_symbols = [req.symbol]
    except Exception as exc:
        logger.error("[NEWS-CHECK] Failed to get positions: %s", exc)
        return {"action": "error", "reason": str(exc)}

    if not open_symbols:
        return {"action": "skip", "reason": "no_open_positions"}

    upcoming = await news_filter.get_upcoming_news(open_symbols)
    if not upcoming:
        return {"action": "skip", "reason": "no_upcoming_news"}

    closed = []
    for event in upcoming:
        for sym in event["affected_symbols"]:
            if sym not in open_symbols:
                continue
            try:
                await mt5_client.close_positions_by_symbol(sym)
                pool = get_pool()
                await pool.execute(
                    "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
                    f"news_close_{sym}_{int(time.time())}",
                    "position_closed_news",
                    json.dumps({"symbol": sym, "event": event["title"],
                                "time_utc": event["time_utc"],
                                "minutes_until": event["minutes_until"]}),
                )
                closed.append({"symbol": sym, "event": event["title"],
                               "minutes_until": event["minutes_until"]})
                open_symbols.remove(sym)
                logger.info("[NEWS-CHECK] Closed %s — '%s' in %.0f min",
                            sym, event["title"], event["minutes_until"])
            except Exception as exc:
                logger.error("[NEWS-CHECK] Failed to close %s: %s", sym, exc)

    return {"action": "closed" if closed else "skip",
            "closed": closed, "upcoming_events": upcoming}


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

@router.get("/audit")
async def get_audit_log(limit: int = 50, _: None = Depends(verify_token)):
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, cycle_id, event, data, created_at FROM audit_log ORDER BY id DESC LIMIT $1", limit
    )
    return [
        {
            "id": r["id"],
            "cycle_id": r["cycle_id"],
            "event": r["event"],
            "data": json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] if r["data"] else {}),
            "created_at": r["created_at"].isoformat()
        } for r in rows
    ]

@router.get("/news")
async def get_all_news(_: None = Depends(verify_token)):
    """Retorna todas las noticias de alto impacto cacheadas."""
    from services.news_filter import _fetch_calendar
    from datetime import timedelta
    events = await _fetch_calendar()
    
    delta = timedelta(minutes=settings.news_blackout_minutes)
    
    # Formatear a JSON serializable
    return [
        {
            "title": ev["title"],
            "currency": ev["currency"],
            "time_utc": ev["time_utc"].isoformat(),
            "start_time_utc": (ev["time_utc"] - delta).isoformat(),
            "end_time_utc": (ev["time_utc"] + delta).isoformat()
        }
        for ev in events
    ]

@router.get("/analytics")
async def get_analytics(_: None = Depends(verify_token)):
    import httpx
    from collections import defaultdict
    from datetime import datetime
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(f"{settings.mt5_http_url}/api/v1/history/deals")
            deals = res.json()
            res_acc = await client.get(f"{settings.mt5_http_url}/api/v1/account/info")
            account_info = res_acc.json() if res_acc.status_code == 200 else {}
        except Exception as e:
            logger.error(f"Error fetching deals for stats: {e}")
            return {"error": str(e)}

    daily_pnl = defaultdict(float)
    wins = 0
    losses = 0
    by_symbol = defaultdict(float)
    history = []
    cumulative = 0.0

    current_balance = account_info.get("balance", 0.0)
    best_trade = 0.0
    worst_trade = 0.0
    current_win_streak = 0
    max_win_streak = 0
    current_loss_streak = 0
    max_loss_streak = 0
    current_consec_profit = 0.0
    max_consec_profit = 0.0
    profit_7d = 0.0
    
    deals_sorted = sorted(deals, key=lambda x: x.get("time_msc", 0))

    for d in deals_sorted:
        if d.get("entry") != 1:
            continue
        
        pnl = d.get("profit", 0.0) + d.get("swap", 0.0) + d.get("commission", 0.0)
        time_msc = d.get("time_msc")
        if not time_msc:
            continue
        
        dt = datetime.fromtimestamp(time_msc / 1000.0)
        date_str = dt.strftime("%Y-%m-%d")

        daily_pnl[date_str] += pnl
        by_symbol[d.get("symbol", "UNKNOWN")] += pnl
        
        if pnl > 0:
            wins += 1
            current_win_streak += 1
            current_loss_streak = 0
            current_consec_profit += pnl
            if current_win_streak > max_win_streak: max_win_streak = current_win_streak
            if current_consec_profit > max_consec_profit: max_consec_profit = current_consec_profit
        elif pnl <= 0:
            if pnl < 0:
                losses += 1
                current_loss_streak += 1
                current_win_streak = 0
                current_consec_profit = 0.0
                if current_loss_streak > max_loss_streak: max_loss_streak = current_loss_streak
            else:
                losses += 1

        if pnl > best_trade: best_trade = pnl
        if pnl < worst_trade: worst_trade = pnl
        profit_7d += pnl
            
        cumulative += pnl
        history.append({"time": date_str + " " + dt.strftime("%H:%M"), "cumulative": cumulative})

    return {
        "daily_pnl": dict(sorted(daily_pnl.items())),
        "wins": wins,
        "losses": losses,
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda x: x[1], reverse=True)),
        "evolution": history,
        "kpis": {
            "balance": current_balance,
            "profit_7d": profit_7d,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "max_consec_profit": max_consec_profit
        }
    }

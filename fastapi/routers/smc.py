import json
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from core.container import build_registry_from_settings
from core.rules import is_winning_close
from db.connection import get_pool
from config import settings
from .deps import verify_token
from services.error_taxonomy import LOOP_ERRORS
from services.timebase import broker_offset_hours

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/smc", tags=["smc"])


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
async def get_analytics(
    _: None = Depends(verify_token),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    granularity: str = "D",
    symbol: str | None = None,
):
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    VALID_GRANULARITIES = {"M", "H", "D", "MN"}

    if granularity not in VALID_GRANULARITIES:
        raise HTTPException(
            status_code=400,
            detail=f"granularity inválida: {granularity}. Use M, H, D o MN.",
        )

    date_from = None
    date_to = None
    if from_date is not None:
        try:
            date_from = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail=f"from inválida: {from_date}. Use formato ISO (YYYY-MM-DD).",
            )
    if to_date is not None:
        try:
            date_to = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail=f"to inválida: {to_date}. Use formato ISO (YYYY-MM-DD).",
            )

    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=400,
            detail=f"from ({from_date}) posterior a to ({to_date}).",
        )

    registry = build_registry_from_settings()
    market_port = registry.get("EURUSD").market
    if market_port is None:
        enabled = registry.enabled()
        if not enabled:
            return {"error": "PAIRS: ningún par habilitado para el analytics"}
        market_port = enabled[0].market

    try:
        deals = await market_port.get_deals()
        account = await market_port.get_account()
        account_info = account if isinstance(account, dict) else {}

        # D-3 (R-2): el desfase broker→UTC se detecta a través del puerto de
        # mercado (`timebase.broker_offset_hours`, B6.2) y se reutiliza para
        # agrupar los deals por día UTC real, alineada con el límite diario
        # de riesgo.
        broker_offset_h = 0
        if deals:
            sym = deals[0].get("symbol", "EURUSD")
            broker_offset_h = await broker_offset_hours(sym, market_port)
            # Sin vela detectable no se agrupa en falso: con deals pero sin
            # feed de velas no hay offset fiable, y agrupar con 0 sería un
            # silent fallback (D-3 / R-2: el día debe ser UTC real).
            offset_candles = await market_port.get_candles(sym, "H1", 1)
            if not offset_candles:
                logger.error(
                    "[ANALYTICS] deals sin velas H1 detectables → sin offset "
                    "broker→UTC fiable; no se agrupa"
                )
                return {"error": "no hay velas H1 para detectar el desfase broker→UTC"}
    except LOOP_ERRORS as e:
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

        # Filtro por símbolo si se especifica
        if symbol is not None and d.get("symbol") != symbol:
            continue

        pnl = d.get("profit", 0.0) + d.get("swap", 0.0) + d.get("commission", 0.0)
        time_msc = d.get("time_msc")
        if not time_msc:
            continue

        # D-3: epoch broker → UTC real (offset detectado arriba); la ventana
        # del P&L diario queda alineada con la del límite de riesgo.
        dt = datetime.fromtimestamp(time_msc / 1000.0, tz=timezone.utc) - timedelta(hours=broker_offset_h)

        # Filtro por rango de fechas (UTC real)
        if date_from is not None and dt < date_from:
            continue
        if date_to is not None and dt > date_to:
            continue

        # Agrupación por granularidad
        if granularity == "D":
            bucket = dt.strftime("%Y-%m-%d")
        elif granularity == "H":
            bucket = dt.strftime("%Y-%m-%d %H:00")
        elif granularity == "M":
            bucket = dt.strftime("%Y-%m-%d %H:%M")
        elif granularity == "MN":
            bucket = dt.strftime("%Y-%m")

        daily_pnl[bucket] += pnl
        by_symbol[d.get("symbol", "UNKNOWN")] += pnl

        # Convención AGENTS.md: cierre < MIN_WIN_EUR (3€) = pérdida, aunque
        # el pnl en euros sea positivo (no cubre spread/slippage reales).
        if is_winning_close(pnl):
            wins += 1
            current_win_streak += 1
            current_loss_streak = 0
            current_consec_profit += pnl
            if current_win_streak > max_win_streak: max_win_streak = current_win_streak
            if current_consec_profit > max_consec_profit: max_consec_profit = current_consec_profit
        else:
            losses += 1
            current_loss_streak += 1
            current_win_streak = 0
            current_consec_profit = 0.0
            if current_loss_streak > max_loss_streak: max_loss_streak = current_loss_streak

        if pnl > best_trade: best_trade = pnl
        if pnl < worst_trade: worst_trade = pnl
        profit_7d += pnl

        cumulative += pnl
        history.append({"time": bucket, "cumulative": cumulative})

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
            "max_consec_profit": max_consec_profit,
        },
        "granularity": granularity,
        "symbol": symbol,
    }

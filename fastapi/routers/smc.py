import json
import logging
from fastapi import APIRouter, Depends
from core.rules import is_winning_close
from db.connection import get_pool
from services.market_time import detect_utc_offset_hours
from config import settings
from .deps import verify_token
from services.error_taxonomy import LOOP_ERRORS

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
async def get_analytics(_: None = Depends(verify_token)):
    import httpx
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(f"{settings.mt5_http_url}/api/v1/history/deals")
            deals = res.json()
            res_acc = await client.get(f"{settings.mt5_http_url}/api/v1/account/info")
            account_info = res_acc.json() if res_acc.status_code == 200 else {}

            # D-3 (R-2): el límite diario de riesgo agrupa por UTC real
            # (risk_state_store/risk_guardian: now(utc).date()), pero los
            # time_msc de MT5 son reloj BROKER vendido como epoch — verificado
            # 2026-09-30: orders.created_at 06:50:17.456 (UTC real, DB) ↔
            # deal de apertura 09:50:17.535 (epoch→UTC) = +3h exactos, mismos
            # ms. Sin corregir, un cierre de 21:00–00:00 UTC cae en el día
            # broker: el dashboard y el límite miden ventanas distintas.
            # El desfase se detecta con la vela formándose (market_time,
            # auto-corrector ya probado en P2-3): 0 si el feed pasa a sellar
            # UTC honesto o cambia el DST del broker — cero hardcode. Si no
            # hay vela detectable no se agrupa en falso: se propaga el error
            # (convención: sin fallbacks silenciosos).
            broker_offset_h = 0
            if deals:
                res_cdl = await client.get(
                    f"{settings.mt5_http_url}/api/v1/market/candles/latest"
                    f"?symbol_name={deals[0]['symbol']}&timeframe=H1&count=1",
                )
                if res_cdl.status_code != 200:
                    raise RuntimeError(
                        f"candles/latest HTTP {res_cdl.status_code} (desfase broker→UTC)"
                    )
                raw = res_cdl.json()
                cdl = raw if isinstance(raw, list) else raw.get("candles", [])
                t = cdl[0]["time"] if cdl else None
                if isinstance(t, (int, float)):
                    candle_time = datetime.fromtimestamp(t, tz=timezone.utc)
                elif isinstance(t, str):
                    candle_time = datetime.fromisoformat(t.replace("Z", "+00:00"))
                else:
                    raise ValueError(f"vela sin 'time' para desfase broker→UTC: {t!r}")
                broker_offset_h = detect_utc_offset_hours(candle_time)
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
        
        pnl = d.get("profit", 0.0) + d.get("swap", 0.0) + d.get("commission", 0.0)
        time_msc = d.get("time_msc")
        if not time_msc:
            continue
        
        # D-3: epoch broker → UTC real (offset detectado arriba); la ventana
        # del P&L diario queda alineada con la del límite de riesgo.
        dt = datetime.fromtimestamp(time_msc / 1000.0, tz=timezone.utc) - timedelta(hours=broker_offset_h)
        date_str = dt.strftime("%Y-%m-%d")

        daily_pnl[date_str] += pnl
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

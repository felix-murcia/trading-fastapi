import json
import logging
import time
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.rules import is_winning_close
from db.connection import get_pool
from services import mt5_client, news_filter
from services.auto_retrain import record_trade_filled
from services.market_time import detect_utc_offset_hours
from services.position_ownership import close_positions_owned
from config import settings
from .deps import verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/smc", tags=["smc"])


class CloseRequest(BaseModel):
    symbol: str
    reason: str = "ema_cross"


# Dominio de trade_outcomes.exit_reason (CHECK en db/schema.sql) — un reason
# fuera de dominio rompe el INSERT con asyncpg (antes: la violación se tragaba
# el except y el cierre quedaba SIN persistir en auto-retrain, solo en log).
_EXIT_REASONS = {"sl", "tp", "manual", "news", "signal_end", "ea"}


def _exit_reason_for_record(reason: str) -> str:
    """Reason válido para trade_outcomes: fuera de dominio → 'signal_end'
    (el cierre lo manda la estrategia al terminar su señal)."""
    return reason if reason in _EXIT_REASONS else "signal_end"


async def _record_closed_for_retrain(
    symbol: str, closed_positions: list[dict], reason: str
) -> None:
    """Auto-retrain: registra SOLO los cierres realmente ejecutados de ESTA
    estrategia (P2-4b). Un cierre fallido no genera trade."""
    exit_reason = _exit_reason_for_record(reason)
    for p in closed_positions:
        try:
            await record_trade_filled(
                symbol=symbol,
                entry_time=p.get("time_open", 0) or time.time(),
                exit_time=time.time(),
                pnl=p.get("profit", 0.0),
                pnl_pct=p.get("profit", 0.0) / 1000.0,  # Aproximado
                direction="LONG" if p.get("type", "").lower() in ("buy", "long") else "SHORT",
                sl_hit=False,
                tp_hit=False,
                exit_reason=exit_reason,
            )
        except Exception as rf_exc:
            logger.warning("[SMC] record_trade_filled falló (%s): %s", symbol, rf_exc)


@router.post("/close")
async def close_position(req: CloseRequest, _: None = Depends(verify_token)):
    try:
        positions = await mt5_client.get_positions()
        open_pos = [p for p in positions.get("open", []) if p["symbol"] == req.symbol]
        if not open_pos:
            return {"action": "skip", "reason": "no_open_position"}

        # P2-4b: cerrar SOLO las posiciones de ESTA estrategia (ledger
        # orders.cycle_id 'smc_%') — la posición del EA (magic 90001) es otro
        # mundo: su ticket no está en nuestro ledger y jamás se cierra aquí.
        result = await close_positions_owned(req.symbol, open_pos)
        closed_set = set(result["closed"])

        # Auto-retrain: solo los cierres REALMENTE ejecutados. Antes se
        # registraba cualquier posición del símbolo (incluida la del EA) y
        # también si el cierre fallaba → datos de retrain ajenos/fantasma.
        await _record_closed_for_retrain(
            req.symbol,
            [p for p in open_pos if p.get("ticket") in closed_set],
            req.reason,
        )

        pool = get_pool()
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
            f"close_{req.symbol}_{req.reason}",
            "position_closed",
            json.dumps({"symbol": req.symbol, "reason": req.reason,
                        "closed_tickets": result["closed"],
                        "skipped_foreign": result["foreign"],
                        "failed": result["failed"]}),
        )
        if not result["closed"]:
            # Nada propio que cerrar: solo posiciones ajenas (EA) y/o fallos.
            reason = "close_failed" if result["failed"] else "no_owned_position"
            logger.info("[CLOSE] %s sin cierre propio (reason=%s, ajenas=%d)",
                        req.symbol, reason, len(result["foreign"]))
            return {"action": "skip", "reason": reason,
                    "foreign": result["foreign"], "failed": result["failed"]}
        logger.info("[CLOSE] %s closed %d position(s) reason=%s (ajenas=%d, fallos=%d)",
                    req.symbol, len(result["closed"]), req.reason,
                    len(result["foreign"]), len(result["failed"]))
        return {"action": "closed", "tickets": result["closed"],
                "foreign": result["foreign"], "failed": result["failed"]}
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
                # P2-4b: solo las posiciones de ESTA estrategia (ledger 'smc_%').
                # La del EA es otro mundo: su protección en noticias es su
                # SL/TP + el blackout de entradas del propio EA.
                candidates = [p for p in positions.get("open", [])
                              if p.get("symbol") == sym]
                result = await close_positions_owned(sym, candidates)
                if not result["closed"]:
                    logger.info(
                        "[NEWS-CHECK] %s — sin cierre propio (ajenas=%d, fallos=%d)",
                        sym, len(result["foreign"]), len(result["failed"]),
                    )
                    if not result["failed"]:
                        open_symbols.remove(sym)  # nada propio que proteger
                    continue
                # Convención AGENTS.md: todo cierre propio se persiste en
                # trade_outcomes (antes estos cierres no se registraban NUNCA).
                await _record_closed_for_retrain(
                    sym,
                    [p for p in candidates if p.get("ticket") in set(result["closed"])],
                    "news",
                )
                pool = get_pool()
                await pool.execute(
                    "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
                    f"news_close_{sym}_{int(time.time())}",
                    "position_closed_news",
                    json.dumps({"symbol": sym, "event": event["title"],
                                "time_utc": event["time_utc"],
                                "minutes_until": event["minutes_until"],
                                "closed_tickets": result["closed"],
                                "skipped_foreign": result["foreign"],
                                "failed": result["failed"]}),
                )
                closed.append({"symbol": sym, "event": event["title"],
                               "minutes_until": event["minutes_until"],
                               "tickets": result["closed"],
                               "foreign": len(result["foreign"])})
                open_symbols.remove(sym)
                logger.info("[NEWS-CHECK] Closed %d on %s — '%s' in %.0f min (%d ajenas no tocadas)",
                            len(result["closed"]), sym, event["title"],
                            event["minutes_until"], len(result["foreign"]))
            except Exception as exc:
                logger.error("[NEWS-CHECK] Failed to close %s: %s", sym, exc)

    return {"action": "closed" if closed else "skip",
            "closed": closed, "upcoming_events": upcoming}


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

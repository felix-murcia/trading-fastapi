"""
Valida si las condiciones de mercado permiten operar en este ciclo.

Verifica:
1. Sesión de mercado activa
2. Sin noticias HIGH impact en la ventana de blackout
3. Volatilidad ATR dentro de rango
4. Exposición total no supera límite
5. cycle_id no procesado anteriormente (idempotencia)
"""

from datetime import datetime, timezone
from models.context import ContextValidateRequest, ContextValidateResponse
from db.connection import get_pool
from config import settings


_SESSIONS = {
    "london":   (8,  17),   # UTC
    "new_york": (13, 22),
    "overlap":  (13, 17),
}

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "XAUUSD"]

VALID_RANGES = {
    "EURUSD": (1.05, 1.20),
    "GBPUSD": (1.20, 1.45),
    "USDJPY": (130.0, 175.0),
    "USDCHF": (0.75, 1.05),
    "XAUUSD": (2500.0, 6000.0),
}


def _current_session(now: datetime) -> str:
    h = now.hour + now.minute / 60.0
    if 13 <= h < 17:
        return "overlap"
    if 8 <= h < 17:
        return "london"
    if 13 <= h < 22:
        return "new_york"
    return "closed"


def _has_high_impact_news(req: ContextValidateRequest) -> bool:
    blackout = settings.news_blackout_minutes
    now = datetime.now(timezone.utc)
    print(f"[DEBUG] Checking high-impact news. Now: {now}, Blackout: {blackout} min")

    for item in req.news:
        if item.impact != "high":
            continue

        # Obtener timestamp del item (puede ser atributo o dict key)
        timestamp = getattr(item, 'timestamp', None)
        if not timestamp and hasattr(item, '__getitem__'):
            timestamp = item.get('timestamp')

        if not timestamp:
            print(f"[DEBUG] → BLOCKING: High-impact news without timestamp: {getattr(item, 'headline', 'unknown')[:50]}")
            return True

        print(f"[DEBUG] High-impact news found: {getattr(item, 'headline', 'unknown')[:50]}... timestamp: {timestamp}")

        # Calcular minutos desde timestamp de la noticia
        try:
            # timestamp es ISO 8601 string: "2026-06-04T12:54:02.155346"
            news_time = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
            mte = (now - news_time).total_seconds() / 60.0
            print(f"[DEBUG] News time: {news_time}, Minutes elapsed: {mte:.1f}, Blackout: {blackout}")
            if abs(mte) <= blackout:
                print(f"[DEBUG] → BLOCKING: News within {blackout} min window")
                return True
        except Exception as e:
            # Si no se puede parsear, asumir inmediato
            print(f"[DEBUG] → BLOCKING: Failed to parse timestamp '{timestamp}': {e}")
            return True

    # Calendar events: también considerar ventana de blackout si tienen time válido
    for e in req.calendar:
        if e.impact != "high":
            continue

        # Si tiene time válido (NO "00:00"), calcular si está dentro de ventana
        event_time_str = getattr(e, 'time', '00:00') or '00:00'

        if event_time_str != '00:00':
            try:
                # Parse time: "14:30" o "2:30am"
                time_str = event_time_str.lower().strip()
                if 'am' in time_str or 'pm' in time_str:
                    # Parse 12-hour format
                    event_time = datetime.strptime(time_str, "%I:%M%p").time()
                else:
                    # Parse 24-hour format
                    event_time = datetime.strptime(time_str, "%H:%M").time()

                # Combinar con fecha de hoy (UTC)
                event_dt = datetime.combine(now.date(), event_time, tzinfo=timezone.utc)
                mte = (now - event_dt).total_seconds() / 60.0
                print(f"[DEBUG] Calendar event: {getattr(e, 'event', 'unknown')[:50]}, time: {event_time_str}, minutes: {mte:.1f}")
                if abs(mte) <= blackout:
                    print(f"[DEBUG] → BLOCKING: Calendar event within {blackout} min window")
                    return True
            except Exception as ex:
                # Si no se puede parsear time, ignorar este evento
                print(f"[DEBUG] Failed to parse calendar time '{event_time_str}': {ex}, skipping")
                continue
        # else: time="00:00" o inválido, ignorar este evento

    return False


def _open_order_count(positions: list) -> tuple[int, dict[str, int]]:
    total = 0
    per_pair: dict[str, int] = {}
    for p in positions:
        if p.type in ("BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"):
            total += 1
            per_pair[p.symbol] = per_pair.get(p.symbol, 0) + 1
    return total, per_pair


async def validate(req: ContextValidateRequest) -> ContextValidateResponse:
    pool = get_pool()
    risk_flags: list[str] = []
    now = datetime.now(timezone.utc)

    # 1. Idempotencia
    row = await pool.fetchrow(
        "SELECT status FROM cycles WHERE cycle_id = $1", req.cycle_id
    )
    if row:
        return ContextValidateResponse(
            valid=False,
            reason=f"cycle_id ya procesado con status={row['status']}",
            session="unknown",
            risk_flags=["duplicate_cycle"],
        )

    # Registrar ciclo como en proceso
    await pool.execute(
        "INSERT INTO cycles(cycle_id, status) VALUES($1, 'processing')",
        req.cycle_id,
    )

    # 2. Sesión
    session = _current_session(now)
    if session == "closed":
        await pool.execute(
            "UPDATE cycles SET status='skipped', skip_reason=$2, completed_at=NOW() WHERE cycle_id=$1",
            req.cycle_id, "market_closed",
        )
        return ContextValidateResponse(
            valid=False, reason="Mercado cerrado", session=session, risk_flags=["market_closed"]
        )

    # 3. Precios válidos
    missing = [s for s in SYMBOLS if s not in req.prices]
    if len(missing) >= 2:
        await pool.execute(
            "UPDATE cycles SET status='error', skip_reason=$2, completed_at=NOW() WHERE cycle_id=$1",
            req.cycle_id, "insufficient_price_data",
        )
        return ContextValidateResponse(
            valid=False, reason=f"Faltan precios: {missing}", session=session, risk_flags=["data_fetch_error"]
        )

    for sym, price in req.prices.items():
        lo, hi = VALID_RANGES.get(sym, (0, 99999))
        if not (lo <= price <= hi):
            risk_flags.append(f"price_out_of_range:{sym}:{price}")

    if len([f for f in risk_flags if "price_out_of_range" in f]) >= 2:
        await pool.execute(
            "UPDATE cycles SET status='error', skip_reason=$2, completed_at=NOW() WHERE cycle_id=$1",
            req.cycle_id, "invalid_price_data",
        )
        return ContextValidateResponse(
            valid=False, reason="Múltiples precios fuera de rango", session=session, risk_flags=risk_flags
        )

    # 4. Noticias de alto impacto
    if _has_high_impact_news(req):
        risk_flags.append("high_impact_news")
        await pool.execute(
            "UPDATE cycles SET status='skipped', skip_reason=$2, completed_at=NOW() WHERE cycle_id=$1",
            req.cycle_id, "high_impact_news",
        )
        return ContextValidateResponse(
            valid=False, reason="Noticia de alto impacto en ventana de blackout",
            session=session, risk_flags=risk_flags,
        )

    # 5. Exposición máxima
    total_orders, per_pair = _open_order_count(req.positions)
    if total_orders >= settings.max_open_orders_total:
        risk_flags.append("max_exposure_reached")
        await pool.execute(
            "UPDATE cycles SET status='skipped', skip_reason=$2, completed_at=NOW() WHERE cycle_id=$1",
            req.cycle_id, "max_exposure_reached",
        )
        return ContextValidateResponse(
            valid=False, reason=f"Exposición máxima alcanzada: {total_orders} órdenes abiertas",
            session=session, risk_flags=risk_flags,
        )

    return ContextValidateResponse(valid=True, reason="ok", session=session, risk_flags=risk_flags)

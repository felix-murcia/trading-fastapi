"""
Cálculo de SL/TP con riesgo monetario fijo.

SL distance = precio × sl_pct  (porcentaje del precio, escalado al instrumento)
Volume      = sl_risk_usd / (sl_pips × pip_value_per_lot)
TP          = SL × rr_min (2.0)

Este enfoque da distancias de SL/TP proporcionadas a la volatilidad de cada
instrumento sin necesidad de ATR ni datos de mercado adicionales.
"""

import logging
import math

from config import settings

logger = logging.getLogger(__name__)

PIP_SIZE = {
    # Forex majors (quote=USD → pip_value fijo $10)
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "AUDUSD": 0.0001,
    "NZDUSD": 0.0001,
    # Forex (base=USD → pip_value = 10/price)
    "USDJPY": 0.01,
    "USDCHF": 0.0001,
    "USDCAD": 0.0001,
    # Materias primas
    "XAUUSD": 0.10,
    "XAUEUR": 0.10,
}

# Pares donde base=USD → pip_value = 10 / price
_USD_BASE = {"USDJPY", "USDCHF", "USDCAD"}


def is_supported(symbol: str) -> bool:
    return symbol in PIP_SIZE


def pip_value_per_lot(symbol: str, price: float) -> float:
    if symbol == "USDJPY":
        return 1000.0 / price   # pip=0.01 → 0.01×100000/price = 1000/price
    if symbol in _USD_BASE:
        return 10.0 / price     # pip=0.0001 → 0.0001×100000/price = 10/price
    if symbol == "XAUEUR":
        return 10.90            # Aprox 10💶 to 💵. Quote=EUR → 10 EUR * (EURUSD aprox 1.09)
    return 10.0                 # quote=USD (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD)




def derive_order_from_bb(
    direction: str, symbol: str, entry: float, sl_anchor: float, tp_anchor: float, spread: float,
) -> tuple[float, float, float, float]:
    """Calcula el tamaño basándose exactamente en los anclajes de las Bandas (SL/TP) y riesgo monetario."""
    pip = PIP_SIZE[symbol]
    ppv = pip_value_per_lot(symbol, entry)

    # 1. SL Distance
    base_dist = abs(entry - sl_anchor)
    sl_min_pips = {
        "USDJPY": settings.sl_min_pips_usdjpy,
        "XAUUSD": settings.sl_min_pips_xauusd,
        "XAUEUR": settings.sl_min_pips_xaueur,
    }.get(symbol, settings.sl_min_pips_default)
    
    sl_floor = max(sl_min_pips * pip, settings.sl_min_spread_mult * spread)
    if base_dist < sl_floor:
        base_dist = sl_floor

    sl_dist = round(base_dist * settings.sl_mult, 5)
    sl_pips = sl_dist / pip
    
    risk_usd_map = {
        "XAUUSD": settings.sl_risk_usd_xauusd,
        "XAUEUR": settings.sl_risk_usd_xaueur,
    }
    risk_usd = risk_usd_map.get(symbol, settings.sl_risk_usd)
    
    max_sl_pips_affordable = risk_usd / (settings.min_volume * ppv)
    if sl_pips > max_sl_pips_affordable:
        sl_pips = max_sl_pips_affordable
        sl_dist = round(sl_pips * pip, 5)

    # 2. TP Distance directly from Bands!
    tp_dist = round(abs(entry - tp_anchor), 5)
    
    # 3. Volume
    volume  = risk_usd / (sl_pips * ppv)
    volume  = max(settings.min_volume, min(settings.max_volume, round(volume, 2)))

    if direction == "buy":
        sl = round(entry - sl_dist, 5)
        tp = round(entry + tp_dist, 5)
    else:
        sl = round(entry + sl_dist, 5)
        tp = round(entry - tp_dist, 5)

    actual_risk   = round(volume * sl_pips * ppv, 2)
    
    logger.info(
        "[SIZING-BB] %s %s entry=%.5f sl_anchor=%.5f tp_anchor=%.5f sl_dist=%.5f sl_pips=%.1f tp_dist=%.5f vol=%.2f risk=$%.2f",
        symbol, direction, entry, sl_anchor, tp_anchor, sl_dist, sl_pips, tp_dist, volume, actual_risk,
    )
    return entry, sl, tp, volume


def derive_order_from_candle_open(
    direction: str, symbol: str, entry: float, candle_open: float, spread: float,
) -> tuple[float, float, float, float]:
    """SL = sl_mult × distancia flecha. Sin TP (salida por señal contraria/EMA)."""
    pip = PIP_SIZE[symbol]
    ppv = pip_value_per_lot(symbol, entry)

    base_dist = abs(entry - candle_open)
    sl_min_pips = {
        "USDJPY": settings.sl_min_pips_usdjpy,
        "XAUUSD": settings.sl_min_pips_xauusd,
        "XAUEUR": settings.sl_min_pips_xaueur,
    }.get(symbol, settings.sl_min_pips_default)
    sl_floor = max(sl_min_pips * pip, settings.sl_min_spread_mult * spread)
    if base_dist < sl_floor:
        base_dist = sl_floor

    sl_dist = round(base_dist * settings.sl_mult, 5)
    sl_pips = sl_dist / pip
    
    risk_usd_map = {
        "XAUUSD": settings.sl_risk_usd_xauusd,
        "XAUEUR": settings.sl_risk_usd_xaueur,
    }
    risk_usd = risk_usd_map.get(symbol, settings.sl_risk_usd)
    
    # HARD-CAP: Protect monetary risk by aggressively clipping SL if it exceeds risk at min_volume
    max_sl_pips_affordable = risk_usd / (settings.min_volume * ppv)
    if sl_pips > max_sl_pips_affordable:
        sl_pips = max_sl_pips_affordable
        sl_dist = round(sl_pips * pip, 5)

    # Resolve RR dynamic values
    rr = getattr(settings, "rr_min_xau", settings.rr_min) if symbol in ["XAUUSD", "XAUEUR"] else settings.rr_min
    tp_dist = round(sl_dist * rr, 5) if rr > 0 else 0.0
    
    volume  = risk_usd / (sl_pips * ppv)
    volume  = max(settings.min_volume, min(settings.max_volume, round(volume, 2)))

    if direction == "buy":
        sl = round(entry - sl_dist, 5)
        tp = round(entry + tp_dist, 5) if tp_dist else 0.0
    else:
        sl = round(entry + sl_dist, 5)
        tp = round(entry - tp_dist, 5) if tp_dist else 0.0

    actual_risk   = round(volume * sl_pips * ppv, 2)
    actual_reward = round(actual_risk * rr, 2) if rr > 0 else 0.0

    logger.info(
        "[SIZING] %s %s entry=%.5f signal_price=%.5f sl_dist=%.5f sl_pips=%.1f tp_dist=%.5f vol=%.2f risk=$%.2f reward=$%.2f",
        symbol, direction, entry, candle_open, sl_dist, sl_pips, tp_dist, volume, actual_risk, actual_reward,
    )
    return entry, sl, tp, volume


def derive_order(direction: str, symbol: str, price: float) -> tuple[float, float, float, float]:
    """Devuelve (entry, sl, tp, volume). SL proporcional al precio, riesgo ~sl_risk_usd."""
    pip  = PIP_SIZE[symbol]
    ppv  = pip_value_per_lot(symbol, price)

    sl_dist = price * settings.sl_pct
    sl_pips = sl_dist / pip
    volume  = settings.sl_risk_usd / (sl_pips * ppv)
    volume  = max(settings.min_volume, round(volume, 2))

    sl_dist = round(sl_pips * pip, 5)
    tp_dist = round(sl_dist * settings.rr_min, 5)

    entry = price
    if direction == "buy":
        sl = round(entry - sl_dist, 5)
        tp = round(entry + tp_dist, 5)
    else:
        sl = round(entry + sl_dist, 5)
        tp = round(entry - tp_dist, 5)

    actual_risk   = round(volume * sl_pips * ppv, 2)
    actual_reward = round(actual_risk * settings.rr_min, 2)

    logger.info(
        "[SIZING] %s %s entry=%.5f sl_pips=%.1f sl_dist=%.5f vol=%.2f risk=$%.2f reward=$%.2f",
        symbol, direction, entry, sl_pips, sl_dist, volume, actual_risk, actual_reward,
    )

    return entry, sl, tp, volume

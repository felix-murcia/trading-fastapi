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
    # Materias primas
    "XAUUSD": 0.10,
}

# Pares donde base=USD → pip_value = 10 / price
_USD_BASE = {"USDJPY", "USDCHF"}


def is_supported(symbol: str) -> bool:
    return symbol in PIP_SIZE


def pip_value_per_lot(symbol: str, price: float) -> float:
    if symbol == "USDJPY":
        return 1000.0 / price   # pip=0.01 → 0.01×100000/price = 1000/price
    if symbol in _USD_BASE:
        return 10.0 / price     # pip=0.0001 → 0.0001×100000/price = 10/price
    return 10.0                 # quote=USD (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD)


SL_MIN_SPREAD_MULT = 3  # el SL nunca puede quedar a menos de N x spread vigente

# Mínimo de SL en pips por símbolo — coherente con SL_LIMITS de order_manager
SL_MIN_PIPS: dict[str, float] = {
    "USDJPY": 5.0,
    "XAUUSD": 10.0,
}
_SL_MIN_PIPS_DEFAULT = 3.0


SL_EMERGENCY_MULT = 3  # SL de emergencia = N × distancia de la flecha


def derive_order_from_candle_open(
    direction: str, symbol: str, entry: float, candle_open: float, spread: float,
) -> tuple[float, float, float, float]:
    """SL de emergencia = 3× distancia flecha. Sin TP (salida por señal contraria/EMA)."""
    pip = PIP_SIZE[symbol]
    ppv = pip_value_per_lot(symbol, entry)

    base_dist = abs(entry - candle_open)
    min_pips = SL_MIN_PIPS.get(symbol, _SL_MIN_PIPS_DEFAULT)
    sl_floor = max(min_pips * pip, SL_MIN_SPREAD_MULT * spread)
    if base_dist < sl_floor:
        base_dist = sl_floor

    sl_dist = round(base_dist * SL_EMERGENCY_MULT, 5)
    sl_pips = sl_dist / pip
    volume  = settings.sl_risk_usd / (sl_pips * ppv)
    volume  = max(settings.min_volume, min(settings.max_volume, round(volume, 2)))

    if direction == "buy":
        sl = round(entry - sl_dist, 5)
        tp = 0.0
    else:
        sl = round(entry + sl_dist, 5)
        tp = 0.0

    actual_risk = round(volume * sl_pips * ppv, 2)

    logger.info(
        "[SIZING] %s %s entry=%.5f signal_price=%.5f base_dist=%.5f sl_dist=%.5f sl_pips=%.1f vol=%.2f risk=$%.2f (no TP)",
        symbol, direction, entry, candle_open, base_dist, sl_dist, sl_pips, volume, actual_risk,
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

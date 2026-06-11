"""
Cálculo de SL/TP con riesgo monetario fijo para todos los pares.

SL  = sl_risk_usd   (15) → distancia = riesgo / (volumen × pip_value)
TP  = SL × rr_min   (2.0) → recompensa = 30
vol = settings.fixed_volume (fijo, igual para todos los pares)

Sin ATR, sin candles, sin datos de mercado: el precio de entrada
viene en la propia señal del indicador.
"""

import logging

from config import settings

logger = logging.getLogger(__name__)

PIP_SIZE = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "USDCHF": 0.0001,
    "XAUUSD": 0.10,
}


def is_supported(symbol: str) -> bool:
    return symbol in PIP_SIZE


def pip_value_per_lot(symbol: str, price: float) -> float:
    """Valor en USD de 1 pip con 1 lote estándar, usando el precio de la señal."""
    if symbol == "USDJPY":
        return 1000.0 / price   # 1 pip = 1000 JPY / tipo de cambio
    if symbol == "USDCHF":
        return 10.0 / price     # 1 pip = 10 CHF / tipo de cambio
    return 10.0                 # EURUSD, GBPUSD, XAUUSD: fijo $10/lote


def derive_order(direction: str, symbol: str, price: float) -> tuple[float, float, float, float]:
    """Devuelve (entry, sl, tp, volume). SL = 15€ y TP = 30€ exactos al volumen fijo."""
    pip    = PIP_SIZE[symbol]
    volume = settings.fixed_volume
    ppv    = pip_value_per_lot(symbol, price)

    sl_pips = settings.sl_risk_usd / (volume * ppv)
    sl_dist = round(sl_pips * pip, 5)
    tp_dist = round(sl_dist * settings.rr_min, 5)

    if direction == "buy":
        entry = price
        sl    = round(entry - sl_dist, 5)
        tp    = round(entry + tp_dist, 5)
    else:
        entry = price
        sl    = round(entry + sl_dist, 5)
        tp    = round(entry - tp_dist, 5)

    logger.info(
        "[SIZING] %s %s entry=%.5f sl_pips=%.1f vol=%.2f ppv=%.4f risk_usd=%.2f reward_usd=%.2f",
        symbol, direction, entry, sl_pips, volume,
        ppv, volume * sl_pips * ppv, volume * sl_pips * ppv * settings.rr_min,
    )

    return entry, sl, tp, volume

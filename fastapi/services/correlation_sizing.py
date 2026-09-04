"""
Correlation-Aware Position Sizing.

Cuando hay múltiples posiciones abiertas en símbolos correlacionados,
se reduce el tamaño para no sobreexponer el portfolio.
"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Matriz de correlación simplificada (valores típicos H1 sobre 100 velas)
# Keys: (symbol_a, symbol_b) → correlación esperada (-1 a 1)
CORRELATION_MATRIX: Dict[tuple, float] = {
    # Majors positively correlated
    ("EURUSD", "GBPUSD"): 0.88,
    ("EURUSD", "AUDUSD"): 0.82,
    ("EURUSD", "NZDUSD"): 0.74,
    ("GBPUSD", "AUDUSD"): 0.85,
    ("GBPUSD", "NZDUSD"): 0.78,
    ("AUDUSD", "NZDUSD"): 0.93,
    # Safe haven correlations
    ("USDJPY", "XAUUSD"): -0.45,  # Gold vs JPY (risk-off)
    ("EURUSD", "USDJPY"): -0.78,  # Typical negative
    ("GBPUSD", "USDJPY"): -0.72,
    ("XAUUSD", "GBPUSD"): 0.55,  # Gold trending with GBP
    ("XAUUSD", "AUDUSD"): 0.65,  # Gold commodity correlation
    # USD base pairs
    ("USDJPY", "USDCHF"): 0.72,
    ("USDCAD", "USDJPY"): -0.55,
    # Gold
    ("XAUUSD", "EURUSD"): 0.48,
}

CORRELATION_THRESHOLD = 0.70  # Si |corr| > 0.7, aplicar reducción


def get_correlation(symbol_a: str, symbol_b: str) -> float:
    """Retorna correlación conocida entre dos símbolos (-1 a 1). Default 0."""
    return CORRELATION_MATRIX.get((symbol_a, symbol_b),
           CORRELATION_MATRIX.get((symbol_b, symbol_a), 0.0))


def get_correlated_lot_multiplier(
    new_symbol: str,
    open_symbols: list[str],
    open_lots: list[float],
) -> float:
    """
    Calcula factor multiplicador para el lot size basándose en correlaciones.

    Si el nuevo símbolo tiene correlación alta (|corr| > 0.7) con alguno
    de los símbolos ya abiertos, se reduce proporcionalmente.

    Ejemplo:
      - EURUSD ya abierto con 0.5 lots
      - GBPUSD correlation con EURUSD = 0.88
      - multiplier = 1.0 - (0.88 * 0.5) = 0.56 → reducir ~44%
    """
    if not open_symbols:
        return 1.0

    total_reduction = 0.0
    for open_sym, open_lot in zip(open_symbols, open_lots):
        corr = abs(get_correlation(new_symbol, open_sym))
        if corr > CORRELATION_THRESHOLD:
            # Reducción proporcional: correlation * lot abierto
            reduction = corr * open_lot
            total_reduction += reduction
            logger.info("[CORR-SIZING] %s vs %s: corr=%.2f, reduction=%.3f",
                        new_symbol, open_sym, corr, reduction)

    # Multiplier: entre 0.3 (mínimo) y 1.0
    multiplier = max(0.3, 1.0 - total_reduction)
    if multiplier < 1.0:
        logger.warning("[CORR-SIZING] Lote reducido para %s: multiplier=%.2f (total_corr_exposure=%.2f)",
                       new_symbol, multiplier, total_reduction)
    return multiplier

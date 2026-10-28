"""Migrado de legacy/services/position_sizing.py:83 -- expected_close_eur(...)

Convencion de cuenta EUR identica al deal del broker. Paridad exacta:
  - EURUSD: pips*vol*pip_value_per_lot/price (P/L en USD -> EUR por /price)
  - USDCHF:  pips*vol*10/(price*eurusd)  (CHF->EUR = USDCHF*EURUSD)
  - GBPUSD/AUDUSD/NZDUSD/XAUUSD: pips*vol*pip_value_per_lot/eurusd
Sin eurusd_price no hay conversion honesta -> ValueError (nunca inventada).
"""

_USD_BASE = {"USDJPY", "USDCAD", "USDCNH", "USDSEK"}
_USD_BASE_CHF_QUOTE = {"USDCHF"}
_USD_QUOTE_NON_EUR_BASE = {"GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"}
_PIP_10_QUOTE = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD", "USDCHF"}


def pip_value_per_lot(symbol: str, price: float) -> float:
    """Migrado de legacy/services/position_sizing.py:70."""
    if symbol == "USDJPY":
        return 1000.0 / price
    if symbol in _USD_BASE:
        return 10.0 / price
    if symbol in _PIP_10_QUOTE:
        return 10.0
    raise ValueError(f"pip_value_per_lot sin regla de pip para {symbol}")


def expected_close_eur(
    symbol: str,
    pips: float,
    volume: float,
    price: float,
    eurusd_price: float | None = None,
) -> float:
    if pips <= 0.0 or volume <= 0.0 or price <= 0.0:
        raise ValueError(
            f"expected_close_eur exige pips/volume/price > 0: {pips}/{volume}/{price}"
        )
    if symbol == "EURUSD":
        return float(pips) * float(volume) * pip_value_per_lot(symbol, price) / float(price)
    if symbol in _USD_BASE_CHF_QUOTE:
        if not eurusd_price or float(eurusd_price) <= 0.0:
            raise ValueError(
                f"expected_close_eur para {symbol} exige el precio EURUSD "
                "(EURCHF = USDCHF x EURUSD); sin el no hay conversion honesta"
            )
        return float(pips) * float(volume) * 10.0 / (float(price) * float(eurusd_price))
    if symbol in _USD_QUOTE_NON_EUR_BASE:
        if not eurusd_price or float(eurusd_price) <= 0.0:
            raise ValueError(
                f"expected_close_eur para {symbol} exige el precio EURUSD "
                "(convencion broker USD->EUR); sin el no hay conversion honesta"
            )
        return float(pips) * float(volume) * pip_value_per_lot(symbol, price) / float(eurusd_price)
    raise ValueError(f"expected_close_eur sin conversion EUR definida para {symbol}")

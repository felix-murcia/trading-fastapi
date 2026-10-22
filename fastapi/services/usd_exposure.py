"""Guard de exposición USD (2026-10-21).

Problema medido: EURUSD BUY (corto USD) y USDCHF BUY (largo USD) quedaron
abiertas a la vez. `MAX_TOTAL_SAME_DIR_POSITIONS` cuenta BUY/SELL, no el
signo del dólar, y las dejó pasar. Este módulo clasifica el signo USD de
cada orden/posición y detecta la contradicción.

Convenio:
  quote=USD, base!=USD (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD):
      BUY  -> corto USD (-1)
      SELL -> largo USD (+1)
  base=USD (USDCHF, USDJPY, USDCAD, USDCNH, USDSEK):
      BUY  -> largo USD (+1)
      SELL -> corto USD (-1)
  sin pata USD (cruces tipo EURGBP): None -> no bloquea.

No cierra posiciones: sólo rechaza la orden nueva. Las posiciones ajenas
cuentan igual: el riesgo es de la cuenta, no del magic.

El guard es una capa de RIESGO, no una señal: no cambia la dirección, el
tamaño, el bundle ni el time-stop. Si no se puede clasificar una posición
(entrada no dict, tipo raro), se ignora; la orden sigue su curso normal.
Los símbolos sin pata USD no se bloquean.
"""
from __future__ import annotations

# Base distinta de USD, quote=USD: un BUY es corto USD.
USD_QUOTE = frozenset({"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"})

# Base=USD, quote distinta: un BUY es largo USD.
USD_BASE = frozenset({"USDCHF", "USDJPY", "USDCAD", "USDCNH", "USDSEK"})

_LONG = frozenset({"BUY", "LONG"})
_SHORT = frozenset({"SELL", "SHORT"})


def usd_sign(symbol: str, order_type: str) -> int | None:
    """Signo de la exposición USD: +1 largo USD, -1 corto USD, None sin pata.

    `order_type` acepta BUY/SELL (EA, MT5) y LONG/SHORT (trade_outcomes).
    Un tipo desconocido devuelve None: no se inventa una dirección.
    """
    sym = str(symbol or "").strip().upper()
    typ = str(order_type or "").strip().upper()
    if typ in _LONG:
        direction = "BUY"
    elif typ in _SHORT:
        direction = "SELL"
    else:
        return None

    if sym in USD_QUOTE:
        return -1 if direction == "BUY" else +1
    if sym in USD_BASE:
        return +1 if direction == "BUY" else -1
    return None


def usd_conflict(
    symbol: str, order_type: str, open_positions: list[dict] | None
) -> dict | None:
    """Devuelve el conflicto USD o None.

    La orden nueva se bloquea si su signo USD es contrario al de CUALQUIER
    posición abierta de otro símbolo. El mismo símbolo se salta: ahí manda
    `position_veto` / el tope de una posición por par, y el motivo debe ser
    el suyo, no un falso `USD_EXPOSURE_CONFLICT`.
    """
    order_sign = usd_sign(symbol, order_type)
    if order_sign is None:
        return None

    order_symbol = str(symbol or "").strip().upper()
    for position in open_positions or []:
        if not isinstance(position, dict):
            continue
        pos_symbol = str(position.get("symbol", "")).strip().upper()
        if pos_symbol == order_symbol:
            continue
        pos_type = str(position.get("type", "")).strip().upper()
        pos_sign = usd_sign(pos_symbol, pos_type)
        if pos_sign is None:
            continue
        if pos_sign == -order_sign:
            return {
                "conflict_symbol": pos_symbol,
                "conflict_type": pos_type,
                "conflict_ticket": position.get("ticket"),
                "conflict_usd_sign": pos_sign,
                "order_usd_sign": order_sign,
            }
    return None

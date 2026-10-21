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
    # P2-2: estos dos vivían solo en order_validator.PIP_SIZE; al unificar la
    # fuente, order_validator importa este dict (pip centralizado, una sola copia).
    "USDCNH": 0.0001,
    "USDSEK": 0.0001,
}

# Pares donde base=USD → pip_value = 10 / price (USDJPY: 0,01 JPY × 100 000
# USD = 1000 JPY por lote, expresado en el precio). USDCHF NO entra aquí
# (G3, 2026-10-20): su pip es 0,0001 CHF × 100 000 USD = **10 USD de
# nocional fijo** (no 10/precio) ⇒ ver `expected_close_eur` y el test de
# cordura de la convención (test_g3_rule_decider.py).
_USD_BASE = {"USDJPY", "USDCAD", "USDCNH", "USDSEK"}

# G3 (2026-10-20): pares base=USD con quote=CHF ⇒ el P/L del bróker llega
# en la moneda de la cuenta (EUR) y la conversión USD→EUR se hace con el
# precio del PROPIO par (1 USD = 1/precio EUR en USDCHF).
_USD_BASE_CHF_QUOTE = {"USDCHF"}

# B1 (G2): pares con QUOTE=USD y BASE≠EUR ⇒ su P/L está en USD y la
# conversión a EUR (cuenta en EUR) se hace por el precio EURUSD — el MISMO
# convenio que el bróker y que el risk_guardian (G2-5/D2).
_USD_QUOTE_NON_EUR_BASE = {"GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"}


async def eurusd_price() -> float:
    """Precio EURUSD vigente (para la conversión honesta USD→EUR del gate de
    recompensa). Misma fuente que usa el risk_guardian (G2-5/D2): la última
    vela H1 del feed. Sin precio ⇒ el llamador falla ruidosamente.
    """
    from services.mt5_client import get_candles
    rows = await get_candles("EURUSD", "H1", 1)
    if not rows:
        raise ValueError("eurusd_price: feed sin velas")
    return float(rows[-1]["close"])


def is_supported(symbol: str) -> bool:
    return symbol in PIP_SIZE


def pip_value_per_lot(symbol: str, price: float) -> float:
    if symbol == "USDJPY":
        return 1000.0 / price   # pip=0.01 → 0.01×100000/price = 1000/price
    if symbol in _USD_BASE:
        return 10.0 / price     # pip=0.0001 → 0.0001×100000/price = 10/price
    if symbol == "XAUEUR":
        return 10.90            # Aprox 10💶 to 💵. Quote=EUR → 10 EUR * (EURUSD aprox 1.09)
    # quote=USD (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD) y base=USD con
    # nocional fijo de 10 USD por pip (USDCHF, G3).
    return 10.0


def expected_close_eur(
    symbol: str,
    pips: float,
    volume: float,
    price: float,
    eurusd_price: float | None = None,
) -> float:
    """Beneficio en EUR que produciría un cierre a `pips` de distancia con
    `volume` lotes — el dato que el gate de recompensa mínima de `/predict`
    compara contra MIN_WIN_EUR.

    Convención de la cuenta (EUR) idéntica al deal del bróker: en EURUSD
    el pip value está en USD y USD→EUR = /price (1 EUR = price USD),
    exacto — verificado contra el trade #255 (21.6 p × 0.01 lot a
    1.12094 → 1.927 € calculado vs 1.93 € pagados por el bróker).

    B1 (G2, orden Félix 2026-10-20 — «continúa con GBPUSD hasta el final»):
    antes esto crasheaba el ciclo de GBPUSD con `ValueError` (el gate de
    recompensa es EURUSD-only ⇒ **GBPUSD no podía operar**). Ahora los pares
    con QUOTE=USD y BASE≠EUR (GBPUSD, AUDUSD, NZDUSD, XAUUSD) se convierten
    con el MISMO convenio que el bróker y que el risk_guardian (G2-5/D2):
    el P/L en USD se divide por el precio EURUSD (1 EUR = price USD).
    Sin el precio EURUSD NO hay conversión honesta definible ⇒ ValueError
    (fracaso ruidoso, nunca silencioso) — no se inventan tasas.
    """
    if pips <= 0.0 or volume <= 0.0 or price <= 0.0:
        raise ValueError(
            f"expected_close_eur exige pips/volume/price > 0: {pips}/{volume}/{price}"
        )
    if symbol == "EURUSD":
        # El P/L ya está en USD y el precio es EURUSD ⇒ /price (convención
        # del bróker, exacta).
        return float(pips) * float(volume) * pip_value_per_lot(symbol, price) / float(price)
    if symbol in _USD_BASE_CHF_QUOTE:
        # base=USD, quote=CHF (USDCHF): 1 pip (0,0001 CHF) × 100 000 USD =
        # 10 USD de nocional fijo; el P/L del bróker llega en EUR ⇒
        # 1 USD = 1/precio USDCHF EUR (convención del bróker, misma forma
        # que el caso EURUSD pero con el precio del par).
        return float(pips) * float(volume) * 10.0 / float(price)
    if symbol in _USD_QUOTE_NON_EUR_BASE:
        if not eurusd_price or float(eurusd_price) <= 0.0:
            raise ValueError(
                f"expected_close_eur para {symbol} exige el precio EURUSD "
                "(convención bróker USD→EUR); sin él no hay conversión honesta"
            )
        # P/L en USD (quote=USD) → EUR dividiendo por EURUSD.
        return (
            float(pips) * float(volume) * pip_value_per_lot(symbol, price)
            / float(eurusd_price)
        )
    raise ValueError(
        f"expected_close_eur sin conversión EUR definida para {symbol}"
    )


def min_lot_for_protection(
    sl_pips: float,
    pip_value: float,
    price: float,
    min_protection_eur: float,
    min_lot: float = 0.01,
) -> float:
    """Lote mínimo para que el trailing "Safe-4" del EA pueda proteger.

    El EA (input `SL_LockSafetyNetEur`, v14.0) sólo mueve el SL al bloquear
    el beneficio si el RIESGO EN € de la posición es ≥ ese valor: con un lote
    más pequeño el trailing queda APAGADO (hace `return` antes de tocar el
    stop) y la posición nunca asegura nada. Félix lo pidió explícitamente el
    2026-10-21 ("un lote que permita el trailing del SL a +4 €").

    Devuelve el lote (redondeado AL ALZA al paso) cuyo riesgo en € es
    ≥ `min_protection_eur`, o 0.0 si es imposible (precio/pip inválidos).
    """
    if sl_pips <= 0.0 or pip_value <= 0.0 or price <= 0.0 or min_protection_eur <= 0.0:
        return 0.0
    eur_per_pip_per_lot = pip_value / price
    if eur_per_pip_per_lot <= 0.0:
        return 0.0
    lots_raw = min_protection_eur / (sl_pips * eur_per_pip_per_lot)
    lots = math.ceil(lots_raw / min_lot - 1e-9) * min_lot
    return round(lots, 2)


def size_ppo_position(
    volume_norm: float,
    sl_pips: float,
    equity: float,
    pip_value: float,
    *,
    risk_pct: float,
    max_lot: float,
    min_lot: float = 0.01,
) -> float:
    """P2-2: sizing del PPO — riesgo fraccional por equity, delegado.

    La cabeza `volume` de la política (Box(2,)_[1], rango [0,1]) modula el
    riesgo entre 0 y ``risk_pct`` del equity::

        lots = equity × risk_pct × clip(volume_norm, 0, 1) / (sl_pips × pip_value)

    Los lotes se redondean HACIA ABAJO al paso ``min_lot`` (nunca se supera el
    tope de riesgo) y se devuelven 0.0 —sin entrada— cuando ni el lote mínimo
    cabe en el presupuesto de riesgo.

    Contrato train/serve (auditoría P2-2): lo usan `/api/v1/ai/predict` (con
    ``pip_value_per_lot(symbol, price)`` centralizado) y el entorno de
    entrenamiento (con ``pip_size × lot_size``, la convención de dinero del
    env). Antes: tope degenerado ``min(0.5, 3%·equity/dist)`` siempre activo y
    una heurística "mín. 3€" con 0.1 €/pip/lot (error ×100 enmascarado por el
    clip) — la cabeza `volume` estaba muerta.
    """
    if equity <= 0.0 or sl_pips <= 0.0 or pip_value <= 0.0 or risk_pct <= 0.0:
        return 0.0
    scale = min(1.0, max(0.0, float(volume_norm)))
    risk_usd = float(equity) * float(risk_pct) * scale
    if risk_usd <= 0.0:
        return 0.0
    lots_raw = risk_usd / (float(sl_pips) * float(pip_value))
    # Epsilon RELATIVO al cociente (no absoluto): el `volume_norm` viene de
    # una cabeza float32 del PPO (p.ej. 0.03999999910593033) ⇒ el riesgo
    # sale con ruido de ~1e-8 RELATIVO. Con un epsilon absoluto de 1e-9 el
    # floor tumbaba un lote que SÍ cabía al borde del mínimo (medido
    # 2026-10-21: 0.00999999977 lote con min_lot 0.01 ⇒ 0 lotes ⇒
    # "sizing inviable" en una entrada válida). El epsilon relativo (1e-6
    # del cociente) es 2 órdenes de magnitud mayor que ese ruido y 4
    # órdenes menor que el paso de lote ⇒ nunca promueve un lote
    # genuinamente inviable.
    lots = math.floor(lots_raw / min_lot + 1e-6) * min_lot
    lots = round(lots, 2)
    if lots < min_lot:
        return 0.0
    return float(min(lots, max_lot))


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

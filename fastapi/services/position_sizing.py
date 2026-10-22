"""Primitivas de dinero: pip por símbolo, pip value, conversión a EUR y sizing.

Este módulo NO decide operaciones (eso es `services/trade_contract.py`): aquí
solo viven las cuentas que el contrato, el guardián de riesgo y el entorno de
entrenamiento comparten. Una sola conversión pips→EUR (`expected_close_eur`).
"""

import math

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

# G3 (2026-10-20), corregido 2026-10-22 con evidencia del bróker: base=USD,
# quote=CHF (USDCHF). 1 pip = 0,0001 CHF × 100 000 USD = **10 CHF** (no 10
# USD). CHF→EUR exige EURCHF = USDCHF × EURUSD (dos conversiones, no una).
# Medido en el deal 58603377390 (trade_outcomes id 285, TP 10,0 p × 0,04 lot,
# +4,33 €) ⇒ 10,825 €/pip/lote; 10/(0,82197 × 1,1236) = 10,828 (+0,03 %).
# Con 10/precio salían 12,166 (+12,4 %) y con 10/EURUSD 8,900 (−17,8 %).
_USD_BASE_CHF_QUOTE = {"USDCHF"}

# B1 (G2): pares con QUOTE=USD y BASE≠EUR ⇒ su P/L está en USD y la
# conversión a EUR (cuenta en EUR) se hace por el precio EURUSD — el MISMO
# convenio que el bróker y que el risk_guardian (G2-5/D2).
_USD_QUOTE_NON_EUR_BASE = {"GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"}

# Únicos símbolos cuyo pip vale 10 unidades de la divisa de cotización
# (nocional fijo 100 000 × 0,0001): quote=USD y USDCHF (10 CHF). Cualquier
# otro símbolo NO tiene regla de pip: falla ruidosamente, no se inventa.
_PIP_10_QUOTE = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD", "USDCHF"}


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
    # quote=USD (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD) y USDCHF (10 CHF):
    # nocional fijo de 10 unidades de la divisa de cotización por pip. La
    # conversión a EUR la hace `expected_close_eur`, no esta función.
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

    USDCHF (2026-10-22): quote=CHF ⇒ el pip son 10 CHF y CHF→EUR exige
    EURCHF = USDCHF × EURUSD (divide por los DOS precios). Medido contra el
    deal 58603377390: 10,828 calculado vs 10,825 real (+0,03 %).
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
        # base=USD, quote=CHF (USDCHF): 1 pip = 0,0001 CHF × 100 000 USD =
        # 10 CHF. CHF→EUR = EURCHF = precio USDCHF × precio EURUSD ⇒ divide
        # por AMBOS. Sin EURUSD no hay conversión honesta definible ⇒
        # ValueError (nunca se inventa la tasa). Verificado contra el deal
        # 58603377390 (ver `_USD_BASE_CHF_QUOTE`).
        if not eurusd_price or float(eurusd_price) <= 0.0:
            raise ValueError(
                f"expected_close_eur para {symbol} exige el precio EURUSD "
                "(EURCHF = USDCHF × EURUSD); sin él no hay conversión honesta"
            )
        return (
            float(pips) * float(volume) * 10.0
            / (float(price) * float(eurusd_price))
        )
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


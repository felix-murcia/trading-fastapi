"""
Decisión de trading y cálculo de riesgo. 100% determinista, sin LLM.

Responsabilidades:
- Voting engine: mayoría ponderada de señales LLM (mínimo 2 de 3)
- Validar confidence >= umbral
- Calcular entry, SL, TP desde datos técnicos
- Calcular volumen: equity * 1% / (sl_pips * pip_value)
- Verificar R/R >= 1.5
- Rechazar si ya hay posición en el par
"""

from models.risk import RiskEvaluateRequest, RiskEvaluateResponse
from models.analysis import TechnicalData
from config import settings


# Pip size y valor por lote estándar (100,000 unidades)
PIP_SIZE = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "USDCHF": 0.0001,
    "XAUUSD": 0.10,
}
PIP_VALUE_PER_LOT = 10.0   # USD aproximado para todos los pares


def _pip_size(symbol: str) -> float:
    return PIP_SIZE.get(symbol, 0.0001)


def _sl_pips(symbol: str, entry: float, sl: float) -> float:
    return abs(entry - sl) / _pip_size(symbol)


def _tp_pips(symbol: str, entry: float, tp: float) -> float:
    return abs(tp - entry) / _pip_size(symbol)


def _calc_volume(equity: float, sl_pips: float) -> float:
    if sl_pips <= 0:
        return settings.min_volume
    raw = (equity * settings.risk_per_trade) / (sl_pips * PIP_VALUE_PER_LOT)
    raw = round(raw / 0.01) * 0.01   # redondear a 0.01
    return max(settings.min_volume, min(settings.max_volume, raw))


def _voting(signals: list) -> tuple[str, float]:
    """
    Devuelve (direction, avg_confidence).
    Requiere mínimo 2 de 3 agentes con señal coherente y confidence >= umbral.
    """
    threshold = settings.llm_confidence_threshold
    valid = [s for s in signals if not s.parse_error and s.confidence >= threshold]

    if len(valid) < 2:
        return "skip", 0.0

    buy_conf  = [s.confidence for s in valid if s.signal == "buy"]
    sell_conf = [s.confidence for s in valid if s.signal == "sell"]

    if len(buy_conf) >= 2 and len(buy_conf) >= len(sell_conf):
        return "buy", sum(buy_conf) / len(buy_conf)
    if len(sell_conf) >= 2 and len(sell_conf) > len(buy_conf):
        return "sell", sum(sell_conf) / len(sell_conf)

    return "skip", 0.0


def _has_position_in_pair(symbol: str, positions: list) -> bool:
    active = {"BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}
    return any(p.symbol == symbol and p.type in active for p in positions)


def _derive_levels(direction: str, tech: TechnicalData) -> tuple[float, float, float]:
    """
    Deriva entry, SL, TP desde datos técnicos del par.
    Entry  = precio actual
    SL     = último soporte (BUY) o resistencia (SELL) con margen de ATR
    TP     = R/R mínimo calculado sobre el SL
    """
    price = tech.current_price
    atr = tech.atr or (price * 0.001)    # fallback: 0.1% del precio

    if direction == "buy":
        entry = price
        # Anclar SL en min(support, entry) para evitar que un soporte histórico
        # por encima del precio actual produzca un SL demasiado ajustado
        sl_anchor = min(tech.support, price)
        sl = round(sl_anchor - atr * 0.5, 5)
        sl_dist = entry - sl
        tp = round(entry + sl_dist * settings.rr_min, 5)
    else:
        entry = price
        # Anclar SL en max(resistance, entry) por la misma razón
        sl_anchor = max(tech.resistance, price)
        sl = round(sl_anchor + atr * 0.5, 5)
        sl_dist = sl - entry
        tp = round(entry - sl_dist * settings.rr_min, 5)

    return entry, sl, tp


def evaluate(req: RiskEvaluateRequest, equity: float) -> RiskEvaluateResponse:
    # 1. Voting engine
    direction, avg_conf = _voting(req.llm_signals)
    if direction == "skip":
        return RiskEvaluateResponse(
            action="skip", entry=0, sl=0, tp=0, volume=0,
            confidence=avg_conf, reason="voting_no_majority",
        )

    # 2. Posición existente en el par
    if _has_position_in_pair(req.best_pair, req.positions):
        return RiskEvaluateResponse(
            action="skip", entry=0, sl=0, tp=0, volume=0,
            confidence=avg_conf, reason="position_already_open",
        )

    # 3. Derivar niveles
    entry, sl, tp = _derive_levels(direction, req.technical)

    # 4. Verificar R/R
    sl_p = _sl_pips(req.best_pair, entry, sl)
    tp_p = _tp_pips(req.best_pair, entry, tp)
    if sl_p <= 0 or round(tp_p / sl_p, 4) < settings.rr_min:
        return RiskEvaluateResponse(
            action="skip", entry=entry, sl=sl, tp=tp, volume=0,
            confidence=avg_conf,
            reason=f"rr_insufficient:{round(tp_p/sl_p, 2) if sl_p > 0 else 0}",
        )

    # 5. Calcular volumen
    volume = _calc_volume(equity, sl_p)

    return RiskEvaluateResponse(
        action=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        volume=volume,
        confidence=round(avg_conf, 4),
        reason="ok",
    )

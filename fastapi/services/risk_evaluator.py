"""
Decisión de trading y cálculo de riesgo. 100% determinista, sin LLM.

Responsabilidades:
- Voting engine: mayoría ponderada de señales LLM (mínimo 2 de 3)
- Validar confidence >= umbral
- Calcular SL desde ATR del M5 (ni muy apretado ni muy ancho)
- Calcular volumen para riesgo fijo en USD (sl_risk_usd)
- TP siempre = SL × rr_min (R:R fijo, reward = sl_risk_usd × rr_min)
- Rechazar si ya hay posición en el par
"""

import logging

from models.risk import RiskEvaluateRequest, RiskEvaluateResponse
from models.analysis import TechnicalData
from db.connection import get_pool
from config import settings

logger = logging.getLogger(__name__)


PIP_SIZE = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "USDCHF": 0.0001,
    "XAUUSD": 0.10,
}

# Rango de SL en pips permitido por símbolo (cota inferior y superior)
# Debe ser consistente con SL_LIMITS en order_manager.py
SL_PIPS_BOUNDS: dict[str, tuple[float, float]] = {
    "USDJPY": (5.0,  80.0),
    "XAUUSD": (50.0, 800.0),
}
_SL_PIPS_DEFAULT = (5.0, 80.0)


def _pip_size(symbol: str) -> float:
    return PIP_SIZE.get(symbol, 0.0001)


def _pip_value_per_lot(symbol: str, price: float) -> float:
    """Valor en USD de 1 pip con 1 lote estándar, usando el precio actual."""
    if symbol == "USDJPY":
        return 1000.0 / price   # 1 pip = 1000 JPY / tipo de cambio
    if symbol == "USDCHF":
        return 10.0 / price     # 1 pip = 10 CHF / tipo de cambio
    return 10.0                 # EURUSD, GBPUSD, XAUUSD: fijo $10/lote


def _voting(signals: list) -> tuple[str, float]:
    """
    Devuelve (direction, avg_confidence).
    - smc_brain con confidence >= umbral: señal directa, no requiere mayoría.
    - Agentes LLM: requiere mínimo 2 de 3 con señal coherente y confidence >= umbral.
    """
    threshold = settings.llm_confidence_threshold

    smc = next((s for s in signals if s.agent == "smc_brain"), None)
    if smc and smc.confidence >= threshold and smc.signal in ("buy", "sell"):
        return smc.signal, smc.confidence

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


def _derive_order(
    direction: str,
    symbol: str,
    tech: TechnicalData,
) -> tuple[float, float, float, float]:
    """
    Deriva entry, SL, TP y volumen con riesgo fijo en USD.

    SL  = ATR_M5 × atr_sl_multiplier  (acotado a SL_PIPS_BOUNDS)
    TP  = SL × rr_min  (siempre R:R = rr_min, TP en USD = sl_risk_usd × rr_min)
    vol = sl_risk_usd / (sl_pips × pip_value_per_lot)
    """
    pip  = _pip_size(symbol)
    ppv  = _pip_value_per_lot(symbol, tech.current_price)
    atr  = tech.atr or (tech.current_price * 0.001)

    # SL en pips basado en ATR del M5
    atr_pips = atr / pip
    sl_pips  = atr_pips * settings.atr_sl_multiplier

    # Acotar a los límites del símbolo
    lo, hi  = SL_PIPS_BOUNDS.get(symbol, _SL_PIPS_DEFAULT)
    sl_pips = max(lo, min(hi, sl_pips))

    sl_dist = round(sl_pips * pip, 5)
    tp_dist = round(sl_dist * settings.rr_min, 5)

    price = tech.current_price
    if direction == "buy":
        entry = price
        sl    = round(entry - sl_dist, 5)
        tp    = round(entry + tp_dist, 5)
    else:
        entry = price
        sl    = round(entry + sl_dist, 5)
        tp    = round(entry - tp_dist, 5)

    # Volumen para riesgo exacto en USD
    raw_vol = settings.sl_risk_usd / (sl_pips * ppv)
    raw_vol = round(raw_vol / 0.01) * 0.01   # paso mínimo de 0.01
    volume  = max(settings.min_volume, min(settings.max_volume, raw_vol))

    logger.info(
        "[RISK] %s atr=%.5f atr_pips=%.1f sl_pips=%.1f sl_dist=%.5f vol=%.2f ppv=%.4f risk_usd=%.2f",
        symbol, atr, atr_pips, sl_pips, sl_dist, volume, ppv, volume * sl_pips * ppv,
    )

    return entry, sl, tp, volume


async def evaluate(req: RiskEvaluateRequest, equity: float) -> RiskEvaluateResponse:
    pool = get_pool()

    # 1. Score mínimo del par (filtro de calidad)
    if req.best_pair_score < settings.min_pair_score:
        return RiskEvaluateResponse(
            action="skip", entry=0, sl=0, tp=0, volume=0,
            confidence=0.0,
            reason=f"pair_score_too_low:{round(req.best_pair_score, 3)}",
        )

    # 2. Señal: debate (si disponible) o voting engine como fallback
    if req.debate and req.debate.signal in ("buy", "sell"):
        direction = req.debate.signal
        avg_conf  = req.debate.confidence
    else:
        direction, avg_conf = _voting(req.llm_signals)
        if direction == "skip":
            return RiskEvaluateResponse(
                action="skip", entry=0, sl=0, tp=0, volume=0,
                confidence=avg_conf, reason="voting_no_majority",
            )

    # 3. Posición existente — doble check: MT5 (tiempo real) + DB (contra race condition)
    if _has_position_in_pair(req.best_pair, req.positions):
        return RiskEvaluateResponse(
            action="skip", entry=0, sl=0, tp=0, volume=0,
            confidence=avg_conf, reason="position_already_open",
        )

    db_open = await pool.fetchval(
        "SELECT id FROM orders WHERE symbol=$1 AND status IN ('pending','placed') LIMIT 1",
        req.best_pair,
    )
    if db_open:
        logger.info("[RISK] Blocked by DB: open order for %s (id=%s)", req.best_pair, db_open)
        return RiskEvaluateResponse(
            action="skip", entry=0, sl=0, tp=0, volume=0,
            confidence=avg_conf, reason="position_already_open_db",
        )

    # 4. Derivar niveles y volumen
    entry, sl, tp, volume = _derive_order(direction, req.best_pair, req.technical)

    return RiskEvaluateResponse(
        action=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        volume=volume,
        confidence=round(avg_conf, 4),
        reason="ok",
    )

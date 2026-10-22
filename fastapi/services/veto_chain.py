"""Cadena de vetos de UNA señal — orden, atribución y traza en un solo sitio.

Application layer puro (sin I/O): el router resuelve las dependencias externas
(Qwen, autoridad del juez) y esta unidad aplica el ORDEN que da la atribución
del audit:

    time-stop → re-entrada → contrato de dinero → posición abierta →
    confianza → Qwen degradado → sesgo del LLM → quórum lateral → sizing

El orden no es cosmético: `_veto_reason` (routers/ai.py) atribuye el primer
veto que dejó la decisión en HOLD. Cambiarlo cambia la traza y los contadores.
En particular, el sizing va DESPUÉS del quórum (así estaba en producción y
así lo fija `_veto_reason`: sizing sólo se activa si ningún otro veto revirtió
la señal).

Extraído de `predict_direction` (H-I, 2026-10-22; 1.229 líneas). Los mensajes
de log se conservan literalmente: son la evidencia que se lee en producción.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from core.rules import MIN_WIN_EUR
from services.quorum import collect_votes, detect_lateral, evaluate
from services.trade_contract import TradeContract

logger = logging.getLogger(__name__)

# Rate-limit del aviso de sizing inviable (S-2: 1 WARNING / 600 s por símbolo).
_SIZING_WARN_INTERVAL_S = 600.0
_SIZING_WARN_TS: dict[str, float] = {}


@dataclass(frozen=True)
class MechanicalInputs:
    cycle_id: str
    symbol: str                 # símbolo CRUDO (clave del rate-limit)
    decision: str               # decisión cruda del PPO/regla
    env_position: int
    position_raw: int           # req.position (0/1/2) — sólo para el log
    steps_h1: int
    time_stop_bars: int
    contract: TradeContract
    lots: float                 # lote final del contrato (0.0 si rechaza)
    tp_pips: float
    min_lock_eur: float
    reentry_window: bool
    bars_since_close: int
    churn_bars: int
    qwen_ok: bool
    qwen_reentry_ok: Optional[bool]
    min_entry_confidence: float
    effective_prob: float


@dataclass(frozen=True)
class MechanicalOutcome:
    decision: str
    time_stop: bool
    reentry_ok: Optional[bool]
    reward_eur: Optional[float]
    reward_below_min: bool
    contract_rejected: bool
    position_rejected: bool
    low_confidence: bool


def _resolve_reentry(inp: MechanicalInputs) -> Optional[bool]:
    """Ventana ≤ CHURN_BARS la fija el sistema; quién decide es el cerebro.

    Con Qwen degradado manda la red de emergencia documentada: sólo se
    bloquea la 1ª vela tras el cierre (bars=0).
    """
    if not inp.reentry_window:
        return None
    if inp.qwen_ok:
        return bool(inp.qwen_reentry_ok if inp.qwen_reentry_ok is not None else True)
    reentry_ok = inp.bars_since_close > 0
    logger.warning(
        "[%s] REENTRY-DEFAULT (Qwen degradado) → reentry_ok=%s (bars=%d)",
        inp.cycle_id, reentry_ok, inp.bars_since_close,
    )
    return reentry_ok


def _time_stop(inp: MechanicalInputs) -> tuple[str, bool]:
    """P1/H4: SALIDA mecánica por delante de todo. El EA ejecuta CLOSE."""
    if inp.env_position != 0 and inp.steps_h1 >= inp.time_stop_bars:
        logger.warning(
            "[%s] TIME-STOP ║ posición abierta %d H1 ≥ %d → CLOSE",
            inp.cycle_id, inp.steps_h1, inp.time_stop_bars,
        )
        return "CLOSE", True
    return inp.decision, False


def _apply_hold_chain(
    inp: MechanicalInputs, decision: str, time_stop: bool,
    reentry_ok: Optional[bool],
) -> MechanicalOutcome:
    """Vetos que pueden revertir a HOLD, en el orden que fija la atribución."""
    # El importe ya lo calculó el CONTRATO (EUR netos de costes): aquí no se
    # vuelve a convertir. Sólo hay snapshot si hay entrada propuesta y lote.
    reward_eur = (
        inp.contract.reward_net_eur
        if (inp.contract.ok and decision in ("BUY", "SELL") and inp.lots > 0.0)
        else None
    )
    reward_below_min = False
    contract_rejected = (not inp.contract.ok) and inp.contract.reason != "sizing_inviable"
    position_rejected = inp.env_position != 0 and decision in ("BUY", "SELL")
    low_confidence = False

    if position_rejected:
        logger.warning(
            "[%s] VETO POSICION ║ flip %s con posición abierta (pos=%d) → HOLD "
            "(coherente con EA: salida solo SL/TP)",
            inp.cycle_id, decision, inp.position_raw,
        )
        decision = "HOLD"
    elif contract_rejected and decision in ("BUY", "SELL"):
        reward_below_min = inp.contract.reason in (
            "tp_no_paga_bloqueo", "tp_no_cubre_costes",
        )
        logger.warning(
            "[%s] VETO CONTRATO ║ %s · TP %.1f p × %.2f lot · riesgo %.2f€ "
            "(tope %.2f€) · TP neto %.2f€ < exigido %.2f€ → HOLD",
            inp.cycle_id, inp.contract.reason, inp.tp_pips, inp.lots,
            inp.contract.risk_eur, inp.contract.cap_eur,
            inp.contract.reward_net_eur,
            max(MIN_WIN_EUR, inp.min_lock_eur),
        )
        decision = "HOLD"
    elif inp.reentry_window and reentry_ok is False and decision in ("BUY", "SELL"):
        logger.warning(
            "[%s] VETO RE-ENTRADA (cerebro) ║ reentry_ok=false, %d velas H1 desde el "
            "último cierre ≤ CHURN_BARS=%d → HOLD",
            inp.cycle_id, inp.bars_since_close, inp.churn_bars,
        )
        decision = "HOLD"
    elif (
        inp.min_entry_confidence > 0.0
        and inp.effective_prob < inp.min_entry_confidence
        and decision in ("BUY", "SELL")
    ):
        low_confidence = True
        logger.warning(
            "[%s] VETO CONFIANZA ║ effective_prob %.3f < umbral %.2f → HOLD",
            inp.cycle_id, inp.effective_prob, inp.min_entry_confidence,
        )
        decision = "HOLD"
    elif not inp.qwen_ok and decision in ("BUY", "SELL"):
        # Fail-closed de degradación: si el cerebro no responde no se abre
        # nada. Es seguridad mecánica, no un juicio de calidad.
        logger.warning(
            "[%s] VETO QWEN-DEGRADADO ║ cerebro no disponible → HOLD",
            inp.cycle_id,
        )
        decision = "HOLD"

    return MechanicalOutcome(
        decision=decision,
        time_stop=time_stop,
        reentry_ok=reentry_ok,
        reward_eur=reward_eur,
        reward_below_min=reward_below_min,
        contract_rejected=contract_rejected,
        position_rejected=position_rejected,
        low_confidence=low_confidence,
    )


def apply_mechanical_vetoes(inp: MechanicalInputs) -> MechanicalOutcome:
    """Aplica los vetos mecánicos en orden y devuelve la decisión + flags.

    No abre ni cierra nada: sólo decide si la señal propuesta puede llegar al
    EA. Cualquier HOLD aquí es una atribución explícita (nunca "unknown_veto").
    """
    decision, time_stop = _time_stop(inp)
    # El log de re-entrada va después del de time-stop, como en producción.
    reentry_ok = _resolve_reentry(inp)
    return _apply_hold_chain(inp, decision, time_stop, reentry_ok)


def bias_veto_pending(decision: str, qwen: dict) -> Optional[str]:
    """Sesgo del LLM que pide revertir la señal, o None.

    Sólo BUY con BEARISH (prob ≤ 0,30) y SELL con BULLISH (prob ≥ 0,70): el
    juez tiene que estar en el extremo contrario, no "neutral tibio".
    """
    if (
        decision == "BUY"
        and qwen["bias"] == "BEARISH"
        and qwen.get("bias_prob", 0.5) <= 0.3
    ):
        return "BEARISH"
    if (
        decision == "SELL"
        and qwen["bias"] == "BULLISH"
        and qwen.get("bias_prob", 0.5) >= 0.7
    ):
        return "BULLISH"
    return None


def apply_bias_veto(
    decision: str, qwen: dict, *, authorized: bool, cycle_id: str,
) -> tuple[str, bool]:
    """Aplica el veto de sesgo si el juez tiene autoridad demostrada.

    `authorized` lo resuelve el router (`_qwen_veto_authorized`, I/O): esta
    función sólo decide y traza. Sin autoridad la señal pasa (el helper de
    autoridad deja su propio log explícito).
    """
    if not authorized:
        return decision, False
    logger.warning(
        "[%s] VETO BIAS ║ %s revertido por LLM %s (conf=%.2f)",
        cycle_id, decision, qwen["bias"], qwen.get("bias_prob", 0.5),
    )
    return "HOLD", True


def apply_sizing_veto(
    *, decision: str, lots: float, cycle_id: str, symbol: str,
    volume_norm: float, sl_pips: float, equity: float,
) -> tuple[str, bool]:
    """S-1/S-2: lote no operable ⇒ HOLD declarado, nunca un ERROR repetido.

    Va DESPUÉS del quórum, como en producción: sólo se activa si ningún otro
    veto revirtió ya la señal (`_veto_reason` le da autoridad entonces).
    """
    if lots > 0.0 or decision not in ("BUY", "SELL"):
        return decision, False
    now_ts = time.time()
    if now_ts - _SIZING_WARN_TS.get(symbol, 0.0) >= _SIZING_WARN_INTERVAL_S:
        _SIZING_WARN_TS[symbol] = now_ts
        logger.warning(
            "[%s] sizing_below_min: vol_norm=%s sl=%s pips equity=%s → HOLD declarado "
            "(veto sizing, 1 aviso/%ss por símbolo)",
            cycle_id, volume_norm, sl_pips, equity, int(_SIZING_WARN_INTERVAL_S),
        )
    else:
        logger.debug("[%s] sizing_below_min (rate-limited) → HOLD", cycle_id)
    return "HOLD", True


def _detector_inputs(
    df: pd.DataFrame, current_price: float, current_atr: float,
    sma200_dist: Optional[float], sma200_slope: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Entradas del detector de régimen (mismas escalas que el env)."""
    adx_h1 = float(df["adx"].iloc[-1]) if "adx" in df.columns else None
    slope_ratio = (
        sma200_slope / current_price
        if (sma200_slope is not None and current_price)
        else None
    )
    dist_ratio = (
        sma200_dist / current_price
        if (sma200_dist is not None and current_price)
        else None
    )
    bb_pos = float(df["bb_pos"].iloc[-1]) if "bb_pos" in df.columns else None
    macd_hist = (
        float(df["macd_hist"].iloc[-1]) if "macd_hist" in df.columns else None
    )
    return adx_h1, slope_ratio, dist_ratio, bb_pos, macd_hist


def evaluate_quorum(
    *,
    decision: str,
    final_decision: str,
    qwen: dict,
    df: pd.DataFrame,
    current_price: float,
    current_atr: float,
    disp24_atr: Optional[float],
    adx_h4: Optional[float],
    sma200_dist: Optional[float],
    sma200_slope: Optional[float],
    cycle_id: str,
) -> tuple[Optional[dict], str, bool]:
    """Quórum PPO/LLM/estructura en régimen lateral (T-31).

    Devuelve (audit, decisión, rechazado). En régimen no lateral NO bloquea:
    sólo declara las entradas del detector para que un "no lateral" sea
    descifrable en el audit.
    """
    adx_h1, slope_ratio, dist_ratio, bb_pos, macd_hist = _detector_inputs(
        df, current_price, current_atr, sma200_dist, sma200_slope,
    )
    lateral = detect_lateral(
        bb_pos=bb_pos,
        atr_pct=(current_atr / current_price) if current_price else None,
        macd_hist=macd_hist,
        adx=adx_h1,
        disp24_atr=disp24_atr,
        adx_h4=adx_h4,
        slope_h1_ratio=slope_ratio,
    )
    if not lateral:
        return (
            {
                "lateral": False,
                "granted": None,
                "reason": "régimen no lateral: quorum no aplica",
                "adx_h1": adx_h1,
                "adx_h4": adx_h4,
                "disp24_atr": disp24_atr,
                "slope_h1_ratio": slope_ratio,
            },
            final_decision,
            False,
        )

    votes = collect_votes(
        ppo_direction=decision,
        llm_bias=qwen.get("bias"),
        llm_bias_prob=qwen.get("bias_prob"),
        llm_abstained=bool(qwen.get("abstained", False)),
        sma200_dist=dist_ratio,
        sma200_slope=slope_ratio,
    )
    verdict = evaluate(votes, direction=decision)
    audit = {
        "lateral": True,
        "granted": verdict["granted"],
        "direction": verdict["direction"],
        "votes": verdict["votes"],
        "tally": verdict["tally"],
        "required": verdict["required"],
        "reason": verdict["reason"],
        "adx_h1": adx_h1,
        "adx_h4": adx_h4,
        "disp24_atr": disp24_atr,
        "slope_h1_ratio": slope_ratio,
    }
    if final_decision in ("BUY", "SELL") and not verdict["granted"]:
        logger.warning(
            "[%s] QUÓRUM ║ mercado lateral sin consenso (%s) → HOLD",
            cycle_id, verdict["reason"],
        )
        return audit, "HOLD", True
    return audit, final_decision, False

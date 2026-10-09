"""
Qwen LLM client — extracted from routers/ai.py.

Provides unified Qwen queries for trade analysis and SL/TP validation.
All functions return safe defaults on ANY failure (connection, HTTP != 200,
malformed response) — Qwen nunca crashea /predict; el estado degradado se
registra en QwenHealthState y lo reporta /pipeline/health (P1-6).
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np

logger = logging.getLogger(__name__)

QWEN_URL = os.getenv("QWEN_URL", "http://100.90.16.33:8080/v1/chat/completions")

SL_MIN_NORM = 0.05
SL_MAX_NORM = 0.30
TP_MIN_NORM = 0.05
TP_MAX_NORM = 0.50


# ─── Estado de salud de Qwen (P1-6) ──────────────────────────────────────────
# Toda llamada (éxito o cualquier fallo) lo actualiza. /pipeline/health lo
# reporta como componente y /predict marca `qwen_degraded` en audit_log.

@dataclass
class QwenHealthState:
    calls_ok: int = 0
    calls_failed: int = 0
    consecutive_failures: int = 0
    last_ok_at: float | None = None
    last_fail_at: float | None = None
    last_error: str | None = None


_QWEN_STATE = QwenHealthState()


def get_qwen_state() -> QwenHealthState:
    return _QWEN_STATE


def _record_success() -> None:
    _QWEN_STATE.calls_ok += 1
    _QWEN_STATE.consecutive_failures = 0
    _QWEN_STATE.last_ok_at = time.time()
    _QWEN_STATE.last_error = None


def _record_failure(error: str) -> None:
    _QWEN_STATE.calls_failed += 1
    _QWEN_STATE.consecutive_failures += 1
    _QWEN_STATE.last_fail_at = time.time()
    _QWEN_STATE.last_error = error[:200]


def _degraded_unified(
    regime: str, sl_proposed: float, tp_proposed: float, ml_prob: float, reason: str,
) -> dict:
    """Safe defaults uniformes ante CUALQUIER fallo de Qwen.

    quality=0.0 es deliberadamente fail-closed: por debajo de QUALITY_THRESHOLD
    el veto de predict revierte BUY/SELL a HOLD mientras Qwen esté degradado.
    """
    return {
        "ok": False,
        "quality": 0.0,
        "reason": reason[:120],
        "bias": "NEUTRAL",
        "confidence_modifier": 1.0,
        "effective_prob": float(ml_prob),
        "sl_adjusted": float(sl_proposed),
        "tp_adjusted": float(tp_proposed),
        "regime": regime,
    }


def _classify_session(hour: int) -> str:
    """Classify FX session from the TRUE UTC hour of the candle.

    Fronteras aproximadas en UTC (P2-3 auditoría): la versión anterior trataba
    la hora del servidor del broker como si fuera UTC y, además, rotulaba
    17-23h como "Asia" (es la tarde de Nueva York) y 23-7h como
    "Weekend/Closed" (entre semana el mercado abre 24h: son Sydney/Tokio).
    La ventana 12-16 UTC es el solape Londres/NY — máxima liquidez — y merece
    etiqueta propia: es cuando el veto decide sobre la señal más fuerte del día.
    """
    if 0 <= hour < 7:
        return "Asia"                # Tokio + Sídney (00-07 UTC)
    if 7 <= hour < 12:
        return "London"              # mañana de Londres (07-12 UTC)
    if 12 <= hour < 16:
        return "London/NY Overlap"   # solape = máxima liquidez (12-16 UTC)
    if 16 <= hour < 21:
        return "New York"            # tarde de NY (16-21 UTC)
    return "Sydney"                  # cierre NY + apertura Sídney (21-24 UTC)


def _classify_regime(bb_pos: float, atr_pct: float, macd_hist: float) -> str:
    """Classify market regime from indicators."""
    if abs(bb_pos) > 0.8:
        return "TRENDING"
    elif atr_pct > 0.015:
        return "VOLATILE"
    elif abs(macd_hist) < 0.0002:
        return "RANGING"
    return "BREAKOUT"


def _parse_json_response(content: str) -> dict:
    """Extract and parse JSON from LLM response content."""
    if "{" not in content:
        raise RuntimeError(f"sin JSON: {content[:80]}")
    json_str = content[content.index("{"):]
    parsed, _ = json.JSONDecoder().raw_decode(json_str)
    return parsed


def _clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp value to [min_val, max_val]."""
    return max(min_val, min(max_val, value))


async def query_qwen_unified(
    symbol: str,
    decision: str,
    ml_prob: float,
    rsi: float,
    atr: float,
    atr_pct: float,
    macd_hist: float,
    bb_pos: float,
    range_pct: float,
    last_ret: float,
    hour: int,
    news: str,
    candle_context: str,
    sl_proposed: float,
    tp_proposed: float,
    cycle_id: str,
    logger: logging.Logger,
    recent_insights: Optional[list[dict]] = None,
    # Context injection (Fase 2: Qwen Context Awareness)
    adx: Optional[float] = None,
    sma200_dist: Optional[float] = None,
    sma200_slope: Optional[float] = None,
    regime: Optional[str] = None,
    support_level: Optional[float] = None,
    resistance_level: Optional[float] = None,
) -> dict:
    """
    Unified Qwen query — single call per candle.

    Returns:
        - quality_score: 0-10
        - quality_reason: str
        - llm_bias: BULLISH/BEARISH/NEUTRAL
        - confidence_modifier: 0.5-1.5
        - sl_adjusted / tp_adjusted: norm values
        - regime: TRENDING/RANGING/VOLATILE/BREAKOUT
    """
    session = _classify_session(hour)
    if regime is None:
        regime = _classify_regime(bb_pos, atr_pct, macd_hist)

    # ADX-based regime detection (Fase 2: Qwen Context Awareness)
    if adx is not None and adx < 0.25:
        regime = "RANGING"
    elif regime == "RANGING" and adx is not None and adx >= 0.25:
        regime = "TRENDING"

    sl_pips = sl_proposed * 100.0
    tp_pips = tp_proposed * 200.0
    news_context = " ".join(str(news).split())[:600] or "Unavailable"

    # Build context injection for Qwen (Fase 2: Context Awareness)
    context_lines = [
        f"Context:",
        f"- Symbol: {symbol} H1 setup",
        f"- RSI(14): {rsi:.1f}",
        f"- ATR: {atr:.5f} ({atr_pct:.2%} of price)",
        f"- MACD histogram: {macd_hist:.6f}",
        f"- Bollinger position: {bb_pos:.2f}",
        f"- Range: {range_pct:.2%} of price",
        f"- Last return: {last_ret:.3%}",
        f"- Session: {session}",
        f"- Regime: {regime}",
        (f"- ADX: {adx:.3f}" if adx is not None else "- ADX: Unavailable"),
    ]

    # Add SMA200 context if available (trend structure)
    if sma200_dist is not None and sma200_slope is not None:
        trend_dir = "ABOVE" if sma200_dist > 0 else "BELOW"
        slope_dir = "RISING" if sma200_slope > 0 else "FALLING" if sma200_slope < 0 else "FLAT"
        context_lines.append(f"- SMA200: Price {trend_dir} ({sma200_dist:.4f}), SMA200 {slope_dir} ({sma200_slope:+.4f})")

    if support_level is not None and resistance_level is not None:
        context_lines.append(f"- Support: {support_level:.5f} | Resistance: {resistance_level:.5f}")

    context_lines.extend([
        f"- Candles: {candle_context}",
        "",
        f"News: {news_context[:400] if news_context != 'Unavailable' else 'Unavailable'}",
        "",
        f"ML Signal: {decision} with ML confidence {ml_prob:.3f}",
        "",
        # P2-H2 (2026-10-09): el ejemplo único con quality=7.5/bias=NEUTRAL
        # anclaba la respuesta en la plantilla (temperature=0.2 + modelo
        # pequeño → quality_score constante 7.5 en 511+ ciclos = veto de
        # calidad decorativo). Dos ejemplos con valores separados + rúbrica
        # explícita: la propiedad ">=2 ejemplos distintos" la fija
        # tests/test_qwen_prompt_anchoring.py.
        "Return EXACTLY JSON, no extra text — ONE JSON object. Two FORMAT",
        "examples with different scores; do NOT copy their values — judge THIS",
        "setup on its own context:",
        '{"quality": 8.5, "reason": "trend and ADX align", "bias": "BULLISH", "confidence_modifier": 1.3,'
        '  "sl_ok": true, "tp_ok": true, "sl_adjusted": 0.2000, "tp_adjusted": 0.4000,'
        '  "regime": "TRENDING"}',
        '{"quality": 3.0, "reason": "range chop against signal", "bias": "BEARISH", "confidence_modifier": 0.7,'
        '  "sl_ok": false, "tp_ok": true, "sl_adjusted": 0.1600, "tp_adjusted": 0.3200,'
        '  "regime": "RANGING"}',
        "",
        "Fields: quality 0-10 — score on evidence: 7-10 only when trend/ADX/session",
        "agree with the ML signal and structure is clean; 1-4 when ranging chop,",
        "against-trend signal or unverified news; 4-7 when mixed. reason max 12 words;",
        "bias BULLISH/BEARISH/NEUTRAL aligned with the setup; confidence_modifier",
        "0.5-1.5 moving effective confidence the same direction; sl_ok/tp_ok booleans;",
        "adjusted SL 0.15-0.30, TP 0.125-0.30 with TP:SL >=1.5; regime",
        "TRENDING/RANGING/VOLATILE/BREAKOUT.",
    ])

    prompt = "\n".join(context_lines)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(QWEN_URL, json={
                "messages": [
                    {"role": "system", "content": "You are a quantitative trading analyst. Always respond in valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 160,
                "response_format": {"type": "json_object"},
            })
    except (httpx.ConnectTimeout, httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("[%s] QWEN-UNIFIED connection failed: %s", cycle_id, repr(exc))
        _record_failure(f"connection: {repr(exc)[:120]}")
        return _degraded_unified(
            regime, sl_proposed, tp_proposed, ml_prob, "Qwen unavailable"
        )

    if res.status_code == 200:
        try:
            content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            parsed = _parse_json_response(content)
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Respuesta basura del LLM: degradar, nunca propagar (P1-6)
            logger.warning("[%s] QWEN-UNIFIED malformed response: %s", cycle_id, exc)
            _record_failure(f"parse: {exc}")
            return _degraded_unified(
                regime, sl_proposed, tp_proposed, ml_prob, f"Qwen degraded: {exc}"
            )

        q = float(parsed.get("quality", 5.0))
        reason = str(parsed.get("reason", ""))[:120]
        bias_raw = str(parsed.get("bias", "NEUTRAL")).upper()
        bias = "NEUTRAL"
        if "BULL" in bias_raw:
            bias = "BULLISH"
        elif "BEAR" in bias_raw:
            bias = "BEARISH"

        conf_mod = _clamp(float(parsed.get("confidence_modifier", 1.0)), 0.5, 1.5)

        sl_adj = _clamp(float(parsed.get("sl_adjusted", sl_proposed)), SL_MIN_NORM, SL_MAX_NORM)
        tp_adj = _clamp(float(parsed.get("tp_adjusted", tp_proposed)), TP_MIN_NORM, TP_MAX_NORM)
        tp_adj = _clamp(sl_adj * 2.0, TP_MIN_NORM, TP_MAX_NORM)

        effective_prob = float(np.exp(np.log(max(ml_prob, 1e-6)) + np.log(conf_mod)))
        effective_prob = _clamp(effective_prob, 0.0, 1.0)

        logger.info(
            "[%s] QWEN-UNIFIED ║ q=%.1f bias=%s conf_mod=%.2f eff_prob=%.4f | "
            "sl=%.3f→%.3f tp=%.3f→%.3f regime=%s | %s",
            cycle_id, q, bias, conf_mod, effective_prob,
            sl_proposed, sl_adj, tp_proposed, tp_adj,
            parsed.get("regime", regime), reason[:60]
        )
        _record_success()
        return {
            "ok": True,
            "quality": q,
            "reason": reason,
            "bias": bias,
            "confidence_modifier": conf_mod,
            "effective_prob": effective_prob,
            "sl_adjusted": sl_adj,
            "tp_adjusted": tp_adj,
            "regime": parsed.get("regime", regime),
        }

    logger.warning("[%s] QWEN-UNIFIED HTTP %s: %s", cycle_id, res.status_code, res.text[:120])
    _record_failure(f"HTTP {res.status_code}: {res.text[:80]}")
    return _degraded_unified(
        regime, sl_proposed, tp_proposed, ml_prob, f"Qwen degraded: HTTP {res.status_code}"
    )


async def query_qwen_sltp_validator(
    symbol: str,
    decision: str,
    atr: float,
    atr_pct: float,
    rsi: float,
    bb_pos: float,
    macd_hist: float,
    range_pct: float,
    hour: int,
    sl_proposed: float,
    tp_proposed: float,
    cycle_id: str,
    logger: logging.Logger,
) -> tuple[float, float, str]:
    """
    SL/TP Validator — Qwen analyzes if proposed SL/TP are reasonable.

    Returns: (sl_norm_final, tp_norm_final, reason)
    """
    regime = _classify_regime(bb_pos, atr_pct, macd_hist)
    session = _classify_session(hour)

    sl_pips = sl_proposed * 100.0
    tp_pips = tp_proposed * 200.0

    prompt = f"""Validate the proposed stop-loss and take-profit for {symbol} {decision}.

Current Market Regime:
- ATR: {atr:.5f} ({atr_pct:.2%} of price → regime is {'HIGH VOLATILITY' if atr_pct > 0.015 else 'NORMAL'})
- RSI: {rsi:.1f}
- MACD histogram: {macd_hist:.6f} ({'bullish' if macd_hist > 0 else 'bearish'})
- Bollinger position: {bb_pos:.2f} ({regime})
- Range size: {range_pct:.2%} of price
- Session: {session}

Proposed Trade:
- Direction: {decision}
- Stop-Loss: {sl_pips:.1f} pips (norm={sl_proposed:.4f})
- Take-Profit: {tp_pips:.1f} pips (norm={tp_proposed:.4f})
- Current ATR: {atr:.5f} pips

Is the SL reasonable for this regime? Is the TP at least 1.5x the SL distance?
Reply EXACTLY JSON (no extra text):
{{"sl_ok": true/false, "tp_ok": true/false, "sl_adjusted": 0.20, "tp_adjusted": 0.28, "reason": "brief"}}
If both are OK, return the same values. If adjustment needed, propose sensible ones."""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(QWEN_URL, json={
                "messages": [
                    {"role": "system", "content": "You are a quantitative risk analyst. Always respond in valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.15,
                "max_tokens": 150,
                "response_format": {"type": "json_object"},
            })
    except (httpx.ConnectTimeout, httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("[%s] SLTP-VALIDATOR connection failed: %s", cycle_id, exc)
        _record_failure(f"connection: {repr(exc)[:120]}")
        return float(sl_proposed), float(tp_proposed), "Qwen unavailable"

    if res.status_code == 200:
        try:
            content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            parsed = _parse_json_response(content)
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Respuesta basura del LLM: devolver los valores propuestos, nunca propagar
            logger.warning("[%s] SLTP-VALIDATOR malformed response: %s", cycle_id, exc)
            _record_failure(f"parse: {exc}")
            return float(sl_proposed), float(tp_proposed), f"Qwen degraded: {exc}"

        sl_ok = bool(parsed.get("sl_ok", True))
        tp_ok = bool(parsed.get("tp_ok", True))
        sl_adj = _clamp(float(parsed.get("sl_adjusted", sl_proposed)), SL_MIN_NORM, SL_MAX_NORM)
        tp_adj = _clamp(float(parsed.get("tp_adjusted", tp_proposed)), TP_MIN_NORM, TP_MAX_NORM)
        tp_adj = sl_adj * 2.0

        if not sl_ok or not tp_ok:
            logger.warning(
                "[%s] SLTP-REJECTED ║ sl_ok=%s tp_ok=%s → adjusted sl=%.4f tp=%.4f | reason=%s",
                cycle_id, sl_ok, tp_ok, sl_adj, tp_adj,
                parsed.get("reason", "")[:80]
            )
        _record_success()
        return sl_adj, tp_adj, parsed.get("reason", "")[:100]

    logger.warning("[%s] SLTP-VALIDATOR HTTP %s: %s", cycle_id, res.status_code, res.text[:120])
    _record_failure(f"HTTP {res.status_code}: {res.text[:80]}")
    return float(sl_proposed), float(tp_proposed), f"Qwen degraded: HTTP {res.status_code}"
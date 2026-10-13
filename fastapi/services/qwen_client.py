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
import re
import time
from dataclasses import dataclass, field
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
    # Guarda anti-parrota (orden Félix 2026-10-13, opción C/C+): tokens de
    # la última razón y racha de razones SIMILARES consecutivas POR
    # SÍMBOLO (preparado para G2: estados por mundo, cero acoplamiento).
    last_reason: dict = field(default_factory=dict)
    reason_streak: dict = field(default_factory=dict)


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


def _degraded_unified(regime: str, ml_prob: float, reason: str) -> dict:
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
        "regime": regime,
    }


# RCA 2026-10-13 — razones EXACTAS de los ejemplos FORMAT del prompt
# unificado. Si la respuesta devuelve una de estas razones SIN el campo
# `reentry_ok` (que los ejemplos SIEMPRE llevan), el modelo copió el
# ejemplo (ancla determinista: temperature=0.2, Qwen3-4B, contexto fino =
# variante out-of-window) en lugar de juzgar: se degrada fail-closed en
# vez de fiarse del quality prestado (8.5) y del bias/×1.3 anclados.
# Sincronizado con el prompt por test_format_example_reasons_synced.
FORMAT_EXAMPLE_REASONS = frozenset({
    "trend and ADX align",
    "range chop against signal",
    "Mixed trend and momentum signals",
})

# Orden Félix 2026-10-13 (opción C): razón idéntica ≥3 ciclos consecutivos
# por símbolo = plantilla parrota, no juicio (Qwen3-4B@temp 0.2 emite la
# MISMA razón con datos de mercado distintos). Fail-closed. Los «falsos
# positivos» (juicio genuino repetido) producen el mismo outcome que el
# veto de re-entrada (HOLD) → costo de oportunidad nulo.
PARROT_STREAK_LIMIT = 3

# Orden Félix 2026-10-13 (opción C+): la comparación de la racha es por
# SIMILITUD de tokens (Jaccard ≥ 0.5 = razón «misma»), no igualdad exacta
# — en vivo el modelo escapa del match exacto rotando variantes casi
# idénticas de la misma plantilla (medido 17:10-17:40 UTC: Jaccard entre
# las 3 variantes vivas 0.58-0.67; juicios genuinos de la mañana 0.0-0.4;
# frontera limpia en 0.5).
PARROT_SIMILARITY_THRESHOLD = 0.5

# Orden Félix 2026-10-13 (opción B): temperatura del prompt unificado
# 0.2 → 0.4 — el modelo (Qwen3-4B) a 0.2 cae en un basin determinista de
# plantilla (parrota con q=8.5 / conf_mod=1.30 constantes en 100 % de los
# ciclos out-of-window desde 10:35). 0.4 busca diversidad de decodificación
# para que discrimine de verdad. Las guardas anti-ancla y anti-parrota (C+)
# quedan como red de seguridad: si el síntoma persiste, fail-closed.
QWEN_TEMPERATURE_UNIFIED = 0.4

# Orden Félix 2026-10-13: el Jetson sirve ahora Llama-3.1-8B-Instruct
# (sustituye al Qwen3-4B). Latencia medida 36-39s por llamada idle (vs
# 11-17s del 4B) — con el timeout viejo (20s) el 8B quedaba 'unavailable'
# en cada ciclo (fail-closed: veto de calidad 0). Presupuesto en escalera:
# backend 45s < EA WebRequest 60s (mql5 §predict, v13.4).
QWEN_TIMEOUT_UNIFIED = 45.0


def _reason_tokens(reason: str) -> frozenset:
    """Tokens normalizados de una razón (minúsculas, alfanuméricos)."""
    return frozenset(re.findall(r"[a-z0-9]+", reason.lower()))


def _jaccard(a: frozenset, b: frozenset) -> float:
    """Similitud de Jaccard entre conjuntos de tokens (∅ → 0.0)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _calendar_line(events: Optional[list[dict]]) -> str:
    """Línea `Calendar:` del prompt (paquete 1-4): ventana −2 h…+4 h ya
    filtrada/limitada en routers.ai (top 3, orden ascendente).

    `None` = filtro de noticias deshabilitado (dicho explícitamente — sin
    estado silencioso); `[]` = activo pero sin eventos en la ventana.
    """
    if events is None:
        return "Calendar: unavailable (news filter disabled)"
    if not events:
        return "Calendar: no events in next 4h"
    parts = []
    for ev in events:
        minutes = float(ev["minutes_until"])
        when = (
            f"in {int(round(minutes))} min" if minutes >= 0
            else f"{int(round(-minutes))} min ago"
        )
        parts.append(
            f"{str(ev['time_utc'])[11:16]} {ev['currency']} "
            f"{str(ev['title'])[:40]} ({when})"
        )
    return "Calendar: " + " | ".join(parts)


def _journal_lines(insights: Optional[list[dict]]) -> list[str]:
    """Bloque «Trade journal» del prompt (paquete 1-4, ítem 3).

    Esquema SIEMPRE constante (anti-ancla H2): cabecera + filas o `- none
    yet` — el modelo ve el mismo bloque en todos los ciclos y sólo cambian
    los datos. Muestra `pnl/dirección/salida/régimen/insight` de los últimos
    cierres; NUNCA `quality_score`/`qwen_confidence` (anclarían la respuesta
    en puntuaciones pasadas). Insight truncada a 48 chars y tope de 5 filas
    (presupuesto de tokens del prompt)."""
    lines = ["Trade journal (recent closes) — context only — do NOT copy values:"]
    rows = (insights or [])[:5]
    if not rows:
        lines.append("- none yet")
        return lines
    for row in rows:
        # Indexación estricta en columnas NOT-NULL (mismo contrato que el
        # bloque de re-entrada); `regime` es nullable → .get con honesto 'n/a'.
        lines.append(
            f"- {row['created_at']:%m-%d %H:%M} {row['direction']} "
            f"pnl={float(row['pnl_pct']) * 100:+.2f}% exit={row['exit_reason']} "
            f"regime={row.get('regime') or 'n/a'}: {str(row['qwen_insight'])[:48]}"
        )
    return lines


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
    # R1 (Félix 2026-10-09): contexto de re-entrada — el veto de re-entrada
    # lo decide el CEREBRO con estos datos (ventana de churn, cierre reciente
    # y movimiento). `None` = fuera de ventana / sin entrada propuesta.
    reentry: Optional[dict] = None,
    # Paquete 1-4 (Félix 2026-10-09): calendario económico ya filtrado por
    # routers.ai (−120…+240 min, top 3). `None` = filtro de noticias
    # deshabilitado; `[]` = activo sin eventos en la ventana.
    calendar_events: Optional[list[dict]] = None,
    # Foco ampliado H4/D (Félix 2026-10-12): marcos SUPERIORES — bloque ya
    # formateado por routers.ai (cabecera + `- H4:` + `- D1:`, con
    # «Unavailable» si el fetch falló). `None` = sin bloque (back-compat).
    # Contexto de tendencia mayor, nunca de puntuación (restricción H2).
    higher_tf_context: Optional[str] = None,
) -> dict:
    """
    Unified Qwen query — single call per candle.

    Returns:
        - quality_score: 0-10
        - quality_reason: str
        - llm_bias: BULLISH/BEARISH/NEUTRAL
        - confidence_modifier: 0.5-1.5
        - regime: TRENDING/RANGING/VOLATILE/BREAKOUT
        - reentry_ok: bool — juicio anti-churn de la re-entrada (R1); true
          si no hay cierre reciente o el cerebro considera legítima la
          entrada; false = churn (→ veto_reason=reentry_veto en serve)

    Contrato sin SL/TP (paquete 1-4): los campos `sl_ok/tp_ok/sl_adjusted/
    tp_adjusted` murieron con la Opción 1 (production_stops = única fuente
    de stops); si el modelo los ecoa se DESCARTAN (no se piden, no se
    devuelven). El validador Opción 2 (`query_qwen_sltp_validator`) se
    conserva como servicio, sin cablear en /predict.
    """
    session = _classify_session(hour)
    if regime is None:
        regime = _classify_regime(bb_pos, atr_pct, macd_hist)

    # ADX-based regime detection (Fase 2: Qwen Context Awareness)
    if adx is not None and adx < 0.25:
        regime = "RANGING"
    elif regime == "RANGING" and adx is not None and adx >= 0.25:
        regime = "TRENDING"

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

    # Foco ampliado H4/D: contexto de marco superior DENTRO del bloque
    # Context (antes de `- Candles:`). Esquema constante anti-ancla H2.
    if higher_tf_context:
        context_lines.extend(higher_tf_context.splitlines())

    # P2 / R1: bloque de re-entrada — SIEMPRE presente (esquema de respuesta
    # constante, estable frente al anti-ancla H2): dentro de la ventana el
    # cerebro recibe el cierre reciente y la rúbrica churn; fuera de ella
    # (o sin entrada propuesta) se instruye reentry_ok=true.
    if reentry is not None:
        last_exit = reentry.get("last_exit")
        move = None if last_exit is None else last_exit.get("price_move_since_close_pips")
        reentry_lines = [
            "Reentry check — a position was closed recently:",
            f"- Bars since close: {reentry['bars_since_close']} H1",
        ]
        if last_exit is None:
            reentry_lines.append(
                "- Exit details unavailable: judge on bars since close only.",
            )
        else:
            reentry_lines.extend([
                f"- Last exit: {last_exit['direction']} ({last_exit['exit_reason']}), "
                f"PnL {last_exit['pnl']:+.2f} EUR, at {last_exit['exit_time']}",
                "- Price move since close: "
                + (f"{move:+.1f} pips" if move is not None else "unavailable"),
            ])
        reentry_lines.extend([
            "- reentry_ok=false if re-opening the same trade moments after closing it",
            "  with no new evidence (churn); true if new information, structure/regime",
            "  change or fresh evidence justifies entering now.",
        ])
    else:
        # RCA (2026-10-13): esta variante (2 líneas, ~90% de los ciclos) era
        # la que colapsaba en copia del ancla — formato paridad con la
        # in-window (cabecera «Reentry check — …:» + instrucción explícita).
        reentry_lines = [
            "Reentry check — no recent close inside the churn window"
            " (or no entry proposed):",
            "- No churn risk: set reentry_ok=true.",
        ]

    context_lines.extend([
        f"- Candles: {candle_context}",
        "",
        f"News: {news_context[:400] if news_context != 'Unavailable' else 'Unavailable'}",
        # Paquete 1-4: calendario económico tras News (misma sección)
        _calendar_line(calendar_events),
        "",
        f"ML Signal: {decision} with ML confidence {ml_prob:.3f}",
        "",
        # Paquete 1-4 (ítem 3): journal de cierres recientes — contexto de
        # EXECUTION pasada, nunca de puntuación (sin quality/confidence).
        *_journal_lines(recent_insights),
        "",
        *reentry_lines,
        "",
        # P2-H2 (2026-10-09): el ejemplo único con quality=7.5/bias=NEUTRAL
        # anclaba la respuesta en la plantilla (temperature=0.2 + modelo
        # pequeño → quality_score constante 7.5 en 511+ ciclos = veto de
        # calidad decorativo). Dos ejemplos con valores separados + rúbrica
        # explícita: la propiedad ">=2 ejemplos distintos" la fija
        # tests/test_qwen_prompt_anchoring.py.
        # RCA (2026-10-13): con dos ejemplos la variante out-of-window caía
        # en copia byte a byte del ejemplo #1 sin `reentry_ok` (100% de los
        # ciclos desde 10:35). Tres ejemplos (prior más plano) + advertencia
        # POST-ejemplos + guarda anti-ancla en el parse (fail-closed).
        "Return EXACTLY JSON, no extra text — ONE JSON object. Three FORMAT",
        "examples with different scores; do NOT copy their values — judge THIS",
        "setup on its own context:",
        '{"quality": 8.5, "reason": "trend and ADX align", "bias": "BULLISH", "confidence_modifier": 1.3,'
        '  "reentry_ok": true, "regime": "TRENDING"}',
        '{"quality": 3.0, "reason": "range chop against signal", "bias": "BEARISH", "confidence_modifier": 0.7,'
        '  "reentry_ok": false, "regime": "RANGING"}',
        '{"quality": 5.0, "reason": "Mixed trend and momentum signals", "bias": "NEUTRAL", "confidence_modifier": 0.9,'
        '  "reentry_ok": true, "regime": "VOLATILE"}',
        "",
        "Any value COPIED from the examples (score, reason, bias, modifier) is",
        "wrong — reason must cite THIS setup from the Context above (trend,",
        "ADX, session, ML signal).",
        "",
        "Fields: quality 0-10 — score on evidence: 7-10 only when trend/ADX/session",
        "agree with the ML signal and structure is clean; 1-4 when ranging chop,",
        "against-trend signal or unverified news; 4-7 when mixed. reason max 12 words;",
        "bias BULLISH/BEARISH/NEUTRAL aligned with the setup; confidence_modifier",
        "0.5-1.5 moving effective confidence the same direction; regime",
        "TRENDING/RANGING/VOLATILE/BREAKOUT; reentry_ok boolean per the Reentry",
        "check block above (true when there is no recent close).",
    ])

    prompt = "\n".join(context_lines)

    messages = [
        {"role": "system", "content": "You are a quantitative trading analyst. Always respond in valid JSON."},
        {"role": "user", "content": prompt},
    ]

    # Traza de entrada/salida del LLM (orden de Félix 2026-10-09): el prompt
    # COMPLETO (system+user) ANTES de la llamada — visible también si la
    # conexión falla — y la respuesta cruda DESPUÉS (más abajo), antes del
    # parse. Ambos con la misma lifecycle que la decisión: un solo par de
    # líneas por ciclo de /predict.
    logger.info(
        "[%s] QWEN-PROMPT ║\n%s",
        cycle_id,
        "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages),
    )

    try:
        async with httpx.AsyncClient(timeout=QWEN_TIMEOUT_UNIFIED) as client:
            res = await client.post(QWEN_URL, json={
                "messages": messages,
                "temperature": QWEN_TEMPERATURE_UNIFIED,
                "max_tokens": 160,
                "response_format": {"type": "json_object"},
            })
    except (httpx.ConnectTimeout, httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("[%s] QWEN-UNIFIED connection failed: %s", cycle_id, repr(exc))
        _record_failure(f"connection: {repr(exc)[:120]}")
        return _degraded_unified(regime, ml_prob, "Qwen unavailable")

    if res.status_code == 200:
        # Traza: cuerpo crudo ANTES de parsear — si el parse falla, la
        # respuesta del modelo sigue siendo visible en el log.
        logger.info("[%s] QWEN-RESPONSE ║ %s", cycle_id, res.text)
        try:
            content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            parsed = _parse_json_response(content)
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Respuesta basura del LLM: degradar, nunca propagar (P1-6)
            logger.warning("[%s] QWEN-UNIFIED malformed response: %s", cycle_id, exc)
            _record_failure(f"parse: {exc}")
            return _degraded_unified(regime, ml_prob, f"Qwen degraded: {exc}")

        q = float(parsed.get("quality", 5.0))
        reason = str(parsed.get("reason", ""))[:120]
        bias_raw = str(parsed.get("bias", "NEUTRAL")).upper()
        bias = "NEUTRAL"
        if "BULL" in bias_raw:
            bias = "BULLISH"
        elif "BEAR" in bias_raw:
            bias = "BEARISH"

        conf_mod = _clamp(float(parsed.get("confidence_modifier", 1.0)), 0.5, 1.5)

        # Guarda anti-ancla (RCA 2026-10-13): razón == razón de ejemplo FORMAT
        # SIN `reentry_ok` (campo que los ejemplos SIEMPRE llevan) = el modelo
        # copió el ancla en lugar de juzgar (10/10 ciclos out-of-window desde
        # 10:35; replay 18:04 UTC reproducido 100%). Fail-closed: degradada
        # (quality=0 → veto de calidad) en vez de fiarse del quality prestado.
        # Con `reentry_ok` presente se asume juicio real (test de especificidad).
        if "reentry_ok" not in parsed and reason.strip() in FORMAT_EXAMPLE_REASONS:
            logger.warning(
                "[%s] QWEN-UNIFIED ancla detectada (reason de ejemplo FORMAT sin "
                "reentry_ok) → respuesta degradada fail-closed",
                cycle_id,
            )
            _record_failure("anchor-copy")
            return _degraded_unified(
                regime, ml_prob, "Qwen degraded: anchor-copy (reason de ejemplo sin reentry_ok)",
            )

        # Guarda anti-parrota (orden Félix 2026-10-13, opción C → C+): razón
        # SIMILAR (Jaccard de tokens ≥ PARROT_SIMILARITY_THRESHOLD) a la
        # anterior ≥ PARROT_STREAK_LIMIT ciclos consecutivos por símbolo =
        # plantilla parrota, no juicio — inmune a cualquier ancla futura y
        # a la rotación de variantes observada en vivo (casi idénticas
        # byte a byte NO; con jitter de palabras SÍ). Sólo cuenta
        # respuestas que superaron la guarda anti-ancla. La racha se
        # resetea con cualquier razón disímil (p. ej. la variante
        # in-window que sí juzga).
        rsn = reason.strip()
        if rsn:
            tokens = _reason_tokens(rsn)
            prev_tokens = _QWEN_STATE.last_reason.get(symbol)
            similar = (
                prev_tokens is not None
                and _jaccard(tokens, prev_tokens) >= PARROT_SIMILARITY_THRESHOLD
            )
            streak = _QWEN_STATE.reason_streak.get(symbol, 0) + 1 if similar else 1
            _QWEN_STATE.last_reason[symbol] = tokens
            _QWEN_STATE.reason_streak[symbol] = streak
            if streak >= PARROT_STREAK_LIMIT:
                logger.warning(
                    "[%s] QWEN-UNIFIED parrota detectada (%d ciclos con razón similar, "
                    "símbolo %s) → respuesta degradada fail-closed",
                    cycle_id, streak, symbol,
                )
                _record_failure("parrot")
                return _degraded_unified(
                    regime, ml_prob, f"Qwen degraded: parrot ({streak}x razon similar)",
                )

        # Paquete 1-4: sin parse de SL/TP — si el modelo ecoa sl_adjusted/
        # tp_adjusted se ignoran (no se piden en el prompt, no se devuelven).
        effective_prob = float(np.exp(np.log(max(ml_prob, 1e-6)) + np.log(conf_mod)))
        effective_prob = _clamp(effective_prob, 0.0, 1.0)

        # R1: juicio de re-entrada — default true (fail-open) si el modelo
        # no devolvió el campo: veto sólo por juicio explícito, nunca
        # inventado; la ausencia se señala con WARNING (nunca silencioso).
        reentry_ok = bool(parsed.get("reentry_ok", True))
        if "reentry_ok" not in parsed:
            logger.warning(
                "[%s] QWEN-UNIFIED reentry_ok ausente en la respuesta → true (fail-open)",
                cycle_id,
            )

        logger.info(
            "[%s] QWEN-UNIFIED ║ q=%.1f bias=%s conf_mod=%.2f eff_prob=%.4f | "
            "regime=%s reentry_ok=%s | %s",
            cycle_id, q, bias, conf_mod, effective_prob,
            parsed.get("regime", regime), reentry_ok, reason[:60]
        )
        _record_success()
        return {
            "ok": True,
            "quality": q,
            "reason": reason,
            "bias": bias,
            "confidence_modifier": conf_mod,
            "effective_prob": effective_prob,
            "regime": parsed.get("regime", regime),
            "reentry_ok": reentry_ok,
        }

    logger.warning("[%s] QWEN-UNIFIED HTTP %s: %s", cycle_id, res.status_code, res.text[:120])
    _record_failure(f"HTTP {res.status_code}: {res.text[:80]}")
    return _degraded_unified(regime, ml_prob, f"Qwen degraded: HTTP {res.status_code}")


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
"""
Qwen LLM client — extracted from routers/ai.py.

Provides unified Qwen queries for trade analysis and SL/TP validation.
All functions return safe defaults on ANY failure (connection, HTTP != 200,
malformed response) — Qwen nunca crashea /predict; el estado degradado se
registra en QwenHealthState y lo reporta /pipeline/health (P1-6).
"""
import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

import httpx
import numpy as np

from services.memory_client import format_memory_lines

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
    # S1 (orden Félix 2026-10-14): la racha sólo cuenta si la quality NO
    # varía — `last_quality` guarda la quality del último juicio por
    # símbolo para poder compararla (ver PARROT_STREAK_LIMIT).
    last_reason: dict = field(default_factory=dict)
    reason_streak: dict = field(default_factory=dict)
    last_quality: dict = field(default_factory=dict)


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


def _has_evidence(reason: str) -> bool:
    """T-31: la razón del LLM debe citar un NÚMERO del contexto.

    Sin número no hay evidencia: es una frase reutilizable (plantilla). Se
    acepta cualquier dígito (precio, %, ATR, ADX, hora, sesión) — el
    objetivo NO es validar el número (no hay parser semántico), sino
    detectar la respuesta de plantilla que medió el 90,6 % de los ciclos.
    """
    return any(ch.isdigit() for ch in reason)


def _abstain_unified(regime: str, ml_prob: float, reason: str) -> dict:
    """ABSTENCIÓN (T-31, 2026-10-19) — el LLM respondió pero SU RESPUESTA NO
    APORTA EVIDENCIA (no cita ningún número del contexto).

    No es lo mismo que estar ROTO (`_degraded_unified`, fail-closed: sin
    LLM no se opera). Aquí el setup sigue siendo observable por el PPO y
    por los controles de riesgo (SL/TP, sizing, límites de pérdida, news,
    time-stop): el juez se APARTA y deja decidir al PPO. Antes, una
    respuesta de plantilla (quality 7-10 inflada, sesgo copiado) bloqueaba
    el 100 % de las operaciones; ahora el juez sin evidencia no vota, y su
    veto además exige autoridad demostrada (services/qwen_authority.py).

    Declarado en la auditoría como `abstained=True` — nunca silencioso.
    """
    return {
        "ok": True,
        "abstained": True,
        "quality": 5.0,          # neutral: pasa el umbral de calidad (5.0)
        "reason": reason[:120],
        "bias": "NEUTRAL",
        "bias_prob": 0.5,
        "confidence_modifier": 1.0,
        "effective_prob": float(ml_prob),
        "regime": regime,
    }


def _degraded_unified(regime: str, ml_prob: float, reason: str) -> dict:
    """Safe defaults uniformes ante CUALQUIER fallo de Qwen.

    quality=0.0 es deliberadamente fail-closed: por debajo de QUALITY_THRESHOLD
    el veto de predict revierte BUY/SELL a HOLD mientras Qwen esté degradado.
    """
    return {
        "ok": False,
        "abstained": False,
        "quality": 0.0,
        "reason": reason[:120],
        "bias": "NEUTRAL",
        # T-31: 0.5 neutral — sin poder de veto en degradación (antes la
        # clave faltaba y el default 1.0 del veto convertía cualquier
        # degradación en veto direccional fantasma).
        "bias_prob": 0.5,
        "confidence_modifier": 1.0,
        "effective_prob": float(ml_prob),
        "regime": regime,
    }


# RCA 2026-10-13 — la guarda anti-ancla (c) degradaba fail-closed la razón
# exacta de un ejemplo FORMAT devuelta SIN `reentry_ok` = el modelo copió
# el ancla en lugar de juzgar (Qwen3-4B@temp 0.2, replay 18:04 UTC 100 %
# reproducible). RETIRADA en S2 (orden Félix 2026-10-14): el Llama-3.1-8B
# nunca copió el ejemplo (0 disparos, 20:47 UTC 10-13 → 10:10 UTC 10-14) —
# el ancla era un defecto de decodificación del 4B, no del prompt. Con la
# guarda se retiró la constante FORMAT_EXAMPLE_REASONS (las razones
# ejemplo viven sólo en el prompt). El campo `reentry_ok` ausente sigue
# emitiendo WARNING + fail-open R1 (decisión aparte, intacta).

# Orden Félix 2026-10-13 (opción C): razón idéntica ≥3 ciclos consecutivos
# por símbolo = plantilla parrota, no juicio (Qwen3-4B@temp 0.2 emite la
# MISMA razón con datos de mercado distintos). Fail-closed. Los «falsos
# positivos» (juicio genuino repetido) producen el mismo outcome que el
# veto de re-entrada (HOLD) → costo de oportunidad nulo.
# S1 (orden Félix 2026-10-14): el disparador exige AMBAS condiciones —
# razón similar (Jaccard ≥0.5) Y quality SIN variar. Motivo medido: con el
# Llama-3.1-8B las razones genuinas en trending son similares por texto
# (Jaccard 0.50-0.60 ≥ umbral) pero su quality VARÍA (6.0-9.0) → la guarda
# solo-por-texto degradaba juicio real (falso positivo 21:25/21:30 = 2/12
# ciclos). La firma parrota real del 4B tenía quality byte a byte fija
# (8.5) — ese patrón sigue degradando.
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
# para que discrimine de verdad. La guarda anti-parrota (C+, recalibrada
# en S1) queda como red de seguridad: si el síntoma persiste, fail-closed.
# (La guarda anti-ancla se retiró en S2 — 0 disparos con el 8B.)
QWEN_TEMPERATURE_UNIFIED = 0.4

# Orden Félix 2026-10-13: el Jetson sirve ahora Llama-3.1-8B-Instruct
# (sustituye al Qwen3-4B). Latencia medida 36-39s por llamada idle (vs
# 11-17s del 4B) — con el timeout viejo (20s) el 8B quedaba 'unavailable'
# en cada ciclo (fail-closed: veto de calidad 0). Presupuesto en escalera:
# backend 45s < EA WebRequest 60s (mql5 §predict, v13.4).
QWEN_TIMEOUT_UNIFIED = 45.0

# ── G2-1 (Félix 2026-10-17): serialización global de Qwen — concurrencia 1 ────
# Un SOLO request en vuelo contra el modelo, siempre. El lock vive DENTRO de
# las funciones de entrada (query_qwen_unified / query_qwen_sltp_validator /
# trade-insight) → todos los llamadores quedan serializados sin que tengan
# que saberlo (cero acoplamiento con routers/). Orden único de adquisición:
# aquí no se adquiere NINGÚN otro lock ⇒ no puede formarse ciclo ⇒ sin
# deadlock (test: el path Qwen nunca corre bajo el lock del journal).
# Métrica de cola: espera registrada en cada adquisición (INFO); WARNING si
# supera QWEN_LOCK_WARN_S = 90 s = 2 × timeout unificado, el peor caso sano
# con 2 símbolos apilados + un trade-insight — por encima hay apilamiento
# anómalo (ciclos solapados más allá del presupuesto de 5 min/par).
QWEN_LOCK = asyncio.Lock()
QWEN_LOCK_WARN_S = 90.0


@asynccontextmanager
async def qwen_serialized(cycle_id: str):
    """Sección crítica de red contra Qwen: 1 request en vuelo + métrica de cola.

    Envuelve SÓLO el `client.post` (la latencia del modelo dentro del lock;
    nunca parseo ni BD). Libera el lock en cualquier salida, incluida
    excepción — la serialización es fail-closed: si algo falla, el lock NO
    queda cogido. La métrica de cola se emite SIEMPRE en el logger de este
    módulo (infraestructura del cliente, independiente del logger que pase
    cada caller) — así /pipeline/health y los tests la ven en un solo sitio.
    """
    t0 = time.monotonic()
    await QWEN_LOCK.acquire()
    wait_s = time.monotonic() - t0
    try:
        if wait_s > QWEN_LOCK_WARN_S:
            logger.warning(
                "[%s] QWEN-LOCK wait=%.1fs > %.0fs — cola de Qwen apilada",
                cycle_id, wait_s, QWEN_LOCK_WARN_S,
            )
        else:
            logger.info("[%s] QWEN-LOCK wait=%.3fs", cycle_id, wait_s)
        yield
    finally:
        QWEN_LOCK.release()


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
    # T-10 (Félix 2026-10-15): memorias a largo plazo — inyección
    # DETERMINISTA (memory como dato, ver services/memory_client). Bloque en
    # el USER prompt tras las velas, nunca en system (restricción H2).
    # `None`/[] = sin bloque → prompt byte-idéntico al pre-T-10 (back-compat).
    memories: Optional[list[dict]] = None,
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
        # T-10: memoria a largo plazo — sección tras las velas (dato de
        # contexto, nunca instrucción); con lista vacía no hay líneas y el
        # prompt queda byte-idéntico al pre-T-10 (back-compat testada).
        *format_memory_lines(memories),
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
        # T-31 (2026-10-19) — RCA DEL ANCLAJE (medida, no hipotética):
        # el ejemplo #1 era BULLISH con razón positiva ("trend and ADX
        # align"). El 8B determinista (temp 0.2) lo COPIABA casi literal en
        # 289/319 ciclos (90,6 %) — de esas, 274 BULLISH (94,8 %). El
        # "monocultivo BULLISH" NO era un juicio de mercado: era la
        # PLANTILLA del ejemplo.
        # MEDICIÓN 2 (tras el 1er intento: exemplos sin "premio"): el modelo
        # copió el ejemplo #3 íntegro (quality 7.0, bias_prob 0.68, conf_mod
        # 1.2 — valores idénticos). Conclusión: a este modelo (8B Q2_K,
        # temp 0.2) un ejemplo con LITERALES es una plantilla, sea cual sea
        # su contenido; las instrucciones textuales no lo detienen.
        # ⇒ SOLUCIÓN ESTRUCTURAL: NO hay ejemplos con literales. Sólo la
        # FORMA con marcadores de posición (nada que copiar) + rúbrica.
        # La rúbrica se reescribe SIN eslóganes copiables (la rúbrica vieja
        # —"trend/ADX/session agree"— era ella misma la frase copiada).
        # Y la respuesta sin evidencia (razón sin ningún número del contexto)
        # se marca como ABSTENCIÓN explícita: el LLM no vota, el PPO decide.
        "Return EXACTLY JSON, no extra text — ONE JSON object with these keys",
        "and ONLY these keys:",
        '{"quality": <number 0-10>, "reason": "<max 12 words, MUST contain a',
        'number taken from THIS Context>", "bias": "<BULLISH|BEARISH|NEUTRAL>",',
        ' "bias_prob": <number 0.00-1.00>, "confidence_modifier": <number 0.5-1.5>,',
        ' "reentry_ok": <true|false>, "regime": "<TRENDING|RANGING|VOLATILE|BREAKOUT>"}',
        "",
        "There are NO examples to imitate: every value must come from THIS",
        "Context. Fill the placeholders above with your own reading.",
        "",
        "EVIDENCE FIRST: the reason MUST contain a NUMBER that appears in the",
        "Context (a price distance, a percentage, an indicator value, a time)",
        "— a sentence that would fit any other setup is not a reason. If you",
        "find no number worth citing, write \"no clear evidence\" and keep",
        "quality near the middle.",
        "",
        "quality 0-10: use the high band only if the Context's own numbers",
        "point the same way as the ML signal and nothing contradicts them; use",
        "the low band if they conflict; use the middle band if they are mixed.",
        "bias: the direction THIS Context supports, not the direction of the",
        "ML signal by default. bias_prob 0.00-1.00 = your honest probability",
        "that price is HIGHER in 24h: 0.50 when genuinely uncertain, 0.40-0.60",
        "for a weak lean, 0.65-0.75 only when several independent numbers",
        "agree, never above 0.85; its sign must match bias (>0.50 BULLISH,",
        "<0.50 BEARISH, about 0.50 NEUTRAL). confidence_modifier 0.5-1.5 moves",
        "the effective confidence the same direction as your bias. regime:",
        "TRENDING/RANGING/VOLATILE/BREAKOUT. reentry_ok: true only when the",
        "Reentry check block above says there is no recent close.",
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
        async with qwen_serialized(cycle_id):
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

        # T-31 Fase B (orden Félix 2026-10-19 — «conf válido, estable y
        # razonado», resolver de raíz): probabilidad direccional REAL del LLM.
        # RCA: el veto leía `qwen["confidence"]` — clave que este dict nunca
        # devolvía (solo `confidence_modifier`) → default 1.0 fantasma → el
        # veto se disparaba SIEMPRE que el sesgo oponía, con «conf=1.00»
        # inventada en el log (medido: 100% de los vetos, ventana T-28
        # conflict 0.96; Fase A: exactitud del veto 22% = anti-predictivo).
        # Ahora: probabilidad explícita 0-1 pedida con rúbrica de calibración
        # en el prompt, clamp a [0,1] y default 0.5 (NEUTRAL = sin poder de
        # veto) — la incoherencia bias↔prob falla seguro (no dispara).
        bias_prob = _clamp(float(parsed.get("bias_prob", 0.5)), 0.0, 1.0)

        # T-31 (2026-10-19): la respuesta del LLM debe APORTAR EVIDENCIA — la
        # razón tiene que citar un número del contexto (precio, %, indicador,
        # hora). Una razón sin ningún número es una plantilla: no es juicio.
        # Medido: 289/319 ciclos (90,6 %) copiaban la frase del ejemplo y
        # producían quality inflada + sesgo BULLISH constante.
        # ⇒ el juez SIN EVIDENCIA se Abstiene (no vota, no bloquea): el PPO
        # decide con los controles de riesgo intactos. Declarado, nunca
        # silencioso (`abstained=True` en la respuesta y en la auditoría).
        rsn = reason.strip()
        if not _has_evidence(rsn):
            logger.warning(
                "[%s] QWEN-UNIFIED respuesta SIN EVIDENCIA (razón sin número del "
                "contexto: %r) → abstención declarada; decide el PPO",
                cycle_id, rsn[:60],
            )
            return _abstain_unified(regime, ml_prob, f"abstained: sin evidencia ({rsn[:60]})")

        # Guarda anti-parrota (orden Félix 2026-10-13, opción C → C+; S1
        # 2026-10-14): razón SIMILAR (Jaccard ≥ umbral) Y quality SIN
        # variar ≥ PARROT_STREAK_LIMIT ciclos consecutivos por símbolo =
        # plantilla parrota, no juicio. Cada respuesta parseada cuenta la
        # racha (S2 retiró la guarda anti-ancla que interceptaba antes).
        # La racha se resetea con cualquier razón disímil O quality
        # distinta (juicio genuino del 8B: razones trending similares pero
        # q variable 6-9 → NO degrada).
        # T-31: la parrota pasa de DEGRADAR (fail-closed → HOLD, es decir
        # parálisis) a ABSTENCIÓN (el juez no vota, el PPO decide): una
        # plantilla repetida no es información, pero el setup sigue siendo
        # observable por el PPO y por los controles de riesgo.
        if rsn:
            tokens = _reason_tokens(rsn)
            prev_tokens = _QWEN_STATE.last_reason.get(symbol)
            prev_q = _QWEN_STATE.last_quality.get(symbol)
            similar = (
                prev_tokens is not None
                and prev_q is not None
                and _jaccard(tokens, prev_tokens) >= PARROT_SIMILARITY_THRESHOLD
                and q == prev_q
            )
            streak = _QWEN_STATE.reason_streak.get(symbol, 0) + 1 if similar else 1
            _QWEN_STATE.last_reason[symbol] = tokens
            _QWEN_STATE.last_quality[symbol] = q
            _QWEN_STATE.reason_streak[symbol] = streak
            if streak >= PARROT_STREAK_LIMIT:
                logger.warning(
                    "[%s] QWEN-UNIFIED parrota detectada (%d ciclos con razón similar, "
                    "símbolo %s) → abstención declarada (el juez no vota)",
                    cycle_id, streak, symbol,
                )
                _record_failure("parrot")
                return _abstain_unified(
                    regime, ml_prob, f"abstained: parrot ({streak}x razon similar)",
                )

        # Paquete 1-4: sin parse de SL/TP — si el modelo ecoa sl_adjusted/
        # tp_adjusted se ignoran (no se piden en el prompt, no se devuelven).
        # T-05 (2026-10-15): `effective_prob` sigue recortado a [0,1] — es
        # el valor DECISIONAL (gates intactos). `effective_prob_raw` expone
        # el producto sin recortar: con conf_mod > 1 y prob alta el techo se
        # satura en 1.0000 (17/17 entradas) y la gradación de confianza era
        # invisible. Telemetría pura, cero cambio de semántica.
        effective_prob_raw = float(np.exp(np.log(max(ml_prob, 1e-6)) + np.log(conf_mod)))
        effective_prob = _clamp(effective_prob_raw, 0.0, 1.0)

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
            "[%s] QWEN-UNIFIED ║ q=%.1f bias=%s bias_prob=%.2f conf_mod=%.2f eff_prob=%.4f | "
            "regime=%s reentry_ok=%s | %s",
            cycle_id, q, bias, bias_prob, conf_mod, effective_prob,
            parsed.get("regime", regime), reentry_ok, reason[:60]
        )
        _record_success()
        return {
            "ok": True,
            "abstained": False,
            "quality": q,
            "reason": reason,
            "bias": bias,
            "bias_prob": bias_prob,
            "confidence_modifier": conf_mod,
            "effective_prob": effective_prob,
            "effective_prob_raw": effective_prob_raw,
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
        async with qwen_serialized(cycle_id):
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
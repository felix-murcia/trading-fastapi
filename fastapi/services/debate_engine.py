"""
Motor de debate multi-agente.

Flujo:
  1. BULL  — argumenta a favor de la señal mayoritaria
  2. BEAR  — argumenta en contra
  3. JUDGE — lee ambos argumentos y emite el veredicto final JSON
"""

import json
import logging
import re

from models.debate import DebateRequest, DebateResponse
from services.llm_client import call_llm

logger = logging.getLogger(__name__)


def _format_signals(signals: list) -> str:
    return " | ".join(
        f"{s.agent.upper()} {s.signal.upper()} {s.confidence:.2f}"
        for s in signals
        if not s.parse_error
    )


def _build_context(req: DebateRequest) -> str:
    lines = [
        f"Par: {req.best_pair} | Precio: {req.price}",
        f"RSI: {req.rsi} | Tendencia M5: {req.trend} | ATR: {req.atr}",
        f"Señales agentes: {_format_signals(req.signals)}",
    ]
    if req.drivers:
        lines.append(f"Drivers del par: {req.drivers}")
    if req.news_summary:
        lines.append(f"Noticias recientes: {req.news_summary}")
    if req.calendar_summary:
        lines.append(f"Eventos calendario: {req.calendar_summary}")
    return "\n".join(lines)


async def run_debate(req: DebateRequest) -> DebateResponse:
    ctx = _build_context(req)

    # ── 1. Argumentos BULL ────────────────────────────────────────────────────
    bull_prompt = (
        f"Eres un analista alcista experto en Forex. "
        f"En 3-4 frases concretas argumenta por qué comprar {req.best_pair} "
        f"ahora mismo es una buena operación. Usa los datos del contexto.\n\n{ctx}"
    )
    bull_raw = await call_llm(bull_prompt)

    # ── 2. Argumentos BEAR ────────────────────────────────────────────────────
    bear_prompt = (
        f"Eres un analista bajista experto en Forex. "
        f"En 3-4 frases concretas argumenta por qué comprar {req.best_pair} "
        f"ahora mismo es mala idea o el precio podría caer. Usa los datos del contexto.\n\n{ctx}"
    )
    bear_raw = await call_llm(bear_prompt)

    # ── 3. Juez ───────────────────────────────────────────────────────────────
    judge_prompt = (
        f"Eres el árbitro de un debate de trading sobre {req.best_pair} (precio {req.price}).\n\n"
        f"CONTEXTO:\n{ctx}\n\n"
        f"ARGUMENTO ALCISTA:\n{bull_raw[:400]}\n\n"
        f"ARGUMENTO BAJISTA:\n{bear_raw[:400]}\n\n"
        f"Basándote en los argumentos y el contexto, emite tu veredicto SOLO en este JSON "
        f"(sin texto adicional, sin bloques de código):\n"
        f'{"{"}"signal": "buy|sell|neutral", "confidence": 0.0, "reasoning": "una frase concisa"{"}"}\n'
        f"Donde signal es exactamente buy, sell o neutral, y confidence entre 0.0 y 1.0."
    )
    judge_raw = await call_llm(judge_prompt)

    # ── Parse veredicto del juez ──────────────────────────────────────────────
    m = re.search(r"\{[\s\S]*?\}", judge_raw)
    if not m:
        logger.warning("debate judge parse failed — raw: %r", judge_raw[:200])
        raise ValueError(f"judge_parse_failed: no JSON block in: {judge_raw[:200]}")

    verdict = json.loads(m.group(0))
    signal = verdict.get("signal", "")
    if signal not in ("buy", "sell", "neutral"):
        raise ValueError(f"judge_invalid_signal: {signal!r}")

    confidence = max(0.0, min(1.0, float(verdict.get("confidence", 0.5))))
    reasoning  = verdict.get("reasoning", "")

    return DebateResponse(
        signal=signal,
        confidence=confidence,
        reasoning=reasoning,
        bull_argument=bull_raw[:300],
        bear_argument=bear_raw[:300],
    )

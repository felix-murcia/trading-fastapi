"""
Opción 5 — Post-Trade Journal:
Analiza trades cerrados usando Qwen para descubrir patrones
y almacenarlos como insights en la base de datos.

Trigger: después de cada cierre de trade (SL, TP, manual)
Flujo:
  1. record_trade_closed() detecta cierre → llama a analyze_trade()
  2. analyze_trade() consulta Qwen con contexto completo del trade
  3. Qwen responde con insights → almacenados en trade_insights
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pandas as pd

from config import settings
from db.connection import get_pool

logger = logging.getLogger(__name__)

QWEN_URL = "http://100.90.16.33:8080/v1/chat/completions"
JOURNAL_LOCK = asyncio.Lock()


# ── Tabla: trade_insights ──────────────────────────────────────────────────────
JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_insights (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        BIGINT REFERENCES trade_outcomes(id),
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    pnl_pct         NUMERIC(8,4) NOT NULL,
    exit_reason     TEXT NOT NULL,
    qwen_insight    TEXT NOT NULL,    -- patrón detectado por Qwen
    qwen_confidence NUMERIC(3,2),     -- 0.00-1.00
    regime          TEXT,             -- régimen al momento del trade
    quality_score   NUMERIC(3,1),    -- quality score del setup
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_insights_symbol ON trade_insights(symbol);
CREATE INDEX IF NOT EXISTS idx_insights_created ON trade_insights(created_at);
"""


async def init_journal_table():
    """Crea la tabla trade_insights si no existe."""
    try:
        pool = get_pool()
        await pool.execute(JOURNAL_SCHEMA)
        logger.info("[JOURNAL] Tabla trade_insights lista")
    except Exception as e:
        logger.warning("[JOURNAL] No se pudo crear trade_insights: %s", e)


async def record_trade_closed(
    trade_id: int,
    symbol: str,
    direction: str,
    entry_time: datetime,
    exit_time: datetime,
    pnl_pct: float,
    exit_reason: str,
    sl_hit: bool,
    tp_hit: bool,
) -> None:
    """
    Llamado desde order_manager cuando un trade se cierra.
    Dispara análisis asíncrono con Qwen (no bloquea).
    """
    asyncio.create_task(
        _analyze_trade_async(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            entry_time=entry_time,
            exit_time=exit_time,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason,
            sl_hit=sl_hit,
            tp_hit=tp_hit,
        )
    )
    logger.info(
        "[JOURNAL] Trade %d closed: %s %s pnl=%.4f reason=%s → análisis disparado",
        trade_id, direction, symbol, pnl_pct, exit_reason
    )


async def _analyze_trade_async(
    trade_id: int,
    symbol: str,
    direction: str,
    entry_time: datetime,
    exit_time: datetime,
    pnl_pct: float,
    exit_reason: str,
    sl_hit: bool,
    tp_hit: bool,
) -> None:
    """Análisis asíncrono del trade con Qwen."""
    async with JOURNAL_LOCK:
        try:
            insight, confidence, regime, quality = await _query_qwen_trade_insight(
                symbol=symbol,
                direction=direction,
                entry_time=entry_time,
                exit_time=exit_time,
                pnl_pct=pnl_pct,
                exit_reason=exit_reason,
                sl_hit=sl_hit,
                tp_hit=tp_hit,
            )

            # Guardar en DB
            pool = get_pool()
            await pool.execute(
                """
                INSERT INTO trade_insights
                    (trade_id, symbol, direction, pnl_pct, exit_reason,
                     qwen_insight, qwen_confidence, regime, quality_score)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                trade_id, symbol, direction, pnl_pct, exit_reason,
                insight, confidence, regime, quality,
            )
            logger.info(
                "[JOURNAL] Insight guardado trade=%d: %s (conf=%.2f)",
                trade_id, insight[:80], confidence
            )
        except Exception as e:
            logger.warning("[JOURNAL] Error análisis trade %d: %s", trade_id, e)


async def _query_qwen_trade_insight(
    symbol: str,
    direction: str,
    entry_time: datetime,
    exit_time: datetime,
    pnl_pct: float,
    exit_reason: str,
    sl_hit: bool,
    tp_hit: bool,
) -> tuple[str, float, str, float]:
    """
    Consulta Qwen para analizar el trade cerrado.

    Retorna: (insight_text, confidence_0_1, regime_at_entry, quality_score)
    """
    # Normalizar entry_time y exit_time a datetime (pueden llegar como float unix)
    if isinstance(entry_time, (int, float)):
        entry_time = datetime.fromtimestamp(entry_time, tz=timezone.utc)
    elif isinstance(entry_time, str):
        try:
            entry_time = datetime.fromtimestamp(float(entry_time), tz=timezone.utc)
        except (ValueError, TypeError):
            entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
    if isinstance(exit_time, (int, float)):
        exit_time = datetime.fromtimestamp(exit_time, tz=timezone.utc)
    elif isinstance(exit_time, str):
        try:
            exit_time = datetime.fromtimestamp(float(exit_time), tz=timezone.utc)
        except (ValueError, TypeError):
            exit_time = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))

    # Calcular duración
    duration = (exit_time - entry_time).total_seconds() / 3600  # horas
    win = "WIN" if pnl_pct > 0 else "LOSS"

    prompt = f"""Analyze this {symbol} trade and identify the key pattern or insight.

Trade Details:
- Direction: {direction}
- Entry time: {entry_time.strftime('%Y-%m-%d %H:%M UTC')}
- Exit time: {exit_time.strftime('%Y-%m-%d %H:%M UTC')}
- Duration: {duration:.1f} hours
- PnL: {pnl_pct:.4%} ({win})
- Exit reason: {exit_reason} (sl={'YES' if sl_hit else 'NO'}, tp={'YES' if tp_hit else 'NO'})

Your task: Identify the most likely reason for this outcome.
Was it:
- "momentum_fade": price reversed after strong move (RSI divergence)
- "trend_continuation": trade aligned with strong trend
- "range_bound": bounded inside range, SL hit at edge
- "volatility_squeeze": squeeze broke wrong direction
- "session_gap": London/NY session shift caused reversal
- "news_shock": macro news caused sharp move
- "timing_error": entered at bad time (late/early)
- "signal_quality_poor": original setup was low quality

Respond EXACTLY JSON (no extra text):
{{"insight": "momentum_fade", "confidence": 0.75, "regime": "RANGING", "quality_score": 3.5}}

confidence: how certain you are (0-1)
regime: market regime at time of entry (TRENDING/RANGING/VOLATILE/BREAKOUT)
quality_score: how good was the original setup (0-10)"""

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(QWEN_URL, json={
                "messages": [
                    {"role": "system", "content": "You are a quantitative trading analyst. Always respond in valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 100,
            })

        if res.status_code == 200:
            content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if "{" in content:
                json_str = content[content.index("{"):]
                parsed = json.loads(json_str)
                insight = str(parsed.get("insight", "unknown"))[:80]
                confidence = float(parsed.get("confidence", 0.5))
                regime = str(parsed.get("regime", "RANGING"))
                quality = float(parsed.get("quality_score", 5.0))
                logger.info(
                    "[JOURNAL] QWEN-INSIGHT ║ SUCCESS ║ %s %s | insight=%s conf=%.2f regime=%s quality=%.1f",
                    direction, symbol, insight, confidence, regime, quality
                )
                return insight, confidence, regime, quality
            else:
                logger.warning(
                    "[JOURNAL] QWEN-INSIGHT ║ PARSE-FAIL ║ %s %s | contenido sin JSON: %s → FALLBACK",
                    direction, symbol, content[:80]
                )
        else:
            logger.warning(
                "[JOURNAL] QWEN-INSIGHT ║ HTTP-%d ║ %s %s → FALLBACK",
                res.status_code, direction, symbol
            )
    except Exception as e:
        logger.warning(
            "[JOURNAL] QWEN-INSIGHT ║ ERROR ║ %s %s | %s → FALLBACK (heurístico)",
            direction, symbol, e
        )

    # Fallback heurístico
    insight = "unknown_analyzed"
    if sl_hit and pnl_pct < 0:
        insight = "range_bound" if abs(pnl_pct) < 0.01 else "momentum_fade"
    elif tp_hit:
        insight = "trend_continuation"
    confidence = 0.4
    regime = "RANGING"
    quality = 5.0
    logger.warning(
        "[JOURNAL] QWEN-INSIGHT ║ FALLBACK ║ %s %s | pnl=%.4f sl_hit=%s tp_hit=%s → insight=%s",
        direction, symbol, pnl_pct, sl_hit, tp_hit, insight
    )
    return insight, confidence, regime, quality


async def get_recent_insights(symbol: str = "EURUSD", limit: int = 10) -> list[dict]:
    """
    Recupera últimos insights de la base de datos para revisión.
    Usado por el dashboard o para análisis humano.
    """
    try:
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT symbol, direction, pnl_pct, exit_reason, qwen_insight,
                   qwen_confidence, regime, quality_score, created_at
            FROM trade_insights
            WHERE symbol = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            symbol, limit
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("[JOURNAL] Error retrieving insights: %s", e)
        return []

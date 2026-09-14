import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx
import numpy as np
import pandas as pd
import torch
import gymnasium as gym
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from config import settings
from db.connection import get_pool
from ml.trading_env_v2 import (
    MARKET_FEATURES,
    build_v3_observation,
    decode_v3_direction,
    engineer_market_features,
    get_market_features,
)
from services.auto_retrain import (
    force_retrain as _force_retrain,
    get_state as _get_retrain_state,
    record_trade_filled,
)
from services.performance_metrics import record_cycle_metrics
from .deps import verify_token


class _DummyEnv(gym.Env):
    def __init__(self, observation_space, action_space):
        super().__init__()
        self.observation_space = observation_space
        self.action_space = action_space

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        return self.observation_space.sample(), {}

    def step(self, action):
        return self.observation_space.sample(), 0.0, False, False, {}


router = APIRouter()
logger = logging.getLogger(__name__)

# Paths de artefactos
MODEL_DIR = os.getenv("PPO_OUTPUT_DIR", "/app/ml")
MODEL_V3_PATH = os.path.join(MODEL_DIR, "ppo_trading_bot_v3.zip")
VEC_NORM_PATH = os.path.join(MODEL_DIR, "ppo_trading_bot_v3_vec_norm.pkl")
QWEN_URL = getattr(settings, "qwen_url", "http://100.90.16.33:8080/v1/chat/completions")

QUALITY_THRESHOLD = 4.0
SL_MIN_NORM = 0.05
SL_MAX_NORM = 0.30
TP_MIN_NORM = 0.05
TP_MAX_NORM = 0.50

# Estado en memoria para inferencia rápida
_CACHED_MODEL: Optional[PPO] = None
_CACHED_VEC_NORM: Optional[VecNormalize] = None


def load_inference_artifacts():
    """Carga en memoria PPO y las estadísticas de normalización una sola vez."""
    global _CACHED_MODEL, _CACHED_VEC_NORM
    if os.path.exists(MODEL_V3_PATH):
        _CACHED_MODEL = PPO.load(MODEL_V3_PATH, device="cpu")
        logger.info("[AI-STARTUP] Modelo PPO v3 cargado en memoria exitosamente.")

        if os.path.exists(VEC_NORM_PATH):
            obs_space = getattr(_CACHED_MODEL, "observation_space", None)
            if obs_space is None:
                raise RuntimeError("Modelo PPO cargado sin observation_space; no se puede inicializar VecNormalize.")
            dummy_env = DummyVecEnv([lambda: _DummyEnv(obs_space, _CACHED_MODEL.action_space)])
            _CACHED_VEC_NORM = VecNormalize.load(VEC_NORM_PATH, dummy_env)
            _CACHED_VEC_NORM.training = False
            _CACHED_VEC_NORM.norm_reward = False
            logger.info("[AI-STARTUP] Estadísticas de VecNormalize cargadas.")
        else:
            _CACHED_VEC_NORM = None
            logger.warning("[AI-STARTUP] Alerta: No se encontró %s. Inferencia sin normalizar.", VEC_NORM_PATH)
    else:
        raise RuntimeError("No se encontró el modelo PPO en %s." % MODEL_V3_PATH)


@router.on_event("startup")
async def on_startup():
    load_inference_artifacts()


def _build_qwen_candle_context(df: pd.DataFrame, count: int = 10) -> str:
    """Compact candle sequence for Qwen: trend information without raw OHLC noise."""
    recent = df.tail(count).copy()
    if recent.empty:
        return "Unavailable"

    close = recent["close"].astype(float)
    previous_close = close.shift(1).fillna(close.iloc[0])
    returns = (close / previous_close - 1.0) * 100.0
    ranges = (recent["high"].astype(float) - recent["low"].astype(float)) / close * 100.0
    bodies = (close - recent["open"].astype(float)).abs() / close * 100.0
    median_volume = recent["tick_volume"].astype(float).median() if "tick_volume" in recent else 0.0

    rows = []
    for index, (_, candle) in enumerate(recent.iterrows()):
        direction = "U" if close.iloc[index] >= float(candle["open"]) else "D"
        volume_ratio = (
            float(candle.get("tick_volume", 0.0)) / median_volume
            if median_volume > 0 else 0.0
        )
        rows.append(
            f"{index + 1}:r={returns.iloc[index]:+.2f}% "
            f"rng={ranges.iloc[index]:.2f}% body={bodies.iloc[index]:.2f}% "
            f"d={direction} v={volume_ratio:.1f}x"
        )

    total_return = (close.iloc[-1] / close.iloc[0] - 1.0) * 100.0
    up_count = int((returns > 0).sum())
    down_count = int((returns < 0).sum())
    return (
        f"last {len(recent)} H1 candles (oldest→newest), "
        f"return={total_return:+.2f}%, up/down={up_count}/{down_count}\n"
        + " | ".join(rows)
    )


async def _query_qwen_unified(
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
) -> dict:
    """
    Opciones 1+2+3 unificadas — una sola llamada a Qwen por candle.

    Retorna:
      - quality_score: 0-10
      - quality_reason: str
      - llm_bias: BULLISH/BEARISH/NEUTRAL
      - confidence_modifier: 0.5-1.5 (Opción 3)
      - sl_adjusted / tp_adjusted: norm values (Opción 2)
      - regime: TRENDING/RANGING/VOLATILE/BREAKOUT (Opción 4 anticipado)
    """
    # Clasificar sesión
    if 7 <= hour < 12:
        session = "London"
    elif 12 <= hour < 17:
        session = "NY"
    elif 17 <= hour < 23:
        session = "Asia"
    else:
        session = "Weekend/Closed"

    # Clasificar régimen
    if abs(bb_pos) > 0.8:
        regime = "TRENDING"
    elif atr_pct > 0.015:
        regime = "VOLATILE"
    elif abs(macd_hist) < 0.0002:
        regime = "RANGING"
    else:
        regime = "BREAKOUT"

    sl_pips = sl_proposed * 100.0
    tp_pips = tp_proposed * 200.0
    news_context = " ".join(str(news).split())[:600] or "Unavailable"

    prompt = f"""Analyze this {symbol} H1 setup. Use the candle sequence as context.

Context:
- RSI(14): {rsi:.1f}
- ATR: {atr:.5f} ({atr_pct:.2%} of price)
- MACD histogram: {macd_hist:.6f}
- Bollinger position: {bb_pos:.2f}
- Range: {range_pct:.2%} of price
- Last return: {last_ret:.3%}
- Session: {session}
- Regime: {regime}
- Candles: {candle_context}

News:
{news_context}

ML Signal: {decision} with ML confidence {ml_prob:.3f}

Return EXACTLY JSON, no extra text:
{{"quality": 7.5, "reason": "brief reason", "bias": "NEUTRAL", "confidence_modifier": 1.0,
  "sl_ok": true, "tp_ok": true, "sl_adjusted": {sl_proposed:.4f}, "tp_adjusted": {tp_proposed:.4f},
  "regime": "{regime}"}}

Fields: quality 0-10; reason max 12 words; bias BULLISH/BEARISH/NEUTRAL;
confidence_modifier 0.5-1.5; sl_ok/tp_ok booleans; adjusted SL 0.15-0.30,
TP 0.125-0.30 with TP:SL >=1.5; regime TRENDING/RANGING/VOLATILE/BREAKOUT."""

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

    if res.status_code == 200:
        content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if "{" in content:
            json_str = content[content.index("{"):]
            parsed, _ = json.JSONDecoder().raw_decode(json_str)
            q = float(parsed.get("quality", 5.0))
            reason = str(parsed.get("reason", ""))[:120]
            bias_raw = str(parsed.get("bias", "NEUTRAL")).upper()
            bias = "NEUTRAL"
            if "BULL" in bias_raw: bias = "BULLISH"
            elif "BEAR" in bias_raw: bias = "BEARISH"

            conf_mod = float(parsed.get("confidence_modifier", 1.0))
            conf_mod = max(0.5, min(1.5, conf_mod))

            sl_adj = float(parsed.get("sl_adjusted", sl_proposed))
            tp_adj = float(parsed.get("tp_adjusted", tp_proposed))
            sl_adj = max(SL_MIN_NORM, min(SL_MAX_NORM, sl_adj))
            tp_adj = max(TP_MIN_NORM, min(TP_MAX_NORM, tp_adj))

            tp_adj = sl_adj

            effective_prob = float(np.exp(np.log(max(ml_prob, 1e-6)) + np.log(conf_mod)))
            effective_prob = max(0.0, min(1.0, effective_prob))

            logger.info(
                "[%s] QWEN-UNIFIED ║ q=%.1f bias=%s conf_mod=%.2f eff_prob=%.4f | "
                "sl=%.3f→%.3f tp=%.3f→%.3f regime=%s | %s",
                cycle_id, q, bias, conf_mod, effective_prob,
                sl_proposed, sl_adj, tp_proposed, tp_adj,
                parsed.get("regime", regime), reason[:60]
            )
            return {
                "quality": q,
                "reason": reason,
                "bias": bias,
                "confidence_modifier": conf_mod,
                "effective_prob": effective_prob,
                "sl_adjusted": sl_adj,
                "tp_adjusted": tp_adj,
                "regime": parsed.get("regime", regime),
            }
        raise RuntimeError(f"[{cycle_id}] QWEN-UNIFIED respuesta sin JSON: {content[:80]}")
    res.raise_for_status()
    raise RuntimeError(f"[{cycle_id}] QWEN-UNIFIED HTTP {res.status_code}: {res.text[:120]}")


# ── Opción 2: SL/TP Validator ─────────────────────────────────────────────────
SL_MIN_NORM = 0.15   # 15 pips minimum
SL_MAX_NORM = 0.30   # 30 pips maximum
TP_MIN_NORM = 0.125  # 25 pips minimum (SL*0.5 para ratio 2:1 mínimo)
TP_MAX_NORM = 0.30   # 60 pips maximum


async def _query_qwen_sltp_validator(
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
) -> tuple[float, float, str]:
    """
    Opción 2 — SL/TP Validator:
    Qwen analiza si el SL y TP propuestos son razonables para el régimen
    de mercado actual (volatilidad, sesión, momentum).

    Retorna: (sl_norm_final, tp_norm_final, reason)
    """
    if abs(bb_pos) > 0.8:
        regime = "TRENDING"
    elif atr_pct > 0.015:
        regime = "VOLATILE"
    elif abs(macd_hist) < 0.0002:
        regime = "RANGING"
    else:
        regime = "BREAKOUT"

    if 7 <= hour < 12:
        session = "London"
    elif 12 <= hour < 17:
        session = "NY"
    else:
        session = "Asia"

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

    if res.status_code == 200:
        content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if "{" in content:
            json_str = content[content.index("{"):]
            parsed, _ = json.JSONDecoder().raw_decode(json_str)
            sl_ok = bool(parsed.get("sl_ok", True))
            tp_ok = bool(parsed.get("tp_ok", True))
            sl_adj = float(parsed.get("sl_adjusted", sl_proposed))
            tp_adj = float(parsed.get("tp_adjusted", tp_proposed))

            sl_adj = max(SL_MIN_NORM, min(SL_MAX_NORM, sl_adj))
            tp_adj = max(TP_MIN_NORM, min(TP_MAX_NORM, tp_adj))
            tp_adj = sl_adj

            if not sl_ok or not tp_ok:
                logger.warning(
                    "[%s] SLTP-REJECTED ║ sl_ok=%s tp_ok=%s → adjusted sl=%.4f tp=%.4f | reason=%s",
                    cycle_id, sl_ok, tp_ok, sl_adj, tp_adj,
                    parsed.get("reason", "")[:80]
                )
            return sl_adj, tp_adj, parsed.get("reason", "")[:100]
        raise RuntimeError(f"[{cycle_id}] SLTP-VALIDATOR respuesta sin JSON: {content[:80]}")
    res.raise_for_status()
    raise RuntimeError(f"[{cycle_id}] SLTP-VALIDATOR HTTP {res.status_code}: {res.text[:120]}")


async def _get_equity_estimate() -> float:
    """Equity aproximado desde MT5 para logging."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{settings.mt5_http_url}/api/v1/account/info", timeout=2.0)
        if r.status_code == 200:
            return float(r.json().get("equity", 0.0))
    return 0.0


def _build_qwen_candle_context(df: pd.DataFrame, count: int = 10) -> str:
    """Compact candle sequence for Qwen: trend information without raw OHLC noise."""
    recent = df.tail(count).copy()
    if recent.empty:
        return "Unavailable"

    close = recent["close"].astype(float)
    previous_close = close.shift(1).fillna(close.iloc[0])
    returns = (close / previous_close - 1.0) * 100.0
    ranges = (recent["high"].astype(float) - recent["low"].astype(float)) / close * 100.0
    bodies = (close - recent["open"].astype(float)).abs() / close * 100.0
    median_volume = recent["tick_volume"].astype(float).median() if "tick_volume" in recent else 0.0

    rows = []
    for index, (_, candle) in enumerate(recent.iterrows()):
        direction = "U" if close.iloc[index] >= float(candle["open"]) else "D"
        volume_ratio = (
            float(candle.get("tick_volume", 0.0)) / median_volume
            if median_volume > 0 else 0.0
        )
        rows.append(
            f"{index + 1}:r={returns.iloc[index]:+.2f}% "
            f"rng={ranges.iloc[index]:.2f}% body={bodies.iloc[index]:.2f}% "
            f"d={direction} v={volume_ratio:.1f}x"
        )

    total_return = (close.iloc[-1] / close.iloc[0] - 1.0) * 100.0
    up_count = int((returns > 0).sum())
    down_count = int((returns < 0).sum())
    return (
        f"last {len(recent)} H1 candles (oldest→newest), "
        f"return={total_return:+.2f}%, up/down={up_count}/{down_count}\n"
        + " | ".join(rows)
    )


# ── Helper: consultar Qwen — Opciones 1, 2, 3 unificadas ────────────────────
async def _query_qwen_unified(
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
    logger,
) -> dict:
    """
    Opciones 1+2+3 unificadas — una sola llamada a Qwen por candle.

    Retorna:
      - quality_score: 0-10
      - quality_reason: str
      - llm_bias: BULLISH/BEARISH/NEUTRAL
      - confidence_modifier: 0.5-1.5 (Opción 3)
      - sl_adjusted / tp_adjusted: norm values (Opción 2)
      - regime: TRENDING/RANGING/VOLATILE/BREAKOUT (Opción 4 anticipado)
    """
    # Clasificar sesión
    if 7 <= hour < 12:
        session = "London"
    elif 12 <= hour < 17:
        session = "NY"
    elif 17 <= hour < 23:
        session = "Asia"
    else:
        session = "Weekend/Closed"

    # Clasificar régimen
    if abs(bb_pos) > 0.8:
        regime = "TRENDING"
    elif atr_pct > 0.015:
        regime = "VOLATILE"
    elif abs(macd_hist) < 0.0002:
        regime = "RANGING"
    else:
        regime = "BREAKOUT"

    sl_pips = sl_proposed * 100.0
    tp_pips = tp_proposed * 200.0
    news_context = " ".join(str(news).split())[:600] or "Unavailable"

    prompt = f"""Analyze this {symbol} H1 setup. Use the candle sequence as context.

Context:
- RSI(14): {rsi:.1f}
- ATR: {atr:.5f} ({atr_pct:.2%} of price)
- MACD histogram: {macd_hist:.6f}
- Bollinger position: {bb_pos:.2f}
- Range: {range_pct:.2%} of price
- Last return: {last_ret:.3%}
- Session: {session}
- Regime: {regime}
- Candles: {candle_context}

News:
{news_context}

ML Signal: {decision} with ML confidence {ml_prob:.3f}

Return EXACTLY JSON, no extra text:
{{"quality": 7.5, "reason": "brief reason", "bias": "NEUTRAL", "confidence_modifier": 1.0,
  "sl_ok": true, "tp_ok": true, "sl_adjusted": {sl_proposed:.4f}, "tp_adjusted": {tp_proposed:.4f},
  "regime": "{regime}"}}

Fields: quality 0-10; reason max 12 words; bias BULLISH/BEARISH/NEUTRAL;
confidence_modifier 0.5-1.5; sl_ok/tp_ok booleans; adjusted SL 0.15-0.30,
TP 0.125-0.30 with TP:SL >=1.5; regime TRENDING/RANGING/VOLATILE/BREAKOUT."""

    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(QWEN_URL, json={
            "messages": [
                {"role": "system", "content": "You are a quantitative trading analyst. Always respond in valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "max_tokens": 160,
            "response_format": {"type": "json_object"},  # Forzar JSON válido
        })

    if res.status_code == 200:
        content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if "{" in content:
            json_str = content[content.index("{"):]
            parsed, _ = json.JSONDecoder().raw_decode(json_str)
            q = float(parsed.get("quality", 5.0))
            reason = str(parsed.get("reason", ""))[:120]
            bias_raw = str(parsed.get("bias", "NEUTRAL")).upper()
            bias = "NEUTRAL"
            if "BULL" in bias_raw: bias = "BULLISH"
            elif "BEAR" in bias_raw: bias = "BEARISH"

            conf_mod = float(parsed.get("confidence_modifier", 1.0))
            conf_mod = max(0.5, min(1.5, conf_mod))

            sl_adj = float(parsed.get("sl_adjusted", sl_proposed))
            tp_adj = float(parsed.get("tp_adjusted", tp_proposed))
            sl_adj = max(SL_MIN_NORM, min(SL_MAX_NORM, sl_adj))
            tp_adj = max(TP_MIN_NORM, min(TP_MAX_NORM, tp_adj))

            tp_adj = sl_adj

            effective_prob = float(np.exp(np.log(max(ml_prob, 1e-6)) + np.log(conf_mod)))
            effective_prob = max(0.0, min(1.0, effective_prob))

            logger.info(
                "[%s] QWEN-UNIFIED ║ q=%.1f bias=%s conf_mod=%.2f eff_prob=%.4f | "
                "sl=%.3f→%.3f tp=%.3f→%.3f regime=%s | %s",
                cycle_id, q, bias, conf_mod, effective_prob,
                sl_proposed, sl_adj, tp_proposed, tp_adj,
                parsed.get("regime", regime), reason[:60]
            )
            return {
                "quality": q,
                "reason": reason,
                "bias": bias,
                "confidence_modifier": conf_mod,
                "effective_prob": effective_prob,
                "sl_adjusted": sl_adj,
                "tp_adjusted": tp_adj,
                "regime": parsed.get("regime", regime),
            }
        raise RuntimeError(f"[{cycle_id}] QWEN-UNIFIED respuesta sin JSON: {content[:80]}")
    res.raise_for_status()
    raise RuntimeError(f"[{cycle_id}] QWEN-UNIFIED HTTP {res.status_code}: {res.text[:120]}")


# ── Opción 2: SL/TP Validator ─────────────────────────────────────────────────
SL_MIN_NORM = 0.15   # 15 pips minimum
SL_MAX_NORM = 0.30   # 30 pips maximum
TP_MIN_NORM = 0.125  # 25 pips minimum (SL*0.5 para ratio 2:1 mínimo)
TP_MAX_NORM = 0.30   # 60 pips maximum

async def _query_qwen_sltp_validator(
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
    logger,
) -> tuple[float, float, str]:
    """
    Opción 2 — SL/TP Validator:
    Qwen analiza si el SL y TP propuestos son razonables para el régimen
    de mercado actual (volatilidad, sesión, momentum).

    Retorna: (sl_norm_final, tp_norm_final, reason)
    """
    # Clasificar régimen según contexto
    if abs(bb_pos) > 0.8:
        regime = "TRENDING"
    elif atr_pct > 0.015:
        regime = "VOLATILE"
    elif abs(macd_hist) < 0.0002:
        regime = "RANGING"
    else:
        regime = "BREAKOUT"

    if 7 <= hour < 12:
        session = "London"
    elif 12 <= hour < 17:
        session = "NY"
    else:
        session = "Asia"

    sl_pips = sl_proposed * 100.0  # desproteger para display
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

    if res.status_code == 200:
        content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if "{" in content:
            json_str = content[content.index("{"):]
            parsed, _ = json.JSONDecoder().raw_decode(json_str)
            sl_ok = bool(parsed.get("sl_ok", True))
            tp_ok = bool(parsed.get("tp_ok", True))
            sl_adj = float(parsed.get("sl_adjusted", sl_proposed))
            tp_adj = float(parsed.get("tp_adjusted", tp_proposed))

            sl_adj = max(SL_MIN_NORM, min(SL_MAX_NORM, sl_adj))
            tp_adj = max(TP_MIN_NORM, min(TP_MAX_NORM, tp_adj))
            tp_adj = sl_adj

            if not sl_ok or not tp_ok:
                logger.warning(
                    "[%s] SLTP-REJECTED ║ sl_ok=%s tp_ok=%s → adjusted sl=%.4f tp=%.4f | reason=%s",
                    cycle_id, sl_ok, tp_ok, sl_adj, tp_adj,
                    parsed.get("reason", "")[:80]
                )
            return sl_adj, tp_adj, parsed.get("reason", "")[:100]
        raise RuntimeError(f"[{cycle_id}] SLTP-VALIDATOR respuesta sin JSON: {content[:80]}")
    res.raise_for_status()
    raise RuntimeError(f"[{cycle_id}] SLTP-VALIDATOR HTTP {res.status_code}: {res.text[:120]}")

    raise RuntimeError(f"[{cycle_id}] SLTP-VALIDATOR respuesta sin JSON: {content[:80]}")


# ── Helper: estimar equity desde MT5 ──────────────────────────────────────────
async def _get_equity_estimate() -> float:
    """Equity aproximado desde MT5 para logging."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{settings.mt5_http_url}/api/v1/account/info", timeout=2.0)
        if r.status_code == 200:
            return float(r.json().get("equity", 0.0))
    return 0.0

class PredictRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    position: int = 0         # 0=Flat, 1=Long, 2=Short (se remapea internamente)
    entry_price: float = 0.0  # Precio real de entrada si la posición está abierta
    sl_price: float = 0.0
    tp_price: float = 0.0
    steps_in_trade: int = 0


class PredictResponse(BaseModel):
    ml_prob: float
    llm_bias: str
    decision: str
    volume: Optional[float] = None
    sl_pips: Optional[float] = None
    tp_pips: Optional[float] = None
    model_version: Optional[str] = None
    quality_score: Optional[float] = None
    quality_reason: Optional[str] = None
    confidence_modifier: Optional[float] = None
    effective_prob: Optional[float] = None
    regime: Optional[str] = None


@router.post("/predict", response_model=PredictResponse)
async def predict_direction(req: PredictRequest, _: None = Depends(verify_token)):
    t_start = time.time()
    cycle_id = f"{req.symbol}_{int(t_start * 1000)}"

    env_position = 1 if req.position == 1 else (-1 if req.position == 2 else 0)

    async with httpx.AsyncClient() as client:
        equity_task = client.get(
            f"{settings.mt5_http_url}/api/v1/account/info",
            timeout=2.0,
        )
        candles_task = client.get(
            f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe={req.timeframe}&count=120",
            timeout=3.0,
        )
        equity_res, candles_res = await asyncio.gather(equity_task, candles_task)

    equity_val = 0.0
    if equity_res.status_code == 200:
        equity_val = equity_res.json().get("equity", 0.0)

    if candles_res.status_code != 200:
        raise HTTPException(status_code=502, detail="Error de conexión con servicio MT5")
    data = candles_res.json()
    candles = data if isinstance(data, list) else data.get("candles", [])

    raw_df = pd.DataFrame(candles)
    if raw_df.empty or len(raw_df) < 30:
        return PredictResponse(ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    df = engineer_market_features(raw_df)
    features_list = get_market_features(df)
    candle_context = _build_qwen_candle_context(raw_df, count=10)

    if _CACHED_MODEL is None:
        load_inference_artifacts()
    if _CACHED_MODEL is None:
        logger.error("[%s] No hay modelo PPO disponible.", cycle_id)
        return PredictResponse(ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    current_price = float(df['close'].iloc[-1])
    current_atr = float(df['atr'].iloc[-1]) if 'atr' in df.columns else 0.0001
    entry_p = req.entry_price if env_position != 0 and req.entry_price > 0 else current_price

    digits = 5 if not req.symbol.upper().endswith("JPY") and len(str(current_price).split(".")[1]) >= 4 else 2
    pip_size = 0.0001 if digits == 5 else 0.01
    atr_pips = current_atr / pip_size

    sl_pips = max(10.0, min(50.0, atr_pips * 1.5))
    tp_pips = sl_pips * 2.0
    sl_distance_price = sl_pips * pip_size

    if env_position != 0 and req.entry_price > 0:
        entry_p = req.entry_price
        sl_p = req.sl_price if req.sl_price > 0 else (entry_p - sl_distance_price if env_position == 1 else entry_p + sl_distance_price)
        tp_p = req.tp_price if req.tp_price > 0 else (entry_p + tp_pips * pip_size if env_position == 1 else entry_p - tp_pips * pip_size)
    else:
        sl_p = entry_p - sl_distance_price if env_position == 1 else entry_p + sl_distance_price
        tp_p = entry_p + tp_pips * pip_size if env_position == 1 else entry_p - tp_pips * pip_size

    market_slice = df[features_list].iloc[-20:].values
    raw_obs = build_v3_observation(
        market_values=market_slice,
        expected_market_features=len(features_list),
        position=env_position,
        entry_price=entry_p,
        current_price=current_price,
        sl_price=sl_p,
        tp_price=tp_p,
        current_atr=current_atr,
        steps_in_trade=req.steps_in_trade,
        max_holding_steps=120,
        balance=equity_val,
        initial_balance=10000.0,
        window_size=20,
    )

    if _CACHED_VEC_NORM is not None:
        norm_obs = _CACHED_VEC_NORM.normalize_obs(raw_obs)
    else:
        norm_obs = raw_obs

    action, _ = _CACHED_MODEL.predict(norm_obs, deterministic=True)
    raw_action = np.asarray(action, dtype=np.float32).reshape(-1)
    direction, volume_norm, sl_norm, tp_norm = raw_action

    target_pos, decision = decode_v3_direction(float(direction), env_position)
    ml_prob = float(np.clip(abs(direction), 0.1, 0.99))

    max_lot_allowed = 0.5
    vol_calculated = float(volume_norm) * max_lot_allowed
    max_risk_usd = equity_val * 0.03
    max_lot_by_risk = max_risk_usd / (sl_distance_price * 100000.0 + 1e-8)

    # Calcular el lote mínimo necesario para ganar ~3€ netos en el TP,
    # teniendo en cuenta el spread y comisiones implícitas.
    min_profit_target = 3.0
    spread_price = 3.0 * pip_size  # margen conservador por spread/comisiones
    net_tp_pips = max(1.0, tp_pips - 2.0)  # restar ~2 pips de spread/comisión
    min_lot_for_profit = min_profit_target / (net_tp_pips * 0.1 + 1e-8)

    candidate_lots = max(min_lot_for_profit, vol_calculated)
    final_lots = float(np.clip(candidate_lots, 0.01, min(max_lot_allowed, max_lot_by_risk)))
    final_lots = round(round(final_lots / 0.01) * 0.01, 2)

    sl_pips_final = sl_pips
    tp_pips_final = tp_pips

    live_news = "Macro data unavailable"
    from services.news_scraper import get_macro_news
    live_news = await get_macro_news(req.symbol)

    qwen = await _query_qwen_unified(
        symbol=req.symbol,
        decision=decision,
        ml_prob=ml_prob,
        rsi=float(df['rsi14'].iloc[-1]) * 100.0,
        atr=current_atr,
        atr_pct=current_atr / current_price,
        macd_hist=float(df['macd_hist'].iloc[-1]),
        bb_pos=float(df['bb_pos'].iloc[-1]),
        range_pct=float(df['range'].iloc[-1]),
        last_ret=float(df['returns'].iloc[-1]),
        hour=int(pd.to_datetime(raw_df['time'].iloc[-1]).hour),
        news=live_news,
        candle_context=candle_context,
        sl_proposed=sl_pips_final / 100.0,
        tp_proposed=tp_pips_final / 200.0,
        cycle_id=cycle_id,
        logger=logger,
    )

    final_decision = decision
    if qwen["quality"] < QUALITY_THRESHOLD and final_decision in ("BUY", "SELL"):
        logger.warning("[%s] VETO CALIDAD ║ Score %.1f < %.1f → HOLD", cycle_id, qwen["quality"], QUALITY_THRESHOLD)
        final_decision = "HOLD"
    elif final_decision == "BUY" and qwen["bias"] == "BEARISH":
        logger.warning("[%s] VETO BIAS ║ BUY revertido por LLM BEARISH", cycle_id)
        final_decision = "HOLD"
    elif final_decision == "SELL" and qwen["bias"] == "BULLISH":
        logger.warning("[%s] VETO BIAS ║ SELL revertido por LLM BULLISH", cycle_id)
        final_decision = "HOLD"

    record_cycle_metrics(
        symbol=req.symbol,
        decision=final_decision,
        ml_prob=qwen["effective_prob"],
        llm_bias=qwen["bias"],
        latency_ms=(time.time() - t_start) * 1000,
        mt5_available=True,
        order_placed=final_decision in ("BUY", "SELL"),
    )

    return PredictResponse(
        ml_prob=round(ml_prob, 4),
        llm_bias=qwen["bias"],
        decision=final_decision,
        volume=final_lots if final_decision in ("BUY", "SELL") else None,
        sl_pips=round(sl_pips_final, 1) if final_decision in ("BUY", "SELL") else None,
        tp_pips=round(tp_pips_final, 1) if final_decision in ("BUY", "SELL") else None,
        model_version="v3",
        quality_score=round(qwen["quality"], 2),
        quality_reason=qwen["reason"],
        confidence_modifier=round(qwen["confidence_modifier"], 2),
        effective_prob=round(qwen["effective_prob"], 4),
        regime=qwen["regime"],
    )


@router.post("/retrain/force")
async def force_retrain_endpoint(_: None = Depends(verify_token)):
    """
    Fuerza un retrain inmediato del modelo PPO.
    Útil para when market regime cambia (e.g., central bank intervention).
    """
    state = _get_retrain_state()
    result = await _force_retrain()
    return {"status": result["status"], "filled_count": state.filled_count, "in_progress": state.retrain_in_progress}


@router.get("/retrain/status")
async def retrain_status(_: None = Depends(verify_token)):
    """Estado actual del auto-retrain."""
    state = _get_retrain_state()
    return {
        "filled_count": state.filled_count,
        "retrain_in_progress": state.retrain_in_progress,
        "last_retrain_time": state.last_retrain_time,
        "outcomes_count": len(state.outcomes),
    }


# ─── Pipeline Health ──────────────────────────────────────────────────────────
# Health check integral del sistema — verifica que todo el pipeline funcione.


class HealthCheckResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "critical"
    components: dict  # {name: {"ok": bool, "error": str|null}}


@router.get("/pipeline/health", response_model=HealthCheckResponse)
async def pipeline_health() -> HealthCheckResponse:
    """
    Verifica la salud de todo el sistema de trading.
    Cada componente que falle se reporta explícitamente.
    """
    from services.alerting import send_alert, AlertLevel

    components = {}
    issues = []

    # 1. Database
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    trade_count = await pool.fetchval("SELECT COUNT(*) FROM trade_outcomes")
    components["database"] = {"ok": True, "trade_count": trade_count, "error": None}

    # 2. MT5 connectivity
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{settings.mt5_http_url}/api/v1/account/info", timeout=5.0)
        mt5_ok = r.status_code == 200
    components["mt5"] = {"ok": mt5_ok, "error": None if mt5_ok else f"HTTP {r.status_code}"}
    if not mt5_ok:
        raise RuntimeError(f"MT5 connectivity failed: HTTP {r.status_code}")

    # 3. PPO model file exists
    MODEL_PATH = "/app/ml/ppo_trading_bot_v3.zip"
    model_exists = os.path.exists(MODEL_PATH)
    components["ppo_model"] = {"ok": model_exists, "error": None if model_exists else "Model file not found"}
    if not model_exists:
        raise RuntimeError("PPO model missing at /app/ml/ppo_trading_bot_v3.zip")

    # 4. Retrain state
    state = _get_retrain_state()
    components["retrain"] = {
        "ok": True,
        "filled_count": state.filled_count,
        "in_progress": state.retrain_in_progress,
        "last_retrain": state.last_retrain_time,
        "error": None,
    }
    if state.filled_count == 0:
        issues.append("No trades recorded yet (EA may not be sending webhooks)")

    # Determine overall status
    critical_failures = sum(1 for c in components.values() if not c["ok"])
    if critical_failures > 0:
        status = "critical" if any(c == "database" or c == "ppo_model" for c in components) else "degraded"
    else:
        status = "healthy"

    if status != "healthy":
        send_alert(
            AlertLevel.ERROR,
            "PIPELINE-HEALTH",
            f"Pipeline status: {status}. Issues: {'; '.join(issues)}",
            context={"components": components, "status": status},
        )

    return HealthCheckResponse(status=status, components=components)


# ─── Trade Completion Webhook ────────────────────────────────────────────────
# Llamado por el MT5 EA cuando una orden se cierra (filled).


class TradeFilledRequest(BaseModel):
    symbol: str
    entry_time: float | str  # Unix timestamp (float) or ISO string
    exit_time: float | str   # Unix timestamp (float) or ISO string
    pnl: float
    pnl_pct: float
    direction: str   # "LONG" or "SHORT"
    sl_hit: bool
    tp_hit: bool
    exit_reason: str  # "sl", "tp", "manual", "news"
    # Fase 1 — observable contract: identificadores del deal en MT5
    deal_ticket: int | None = None
    position_id: int | None = None


@router.post("/trade/filled")
async def trade_filled_webhook(req: TradeFilledRequest, _: None = Depends(verify_token)):
    """
    Webhook que el MT5 EA llama cuando una orden se cierra.
    Registra el trade y dispara auto-retrain si corresponde.
    """
    from services.auto_retrain import _parse_timestamp
    entry_ts = _parse_timestamp(req.entry_time)
    exit_ts = _parse_timestamp(req.exit_time)
    minimum_timestamp = 946684800.0  # 2000-01-01 UTC
    if entry_ts < minimum_timestamp or exit_ts < minimum_timestamp:
        raise HTTPException(status_code=422, detail="entry_time/exit_time must be >= 2000-01-01 UTC")
    if exit_ts <= entry_ts:
        raise HTTPException(status_code=422, detail="exit_time must be greater than entry_time")

    logger.info("[TRADE-FILLED] payload symbol=%s entry=%s exit=%s pnl=%.2f pct=%.4f dir=%s reason=%s",
                req.symbol, req.entry_time, req.exit_time, req.pnl, req.pnl_pct, req.direction, req.exit_reason)
    # Normalize direction to uppercase to match DB constraint (LONG/SHORT)
    direction = req.direction.upper() if req.direction else req.direction
    await record_trade_filled(
        symbol=req.symbol,
        entry_time=req.entry_time,
        exit_time=req.exit_time,
        pnl=req.pnl,
        pnl_pct=req.pnl_pct,
        direction=direction,
        sl_hit=req.sl_hit,
        tp_hit=req.tp_hit,
        exit_reason=req.exit_reason,
    )
    return {"status": "recorded"}

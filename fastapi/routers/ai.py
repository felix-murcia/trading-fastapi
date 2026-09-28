import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import numpy as np
import pandas as pd
import torch
import gymnasium as gym
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError
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
    approve_retrain as _approve_retrain,
    force_retrain as _force_retrain,
    get_state as _get_retrain_state,
    record_trade_filled,
)
from services.performance_metrics import record_cycle_metrics
from services.qwen_client import query_qwen_unified, query_qwen_sltp_validator
from services.structured_logging import log_to_audit
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
QWEN_URL = getattr(settings, "qwen_url", "http://100.90.16.33:8080/v1/chat/completions")

QUALITY_THRESHOLD = 5.0
RANGING_VETO = True
SL_MIN_NORM = 0.05
SL_MAX_NORM = 0.30
TP_MIN_NORM = 0.05
TP_MAX_NORM = 0.50


def _veto_reason(original: str, final: str, qwen: dict) -> str | None:
    if original == final:
        return None
    if final == "HOLD" and qwen.get("regime") == "RANGING":
        return "ranging_veto"
    if final == "HOLD" and qwen.get("quality", 10) < QUALITY_THRESHOLD:
        return "quality_veto"
    if original == "BUY" and final == "HOLD" and qwen.get("bias") == "BEARISH":
        return "bias_veto_bearish"
    if original == "SELL" and final == "HOLD" and qwen.get("bias") == "BULLISH":
        return "bias_veto_bullish"
    return "unknown_veto"


# Estado en memoria para inferencia rápida
_CACHED_MODEL: Optional[PPO] = None
_CACHED_VEC_NORM: Optional[VecNormalize] = None
_CACHED_BUNDLE: dict = {}   # P1-2: procedencia del bundle EN MEMORIA (sha, source, loaded_at)


def load_inference_artifacts():
    """Carga PPO y VecNormalize una sola vez, validando el bundle contra su manifest (P0-5).

    Cualquier incoherencia (manifest ausente, hash distinto, obs_shape distinto)
    lanza RuntimeError y detiene el arranque: nunca se sirve un binario sin
    certificar ni se degrada en silencio.

    El swap de la caché es ATÓMICO (se asigna al final): si la validación o la
    carga de VecNormalize fallan, se mantiene íntegro el bundle anterior — nunca
    un estado mitad nuevo, mitad viejo.
    """
    global _CACHED_MODEL, _CACHED_VEC_NORM, _CACHED_BUNDLE
    from services.model_registry import assert_obs_shape, validate_bundle

    manifest = validate_bundle(MODEL_V3_PATH)
    model = PPO.load(MODEL_V3_PATH, device="cpu")
    logger.info("[AI-STARTUP] Modelo PPO v3 cargado en memoria exitosamente.")

    obs_space = getattr(model, "observation_space", None)
    assert_obs_shape(manifest, getattr(obs_space, "shape", None))

    vec_norm_file = manifest.get("vec_norm_file")
    if vec_norm_file:
        vec_path = os.path.join(os.path.dirname(MODEL_V3_PATH), vec_norm_file)
        dummy_env = DummyVecEnv([lambda: _DummyEnv(obs_space, model.action_space)])
        vec_norm = VecNormalize.load(vec_path, dummy_env)
        vec_norm.training = False
        vec_norm.norm_reward = False
        logger.info("[AI-STARTUP] Estadísticas de VecNormalize cargadas (hash verificado).")
    else:
        vec_norm = None
        logger.warning(
            "[AI-STARTUP] Bundle certificado SIN normalización — inferencia en crudo (declarado en manifest)."
        )

    # Swap atómico al final: solo aquí deja de valer el bundle anterior.
    _CACHED_MODEL = model
    _CACHED_VEC_NORM = vec_norm
    _CACHED_BUNDLE = {
        "model_sha256": manifest["model_sha256"],
        "vec_norm_sha256": manifest.get("vec_norm_sha256"),
        "source": manifest["source"],
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }


def reload_inference_artifacts() -> dict:
    """P1-2: hot-reload del bundle tras una promoción, sin reiniciar el proceso.

    Valida el bundle nuevo ANTES de tocar la caché: si falla, la excepción se
    propaga (el done-callback del retrain emite alerta) y se sigue sirviendo el
    bundle anterior — nunca un swap parcial ni degradación silenciosa.
    """
    load_inference_artifacts()
    logger.warning(
        "[AI-RELOAD] Bundle recargado en memoria: source=%s sha=%s…",
        _CACHED_BUNDLE.get("source"),
        str(_CACHED_BUNDLE.get("model_sha256"))[:12],
    )
    return dict(_CACHED_BUNDLE)


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



# Qwen functions moved to services/qwen_client.py



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
    mt5_time: float | None = None  # epoch desde MT5; si falta, se usa time.time() como fallback


class PredictResponse(BaseModel):
    cycle_id: str
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
    mt5_ts = req.mt5_time if req.mt5_time is not None else t_start
    cycle_id = f"{req.symbol}_{int(mt5_ts * 1000)}"

    env_position = 1 if req.position == 1 else (-1 if req.position == 2 else 0)

    equity_val = 0.0
    candles = []
    try:
        async with httpx.AsyncClient() as client:
            equity_task = client.get(
                f"{settings.mt5_http_url}/api/v1/account/info",
                timeout=2.0,
            )
            candles_task = client.get(
                f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe=H1&count=120",
                timeout=3.0,
            )
            equity_res, candles_res = await asyncio.gather(equity_task, candles_task)
    except (httpx.ConnectTimeout, httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("[%s] MT5 connection error in predict: %s", cycle_id, exc)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    if equity_res.status_code == 200:
        equity_val = equity_res.json().get("equity", 0.0)

    if candles_res.status_code != 200:
        logger.warning("[%s] candles HTTP %s from MT5", cycle_id, candles_res.status_code)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
    data = candles_res.json()
    candles = data if isinstance(data, list) else data.get("candles", [])

    raw_df = pd.DataFrame(candles)
    if raw_df.empty or len(raw_df) < 30:
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    df = engineer_market_features(raw_df)
    features_list = get_market_features(df)
    candle_context = _build_qwen_candle_context(raw_df, count=10)

    if _CACHED_MODEL is None:
        load_inference_artifacts()
    if _CACHED_MODEL is None:
        logger.error("[%s] No hay modelo PPO disponible.", cycle_id)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

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

    from services.trade_journal import get_recent_insights
    try:
        recent_insights = await get_recent_insights(req.symbol, limit=5)
    except Exception as e:
        logger.warning("[%s] get_recent_insights falló: %s", cycle_id, e)
        recent_insights = []

    qwen = await query_qwen_unified(
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
        recent_insights=recent_insights,
    )

    final_decision = decision
    if RANGING_VETO and qwen.get("regime") == "RANGING" and final_decision in ("BUY", "SELL"):
        if qwen.get("quality", 0) < QUALITY_THRESHOLD or qwen.get("effective_prob", 0) < 0.35:
            logger.warning("[%s] VETO RANGING ║ Régimen lateral + calidad/prob baja → HOLD", cycle_id)
            final_decision = "HOLD"
    elif qwen["quality"] < QUALITY_THRESHOLD and final_decision in ("BUY", "SELL"):
        logger.warning("[%s] VETO CALIDAD ║ Score %.1f < %.1f → HOLD", cycle_id, qwen["quality"], QUALITY_THRESHOLD)
        final_decision = "HOLD"
    elif final_decision == "BUY" and qwen["bias"] == "BEARISH" and qwen.get("confidence", 1.0) >= 0.7:
        logger.warning("[%s] VETO BIAS ║ BUY revertido por LLM BEARISH (conf=%.2f)", cycle_id, qwen.get("confidence", 1.0))
        final_decision = "HOLD"
    elif final_decision == "SELL" and qwen["bias"] == "BULLISH" and qwen.get("confidence", 1.0) >= 0.7:
        logger.warning("[%s] VETO BIAS ║ SELL revertido por LLM BULLISH (conf=%.2f)", cycle_id, qwen.get("confidence", 1.0))
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

    await log_to_audit(
        "ai_predict_cycle",
        {
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "position": req.position,
            "decision": final_decision,
            "ml_prob": round(ml_prob, 4),
            "effective_prob": round(qwen["effective_prob"], 4),
            "llm_bias": qwen["bias"],
            "quality_score": round(qwen["quality"], 2),
            "quality_reason": qwen["reason"],
            "qwen_degraded": not qwen.get("ok", True),
            "confidence_modifier": round(qwen["confidence_modifier"], 2),
            "regime": qwen["regime"],
            "volume": final_lots if final_decision in ("BUY", "SELL") else None,
            "sl_pips": round(sl_pips_final, 1) if final_decision in ("BUY", "SELL") else None,
            "tp_pips": round(tp_pips_final, 1) if final_decision in ("BUY", "SELL") else None,
            "latency_ms": round((time.time() - t_start) * 1000, 2),
            "veto": decision != final_decision,
            "veto_reason": _veto_reason(decision, final_decision, qwen),
            "mt5_time": mt5_ts,
        },
    )

    return PredictResponse(
        cycle_id=cycle_id,
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
    await log_to_audit(
        "retrain_forced",
        {
            "status": result.get("status"),
            "filled_count": state.filled_count,
            "in_progress": state.retrain_in_progress,
            "last_retrain_time": state.last_retrain_time,
            "outcomes_count": len(state.outcomes),
        },
    )
    return {"status": result["status"], "filled_count": state.filled_count, "in_progress": state.retrain_in_progress}


@router.post("/retrain/approve")
async def approve_retrain_endpoint(_: None = Depends(verify_token)):
    """Aprueba el retrain pendiente (umbral alcanzado con retrain_auto_approve=false).

    No salta el gate de promoción: ese se aplica dentro de _do_retrain.
    """
    result = await _approve_retrain()
    await log_to_audit(
        "retrain_approved",
        {
            "status": result.get("status"),
            "filled_count": result.get("filled_count"),
            "min_trades_for_retrain": result.get("min_trades_for_retrain"),
        },
    )
    return result


@router.get("/model/status")
async def model_status(_: None = Depends(verify_token)):
    """P1-2: qué bundle corre EN MEMORIA vs qué certifica EL DISCO (in_sync).

    `in_sync=false` = hubo una promoción que no se recargó (o un cambio en disco
    no certificado): el operador ve de inmediato disco ≠ memoria.
    """
    from services.model_registry import load_manifest

    disk = load_manifest(MODEL_V3_PATH)  # RuntimeError si no está certificado
    memory = dict(_CACHED_BUNDLE)
    in_sync = (
        memory.get("model_sha256") == disk["model_sha256"]
        and memory.get("vec_norm_sha256") == disk.get("vec_norm_sha256")
    )
    return {
        "memory": memory,
        "disk": {
            "model_sha256": disk["model_sha256"],
            "vec_norm_sha256": disk.get("vec_norm_sha256"),
            "source": disk["source"],
            "created_at": disk.get("created_at"),
        },
        "in_sync": in_sync,
    }


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

    # 5. Qwen LLM — estado degradado (P1-6). Mientras falle, /predict usa
    #    safe defaults (quality=0 → veto fail-closed BUY/SELL → HOLD).
    from services.qwen_client import get_qwen_state
    qs = get_qwen_state()
    qwen_ok = qs.consecutive_failures == 0
    components["qwen"] = {
        "ok": qwen_ok,
        "calls_ok": qs.calls_ok,
        "calls_failed": qs.calls_failed,
        "consecutive_failures": qs.consecutive_failures,
        "last_ok_at": qs.last_ok_at,
        "last_fail_at": qs.last_fail_at,
        "last_error": qs.last_error,
        "error": None if qwen_ok else (qs.last_error or "Qwen degraded"),
    }
    if not qwen_ok:
        issues.append(
            f"Qwen degradado ({qs.consecutive_failures} fallos seguidos): "
            f"{qs.last_error} — predict opera con safe defaults (veto a compra/venta)"
        )

    # Determine overall status
    critical_failures = sum(1 for c in components.values() if not c["ok"])
    if critical_failures > 0:
        # Solo database/ppo_model caídos son críticos; el resto (qwen, mt5) degrada
        critical_component_failed = any(
            not components[name]["ok"] for name in ("database", "ppo_model")
        )
        status = "critical" if critical_component_failed else "degraded"
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


# Dominios de trade_outcomes: DEBEN igualar los CHECK de db/schema.sql para que
# un payload inválido falle en el borde con 422, nunca con un 500 desde asyncpg.
VALID_EXIT_REASONS = frozenset({"sl", "tp", "manual", "news", "signal_end", "ea"})
DIRECTION_CANONICAL = {"LONG": "LONG", "SHORT": "SHORT", "BUY": "LONG", "SELL": "SHORT"}


class TradeFilledRequest(BaseModel):
    symbol: str
    entry_time: float | str  # Unix timestamp (float) or numeric string
    exit_time: float | str   # Unix timestamp (float) or numeric string
    pnl: float | str
    pnl_pct: float | str
    direction: str   # "LONG", "SHORT", "BUY", "SELL" (case-insensitive)
    sl_hit: bool
    tp_hit: bool
    exit_reason: str  # "sl", "tp", "manual", "news", "signal_end", "ea"
    deal_ticket: int | str | None = None
    position_id: int | str | None = None

    @field_validator("direction")
    @classmethod
    def _canonical_direction(cls, v: str) -> str:
        canonical = DIRECTION_CANONICAL.get(str(v).strip().upper())
        if canonical is None:
            # PydanticCustomError en vez de ValueError: pydantic mete el
            # ValueError original en errors()['ctx'] y FastAPI no puede
            # serializarlo a JSON → el 422 se rompería en un 500.
            raise PydanticCustomError(
                "value_error.direction",
                f"direction must be one of {sorted(DIRECTION_CANONICAL)} (got {v!r})",
            )
        return canonical

    @field_validator("exit_reason")
    @classmethod
    def _valid_exit_reason(cls, v: str) -> str:
        reason = str(v).strip().lower()
        if reason not in VALID_EXIT_REASONS:
            raise PydanticCustomError(
                "value_error.exit_reason",
                f"exit_reason must be one of {sorted(VALID_EXIT_REASONS)} (got {v!r})",
            )
        return reason


class EADecisionRequest(BaseModel):
    cycle_id: str
    symbol: str
    received_decision: str
    action: str
    order_ticket: str | None = None
    volume: float | None = None
    sl: float | None = None
    tp: float | None = None
    reason: str | None = None
    latency_ms: float | None = None
    raw_response: str | None = None
    mt5_time: float | None = None  # epoch desde MT5; si falta, se usa time.time() como fallback


@router.post("/ea/decision")
async def ea_decision_webhook(req: EADecisionRequest, _: None = Depends(verify_token)):
    mt5_ts = req.mt5_time if req.mt5_time is not None else time.time()
    await log_to_audit(
        "ea_decision",
        {
            "symbol": req.symbol,
            "received_decision": req.received_decision,
            "action": req.action,
            "order_ticket": req.order_ticket,
            "volume": req.volume,
            "sl": req.sl,
            "tp": req.tp,
            "reason": req.reason,
            "latency_ms": req.latency_ms,
            "raw_response": req.raw_response,
            "mt5_time": mt5_ts,
        },
        cycle_id=req.cycle_id,
    )
    return {"status": "recorded"}


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
    if entry_ts == 0:
        entry_ts = exit_ts - 1
    if exit_ts == 0:
        exit_ts = entry_ts + 1
    if entry_ts == exit_ts:
        exit_ts = entry_ts + 1
    if entry_ts < minimum_timestamp or exit_ts < minimum_timestamp:
        raise HTTPException(status_code=422, detail="entry_time/exit_time must be >= 2000-01-01 UTC")
    if exit_ts <= entry_ts:
        raise HTTPException(status_code=422, detail="exit_time must be greater than entry_time")
    entry_time_out = str(int(entry_ts))
    exit_time_out = str(int(exit_ts))

    logger.info("[TRADE-FILLED] payload symbol=%s entry=%s exit=%s pnl=%.2f pct=%.4f dir=%s reason=%s",
                req.symbol, entry_time_out, exit_time_out, req.pnl, req.pnl_pct, req.direction, req.exit_reason)
    direction_map = {"BUY": "LONG", "SELL": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}
    direction = direction_map.get(req.direction.upper()) if req.direction else req.direction
    pnl = float(req.pnl) if isinstance(req.pnl, str) else req.pnl
    pnl_pct = float(req.pnl_pct) if isinstance(req.pnl_pct, str) else req.pnl_pct
    deal_ticket = int(req.deal_ticket) if req.deal_ticket not in (None, "") else None
    position_id = int(req.position_id) if req.position_id not in (None, "") else None
    await record_trade_filled(
        symbol=req.symbol,
        entry_time=entry_time_out,
        exit_time=exit_time_out,
        pnl=pnl,
        pnl_pct=pnl_pct,
        direction=direction,
        sl_hit=req.sl_hit,
        tp_hit=req.tp_hit,
        exit_reason=req.exit_reason,
        deal_ticket=deal_ticket,
        position_id=position_id,
    )
    return {"status": "recorded"}

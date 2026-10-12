import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np
import pandas as pd
import torch
import gymnasium as gym
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from config import settings
from core.container import build_registry_from_settings
from core.pair import PairRegistry, default_model_path
from core.rules import MIN_WIN_EUR
from db.connection import get_pool
from ml.trading_env_v2 import (
    CHURN_BARS,
    EXTRA_STATE_FEATURES,
    FOREX_MARKET_FEATURE_COUNT,
    MARKET_FEATURES,
    TIME_STOP_BARS,
    build_v3_observation,
    decode_v3_direction,
    engineer_market_features,
    get_market_features,
    production_stops,
)
from services.auto_retrain import (
    approve_retrain as _approve_retrain,
    force_retrain as _force_retrain,
    get_state as _get_retrain_state,
    record_trade_filled,
    TRAINING_SYMBOL as _TRAINING_SYMBOL,
)
from services.market_time import utc_hour_from_candle
from services.news_filter import get_upcoming_news, is_news_blackout
from services.performance_metrics import record_cycle_metrics
from services.risk_guardian import observe_equity
from services.risk_state_store import flush_risk_state
from services.position_sizing import (
    PIP_SIZE,
    expected_close_eur,
    is_supported,
    pip_value_per_lot,
    size_ppo_position,
)
from services.order_manager import mark_order_filled
from services.qwen_client import query_qwen_unified
from services.structured_logging import log_to_audit
from services.strategist import (
    STRATEGIST_VERSION,
    deliberate,
    fetch_bars_since_close,
    fetch_last_exit,
)
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

# Paths de artefactos: P3-1b — la convención de rutas vive en
# core.pair.default_model_path (fuente única); este router solo la consume.
QWEN_URL = getattr(settings, "qwen_url", "http://100.90.16.33:8080/v1/chat/completions")

QUALITY_THRESHOLD = 5.0
RANGING_VETO = True

# holding_norm + time-stop — conversión de unidades (Félix 2026-10-08): el EA
# mide la edad de la posición en pasos M5 ((TimeCurrent−pTime)/
# PeriodSeconds(M5), L310) y env/obs trabajan en velas H1 (max_holding_steps
# = 120, TIME_STOP_BARS=12) → serve convierte M5→H1 UNA vez y usa el dominio
# entrenado en obs, time-stop y traza. Sin la conversión, holding_norm del
# bundle saturaba a la 1.ª hora (60/120=0.5) frente a las 120 h del env.
# Paridad en reloj: env cierra en la barra H1 12, serve ordena CLOSE en el
# primer ciclo con edad ≥ 12 h (desfase ≤ 5 min + barra).
_M5_PER_H1 = 12
SL_MIN_NORM = 0.05
SL_MAX_NORM = 0.30
TP_MIN_NORM = 0.05
TP_MAX_NORM = 0.50

# S-1/S-2: rate-limit del aviso de sizing (1 WARNING / 600 s por símbolo).
# La verdad completa (sizing_below_min + veto_reason sizing_veto) se escribe
# en audit_log EN TODOS los ciclos — el rate-limit solo silencia el ruido de
# `docker logs` (antes: 21 ERROR "sizing inviable" en una ventana).
_SIZING_WARN_TS: dict[str, float] = {}

# Histeresis CLOSE K=2 ciclos (ítem cierres tempranos, 2026-10-12): el
# contrafactual con datos reales mostró que los cierres `ea` (zona muerta)
# dejan +257€ sobre la mesa (83% tocó TP en 48h). K=2: 1er ciclo en zona
# muerta → HOLD, 2º ciclo consecutivo → CLOSE. Contador por símbolo.
_DEAD_ZONE_COUNTER: dict[str, int] = {}


def _dead_zone_close(symbol: str, decision: str, env_position: int = 1) -> bool:
    """Histeresis CLOSE K=2. True = cerrar; False = mantener (HOLD).

    Resetea el contador cuando la decisión no es CLOSE o no hay posición.
    El 1er ciclo en zona muerta no cierra; el 2º consecutivo sí.
    `env_position` default 1 = posición abierta (el caso que importa).
    """
    if decision != "CLOSE" or env_position == 0:
        _DEAD_ZONE_COUNTER[symbol] = 0
        return True
    count = _DEAD_ZONE_COUNTER.get(symbol, 0) + 1
    _DEAD_ZONE_COUNTER[symbol] = count
    return count >= 2
_SIZING_WARN_INTERVAL_S = 600.0


def _veto_reason(
    original: str,
    final: str,
    qwen: dict,
    sizing_rejected: bool = False,
    position_rejected: bool = False,
    time_stop: bool = False,
    reentry_rejected: bool = False,
    low_confidence: bool = False,
    reward_rejected: bool = False,
) -> str | None:
    if original == final:
        return None
    # P1 / H4: el time-stop no es un veto del modelo — es una SALIDA mecánica
    # (posición ≥ 12 H1 → CLOSE). Explica un final CLOSE que ningún veto de
    # la cadena puede justificar, así que va por delante de todo.
    if time_stop:
        return "time_stop"
    # A-1: el veto de posición es el más mecánico de todos — el EA lo ejecuta
    # en 3 puntos (L407/L440/L475) con MISMA variable pre-request, así que su
    # flag es autoridad absoluta: se declara antes que sizing y cualquier
    # motivo derivado de Qwen. Es mutuamente excluyente con sizing por
    # construcción (se activa antes de la cadena → final_decision ya es HOLD).
    if position_rejected:
        return "position_veto"
    # S-1/S-2: el bloque de sizing solo se activa con final_decision en
    # (BUY, SELL), es decir, cuando NINGÚN otro veto ya había revertido la
    # decisión → el flag es autoridad: se declara ANTES que cualquier otro
    # motivo (antes, 21 rechazos de sizing se atribuían a ranging/unknown_veto).
    if sizing_rejected:
        return "sizing_veto"
    # Opción A (Félix 2026-10-12): gate de recompensa mínima — mecánico
    # como sizing (la regla 3€ decide, sin juicio) y MUTUAMENTE EXCLUSIVO
    # con él por construcción (exige final_lots > 0 → si no cabe lote,
    # sizing se queda la atribución y el orden entre ambos nunca decide).
    # Va ANTES del juicio de re-entrada y de los inferidos de Qwen: es un
    # veto EXPLÍCITO y trazable (calidad de la regla, no interpretación).
    if reward_rejected:
        return "reward_below_min"
    # P1 / H6 → R1 (Félix 2026-10-09): re-entrada juzgada por el CEREBRO
    # (`reentry_veto`) — sustituyó al veto duro `churn_cooldown` (constante
    # «grabada a fuego»). Sigue declarado ANTES de los inferidos de Qwen
    # (ranging/quality/bias): es la razón más específica del ciclo (contexto
    # de cierre) y con final=HOLD + RANGING el ranging_veto de abajo se lo
    # atribuiría indebidamente. Mutuamente excluyente con position_veto
    # (position==0) y con sizing (la cadena ya revirtió a HOLD →
    # sizing_below_min no se activa).
    if reentry_rejected:
        return "reentry_veto"
    # P0 / H1: gate de confianza de entrada (umbral operado por el operador).
    # Va ANTES de los inferidos de Qwen (ranging/quality/bias): es un veto
    # EXPLÍCITO y trazable, no una interpretación de régimen. Detrás de los
    # mecánicos (time-stop/position/reentry) por la misma regla de prioridad.
    if low_confidence:
        return "low_confidence"
    if final == "HOLD" and qwen.get("regime") == "RANGING":
        return "ranging_veto"
    if final == "HOLD" and qwen.get("quality", 10) < QUALITY_THRESHOLD:
        return "quality_veto"
    if original == "BUY" and final == "HOLD" and qwen.get("bias") == "BEARISH":
        return "bias_veto_bearish"
    if original == "SELL" and final == "HOLD" and qwen.get("bias") == "BULLISH":
        return "bias_veto_bullish"
    return "unknown_veto"


# Estado en memoria para inferencia rápida — P3-1b: UN BRAIN POR PAR.
# Cada entrada es el bundle certificado de ESE símbolo (modelo + normalizador
# + procedencia): los pares no comparten estado de inferencia.
@dataclass(frozen=True)
class Brain:
    """Bundle certificado de UN par en memoria (P0-5/P1-2 + P3-1b)."""

    model: PPO
    vec_norm: Optional[VecNormalize]
    bundle: dict   # P1-2: procedencia del bundle EN MEMORIA (sha, source, loaded_at)
    # Fase 3: contrato de obs del bundle — True = (20,25) con canal
    # bars_since_close; False = (20,24) pre-memoria (derivado en load, nunca
    # inferido a mano).
    memory_channel: bool = False


_BRAINS: dict[str, Brain] = {}
_REGISTRY: Optional[PairRegistry] = None   # mundos configurados vía PAIRS


def _get_registry() -> PairRegistry:
    """Registro de pares (mundos) — construido una vez, de forma explícita.

    Construcción perezosa (nunca en import del módulo): los tests parchean
    `services.*` antes de que exista ningún adapter, y un PAIRS inválido
    falla con su error en el primer uso, no en el import.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry_from_settings()
    return _REGISTRY


def load_inference_artifacts(symbol: str) -> None:
    """Carga PPO y VecNormalize del par `symbol`, validando su bundle (P0-5).

    Cualquier incoherencia (manifest ausente, hash distinto, obs_shape distinto)
    lanza RuntimeError y detiene el arranque: nunca se sirve un binario sin
    certificar ni se degrada en silencio.

    El swap de la caché es ATÓMICO por símbolo: la entrada `_BRAINS[symbol]`
    se asigna al final — si la validación o la carga de VecNormalize fallan,
    se mantiene íntegro el bundle anterior de ese par (o su ausencia), nunca
    un estado mitad nuevo, mitad viejo.
    """
    from services.model_registry import assert_obs_shape, validate_bundle

    model_path = default_model_path(symbol)
    manifest = validate_bundle(model_path)
    model = PPO.load(model_path, device="cpu")
    logger.info("[AI-STARTUP] Modelo PPO v3 de %s cargado en memoria exitosamente.", symbol)

    obs_space = getattr(model, "observation_space", None)
    assert_obs_shape(manifest, getattr(obs_space, "shape", None))

    # Fase 3: el ancho del observation_space del bundle decide si el obs
    # lleva el canal de memoria (20,25) o es pre-memoria (20,24). Ancho
    # desconocido = RuntimeError: nunca se sirve un contrato que no se
    # construye.
    obs_width = None
    obs_shape = getattr(obs_space, "shape", None)
    if obs_shape:
        obs_width = obs_shape[-1]
    base_width = FOREX_MARKET_FEATURE_COUNT + EXTRA_STATE_FEATURES
    if obs_width == base_width:
        memory_channel = False
    elif obs_width == base_width + 1:
        memory_channel = True
    else:
        raise RuntimeError(
            f"[AI-STARTUP] Contrato de observación desconocido para {symbol}: "
            f"{obs_shape} (esperado {base_width} pre-memoria "
            f"o {base_width + 1} con memoria)"
        )

    vec_norm_file = manifest.get("vec_norm_file")
    if vec_norm_file:
        vec_path = os.path.join(os.path.dirname(model_path), vec_norm_file)
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
    _BRAINS[symbol] = Brain(
        model=model,
        vec_norm=vec_norm,
        memory_channel=memory_channel,
        bundle={
            "model_sha256": manifest["model_sha256"],
            "vec_norm_sha256": manifest.get("vec_norm_sha256"),
            "source": manifest["source"],
            "loaded_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def reload_inference_artifacts(symbol: str) -> dict:
    """P1-2: hot-reload del bundle de `symbol` tras una promoción, sin reiniciar.

    Valida el bundle nuevo ANTES de tocar la caché: si falla, la excepción se
    propaga (el done-callback del retrain emite alerta) y se sigue sirviendo el
    bundle anterior de ESE par — nunca un swap parcial ni degradación silenciosa.
    """
    load_inference_artifacts(symbol)
    brain = _BRAINS[symbol]
    logger.warning(
        "[AI-RELOAD] Bundle de %s recargado en memoria: source=%s sha=%s…",
        symbol,
        brain.bundle.get("source"),
        str(brain.bundle.get("model_sha256"))[:12],
    )
    return dict(brain.bundle)


@router.on_event("startup")
async def on_startup():
    # P3-1b: cada par habilitado en PAIRS carga SU brain (mundo autónomo).
    for ctx in _get_registry().enabled():
        load_inference_artifacts(ctx.config.symbol)


def _sma200_context(raw_sorted: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
    """(distancia, pendiente) del SMA200 sobre la serie ASCENDENTE completa.

    Paquete 1-4 (Félix 2026-10-09): contexto de ESTRUCTURA de largo plazo para
    el prompt de Qwen. Se calcula sobre la serie larga (hasta 240 velas) — las
    features del modelo nunca la ven (siguen en las 120 recientes, obs
    byte-idéntico).

    Guardas — contexto de calidad, nunca inventado:
      - sin columna `time` (serie sin orden fiable) → (None, None);
      - < 205 velas (SMA200 + 5 velas de margen para la pendiente) → (None, None);
      - NaN en los puntos que se leen (la media no está aún formada) → (None, None).
    """
    if "time" not in raw_sorted.columns or len(raw_sorted) < 205:
        return None, None
    close = raw_sorted["close"].astype(float)
    sma = close.rolling(200).mean()
    if pd.isna(sma.iloc[-1]) or pd.isna(sma.iloc[-6]):
        return None, None
    # dist: precio actual lejos de la media larga; slope: pendiente de 5 barras
    return float(close.iloc[-1] - sma.iloc[-1]), float(sma.iloc[-1] - sma.iloc[-6])


def _higher_tf_context_line(tf: str, res) -> str:
    """Línea de contexto del marco superior H4/D para el prompt de Qwen.

    Foco ampliado H4/D (Félix 2026-10-12, backlog P2 «Contexto H4/D»): SMA200
    (estructura: posición + pendiente), RSI14 y ADX del timeframe superior —
    el «contexto global» que la Fase 2 nunca cableó (solo `adx` H1) y que el
    pack 1-4 sólo aproximó con un SMA200 DERIVADO de 240 H1.

    Guardas — contexto de calidad, nunca inventado y nunca una excepción
    hacia /predict (H4/D1 son contexto, no críticos):
      - Exception (fetch caído) o HTTP ≠ 200 → «- {tf}: Unavailable» + warning;
      - JSON inválido o payload sin ≥30 velas o sin columna `time`
        (orden fiable = tendencia honesta) → «- {tf}: Unavailable»;
      - CONTINUIDAD (vuelta roja 2026-10-12): hueco >168h entre barras →
        «- {tf}: Unavailable» + warning. El feed real del MCP tenía un
        agujero de 9.5 meses (historia hasta 2025-12-31 + solo el día vivo;
        `candles/date` no recupera el rango) y sobre esa serie el SMA200/RSI
        mienten (ejemplo medido: RSI 2.7, SMA200 dist −0.047 mezclando
        dic-2025 con hoy). Ningún cierre de mercado/feriado legítimo supera
        7 días (fin de semana ≤56h, festivos ≤~120h);
      - NaN en RSI/ADX → se omite el campo (el SMA200 ya tiene sus guardas
        en `_sma200_context`: ≥205 velas y no-NaN).
    """
    if isinstance(res, Exception):
        logger.warning("higher-TF %s fetch falló: %s", tf, res)
        return f"- {tf}: Unavailable"
    status = getattr(res, "status_code", None)
    if status != 200:
        logger.warning("higher-TF %s HTTP %s del MCP", tf, status)
        return f"- {tf}: Unavailable"
    try:
        data = res.json()
    except ValueError:
        logger.warning("higher-TF %s: payload sin JSON", tf)
        return f"- {tf}: Unavailable"
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("candles") or []
    else:
        raw = []
    if len(raw) < 30:
        return f"- {tf}: Unavailable"
    try:
        frame = pd.DataFrame(raw)
        if "time" not in frame.columns:
            return f"- {tf}: Unavailable"
        # Continuidad: un hueco >7 días invalida la serie entera (el bloque
        # viejo y el reciente no forman una historia; los indicadores saldrían
        # de la mezcla). Ver docstring — feed real roto desde ≤2026-10-07.
        times = pd.to_datetime(frame["time"], errors="coerce", utc=True).dropna()
        if len(times) < len(frame):
            return f"- {tf}: Unavailable"
        gap_h = float(times.sort_values().diff().dt.total_seconds().div(3600).max())
        if gap_h > 168.0:
            logger.warning(
                "higher-TF %s: hueco de %.0fh en la serie → contexto descartado",
                tf, gap_h,
            )
            return f"- {tf}: Unavailable"
        df = engineer_market_features(frame)   # ordena por `time` (ascendente)
    except (KeyError, ValueError, TypeError):
        logger.warning("higher-TF %s: payload de velas no aprovechable", tf)
        return f"- {tf}: Unavailable"
    parts: list[str] = []
    dist, slope = _sma200_context(df)
    if dist is not None and slope is not None:
        trend = "ABOVE" if dist > 0 else "BELOW"
        slope_dir = "RISING" if slope > 0 else "FALLING" if slope < 0 else "FLAT"
        parts.append(
            f"Price {trend} SMA200 ({dist:+.4f}), SMA200 {slope_dir} ({slope:+.4f})"
        )
    rsi_v = float(df["rsi14"].iloc[-1]) * 100.0
    adx_v = float(df["adx"].iloc[-1])
    if not pd.isna(rsi_v):
        parts.append(f"RSI {rsi_v:.1f}")
    if not pd.isna(adx_v):
        parts.append(f"ADX {adx_v:.2f}")
    if not parts:
        return f"- {tf}: Unavailable"
    return f"- {tf}: " + ", ".join(parts)


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

    # P2-2: pip centralizado en position_sizing.PIP_SIZE (fuente única; antes un
    # heurístico de dígitos daba 0.01 en oro frente al 0.10 del resto del
    # sistema). Símbolo sin pip declarado → HOLD visible, nunca heurística.
    sym = req.symbol.upper()

    # P3-1b: gate de MUNDO — el símbolo debe estar configurado en PAIRS y
    # habilitado. Desconocido o deshabilitado → HOLD explícito ANTES de tocar
    # MT5, noticias o el modelo: jamás un modelo de otro par responde por un
    # par que no es suyo (los mundos son independientes y la lista de mundos
    # es settings.pairs).
    ctx = _get_registry().get(sym)
    if ctx is None:
        logger.warning("[%s] par NO configurado en PAIRS: %r → HOLD", cycle_id, req.symbol)
        return PredictResponse(
            cycle_id=cycle_id,
            ml_prob=0.5,
            llm_bias="ERROR",
            decision="HOLD",
            quality_reason="pair_not_configured",
        )
    if not ctx.config.enabled:
        logger.warning("[%s] par deshabilitado en PAIRS: %r → HOLD", cycle_id, req.symbol)
        return PredictResponse(
            cycle_id=cycle_id,
            ml_prob=0.5,
            llm_bias="NEUTRAL",
            decision="HOLD",
            quality_reason="pair_disabled",
        )

    if not is_supported(sym):
        logger.error("[%s] símbolo sin pip centralizado: %r → HOLD", cycle_id, req.symbol)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
    pip_size = PIP_SIZE[sym]

    # P2-3: blackout de noticias de alto impacto — mismo mecanismo que el SMC
    # (simple_pipeline → news_filter.is_news_blackout): ventana ±news_blackout_minutes
    # alrededor de un evento High/Medium de la divisa → sin órdenes (ni entrada
    # ni inversión); SL/TP y trailing del EA siguen activos. Si el calendario no
    # se puede descargar, la función hace fail-open con error logueado (idéntico
    # al SMC — no se inventa un segundo comportamiento).
    blacked_out, news_event = await is_news_blackout(sym)
    if blacked_out:
        logger.warning("[%s] BLACKOUT noticias (%s) → HOLD", cycle_id, news_event)
        return PredictResponse(
            cycle_id=cycle_id,
            ml_prob=0.5,
            llm_bias="NEUTRAL",
            decision="HOLD",
            quality_reason=f"news_blackout:{news_event}",
        )

    equity_val = 0.0
    candles = []
    try:
        async with httpx.AsyncClient() as client:
            equity_task = client.get(
                f"{settings.mt5_http_url}/api/v1/account/info",
                timeout=2.0,
            )
            candles_task = client.get(
                # Paquete 1-4: 240 velas para el SMA200 (contexto de estructura);
                # las features del modelo se calculan sobre las 120 recientes
                # (tail tras ordenar → obs byte-idéntico, ver test de wiring).
                f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe=H1&count=240",
                timeout=3.0,
            )
            # Foco ampliado H4/D (Félix 2026-10-12): marcos SUPERIORES para el
            # contexto de Qwen — 250 velas por TF (SMA200 + 5 de margen). Van en
            # el MISMO gather (sin latencia secuencial) y con
            # return_exceptions: su fallo degrada la línea a «Unavailable»
            # (son contexto, no críticos); equity/H1 conservan su camino HOLD.
            h4_task = client.get(
                f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe=H4&count=250",
                timeout=3.0,
            )
            d1_task = client.get(
                f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe=D1&count=250",
                timeout=3.0,
            )
            equity_res, candles_res, h4_res, d1_res = await asyncio.gather(
                equity_task, candles_task, h4_task, d1_task,
                return_exceptions=True,
            )
    except (httpx.ConnectTimeout, httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("[%s] MT5 connection error in predict: %s", cycle_id, exc)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    # equity y H1 son CRÍTICOS: el return_exceptions del gather no puede
    # enmascarar su tratamiento de siempre (mismo camino HOLD / re-lanzado).
    for _critico in (equity_res, candles_res):
        if isinstance(_critico, Exception):
            if isinstance(_critico, httpx.HTTPError):
                logger.warning("[%s] MT5 connection error in predict: %s", cycle_id, _critico)
                return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
            raise _critico

    try:
        equity_val = (
            float(equity_res.json().get("equity", 0.0))
            if equity_res.status_code == 200 else 0.0
        )
    except (ValueError, TypeError, KeyError):
        logger.error("[%s] payload de equity no numérico (HTTP %s)", cycle_id, equity_res.status_code)
        equity_val = 0.0

    # P2-2: sin equity fiable no hay sizing posible — se corta aquí en vez de
    # emitir órdenes de volumen 0 (auditoría: "equity=0 → volumen 0").
    if equity_val <= 0.0:
        logger.error(
            "[%s] equity no disponible (HTTP %s, equity=%s) → HOLD",
            cycle_id, equity_res.status_code, equity_val,
        )
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    # P2-4: la cuenta observa el equity en CADA ciclo — rollover automático a
    # medianoche UTC + peak en vivo (el drawdown se mide contra el pico real,
    # no contra el del arranque). Sin cambios dirty → el flush no toca la BD.
    observe_equity(equity_val)
    await flush_risk_state(symbol=req.symbol)

    if candles_res.status_code != 200:
        logger.warning("[%s] candles HTTP %s from MT5", cycle_id, candles_res.status_code)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
    data = candles_res.json()
    candles = data if isinstance(data, list) else data.get("candles", [])

    raw_df = pd.DataFrame(candles)
    if raw_df.empty or len(raw_df) < 30:
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    # Paquete 1-4: la serie COMPLETA (hasta 240 velas) alimenta el contexto de
    # estructura (SMA200); las features se calculan sólo sobre las 120 más
    # recientes — idénticas al contrato de siempre (obs byte-idéntico con
    # cualquier profundidad de fetch). Sin columna `time` → previo (sin orden
    # fiable no hay SMA200 honesto).
    if "time" in raw_df.columns:
        raw_sorted = raw_df.sort_values("time").reset_index(drop=True)
        sma200_dist, sma200_slope = _sma200_context(raw_sorted)
        features_input = raw_sorted.tail(120)
    else:
        sma200_dist, sma200_slope = None, None
        features_input = raw_df

    df = engineer_market_features(features_input)
    features_list = get_market_features(df)
    # P0: leer de `df` (ordenada por engineer) — con raw_df (descendente del
    # MCP) el "reciente" eran las velas más ANTIGUAS y la hora de sesión la de
    # una semana atrás.
    candle_context = _build_qwen_candle_context(df, count=10)

    # Foco ampliado H4/D: bloque de marcos superiores para el Qwen — esquema
    # SIEMPRE constante (`- H4:` / `- D1:`, con «Unavailable» si el fetch
    # falló) → anti-ancla H2 y degradación honesta y visible.
    higher_tf_context = (
        "- Higher-timeframe context (H4/D1) — contrast the H1 signal with the "
        "larger trend:\n"
        f"{_higher_tf_context_line('H4', h4_res)}\n"
        f"{_higher_tf_context_line('D1', d1_res)}"
    )

    brain = _BRAINS.get(sym)
    if brain is None:
        load_inference_artifacts(sym)
        brain = _BRAINS.get(sym)
    if brain is None:
        logger.error("[%s] No hay modelo PPO disponible para %s.", cycle_id, sym)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    current_price = float(df['close'].iloc[-1])
    current_atr = float(df['atr'].iloc[-1]) if 'atr' in df.columns else 0.0001
    entry_p = req.entry_price if env_position != 0 and req.entry_price > 0 else current_price

# P2-1: mismos stops que el env de entrenamiento (production_stops) —
    # única fuente de verdad del contrato train/serve.
    # Paquete 1-4: niveles estructurales calculados UNA vez → reutilizados en
    # stops Y en el prompt de Qwen (antes inline, sin ruta hacia el LLM).
    support_level = float(df['support_level'].iloc[-1]) if 'support_level' in df.columns else None
    resistance_level = float(df['resistance_level'].iloc[-1]) if 'resistance_level' in df.columns else None
    sl_pips, tp_pips = production_stops(
        current_atr, pip_size,
        swing_high=float(df['last_swing_high'].iloc[-1]) if 'last_swing_high' in df.columns else None,
        swing_low=float(df['last_swing_low'].iloc[-1]) if 'last_swing_low' in df.columns else None,
        resistance_level=resistance_level,
        support_level=support_level,
        current_price=current_price,
        position=env_position,
    )
    sl_distance_price = sl_pips * pip_size

    if env_position != 0 and req.entry_price > 0:
        entry_p = req.entry_price
        sl_p = req.sl_price if req.sl_price > 0 else (entry_p - sl_distance_price if env_position == 1 else entry_p + sl_distance_price)
        tp_p = req.tp_price if req.tp_price > 0 else (entry_p + tp_pips * pip_size if env_position == 1 else entry_p - tp_pips * pip_size)
    else:
        sl_p = entry_p - sl_distance_price if env_position == 1 else entry_p + sl_distance_price
        tp_p = entry_p + tp_pips * pip_size if env_position == 1 else entry_p - tp_pips * pip_size

    market_slice = df[features_list].iloc[-20:].values
    # Fase 3 / P1 / R1: UNA sola consulta de velas desde el último cierre —
    # alimenta el canal de memoria del obs (bundle (20,25)) Y la ventana de
    # re-entrada (R1: dentro de ella decide el cerebro, `reentry_ok`). Con
    # el bundle activo la memoria ya se consultaba en cada ciclo; para
    # bundles legados pasa a consultarse también (la ventana no depende del
    # contrato del obs). `None` = sin cierre registrado o salida anterior a
    # la ventana → normalización 1.0 y SIN ventana, idéntico al env sin
    # `ever_closed`.
    bars_since_close = await fetch_bars_since_close(
        sym, df=df, current_price=current_price, pip_size=pip_size,
    )
    # R1 (Félix 2026-10-09): ventana de re-entrada — MISMO umbral que la
    # fricción anti-churn del env (CHURN_BARS=2 velas H1). La ventana es
    # paridad serve↔env; el JUICIO dentro de ella es del cerebro (antes:
    # veto duro `churn_cooldown` con esta misma condición).
    reentry_window = (
        env_position == 0
        and bars_since_close is not None
        and bars_since_close <= CHURN_BARS
    )
    max_holding_steps = 120
    bars_norm = None
    if brain.memory_channel:
        bars_norm = 1.0 if bars_since_close is None else min(1.0, bars_since_close / max_holding_steps)
    # Unidad única para edad/obs/time-stop: velas H1 (ver _M5_PER_H1).
    steps_h1 = req.steps_in_trade // _M5_PER_H1
    raw_obs = build_v3_observation(
        market_values=market_slice,
        expected_market_features=len(features_list),
        position=env_position,
        entry_price=entry_p,
        current_price=current_price,
        sl_price=sl_p,
        tp_price=tp_p,
        current_atr=current_atr,
        steps_in_trade=steps_h1,
        max_holding_steps=max_holding_steps,
        balance=equity_val,
        initial_balance=10000.0,
        window_size=20,
        bars_since_close_norm=bars_norm,
    )

    if brain.vec_norm is not None:
        norm_obs = brain.vec_norm.normalize_obs(raw_obs)
    else:
        norm_obs = raw_obs

    action, _ = brain.model.predict(norm_obs, deterministic=True)
    raw_action = np.asarray(action, dtype=np.float32).reshape(-1)
    # P2-1 / D3: contrato [direction, volume]. El bundle activo pre-P2-1
    # (4 cabezas) sigue siendo válido durante la transición: sl/tp ya se
    # ignoraban en serve (los stops salen de production_stops).
    direction, volume_norm = float(raw_action[0]), float(raw_action[1])

    target_pos, decision = decode_v3_direction(float(direction), env_position)
    # Histeresis CLOSE K=2 (ítem cierres tempranos, 2026-10-12): no cerrar en
    # el 1er ciclo en zona muerta — esperar 2 consecutivos antes de ejecutar.
    # Sin el contrafactual con datos reales, estos cierres dejaban +257€ sobre
    # la mesa (83% tocó TP en 48h). Resetea el contador fuera de zona muerta.
    if not _dead_zone_close(req.symbol, decision, env_position):
        logger.info(
            "[%s] HISTERESIS CLOSE ║ 1er ciclo en zona muerta → HOLD (K=2)",
            cycle_id,
        )
        decision = "HOLD"
    ml_prob = float(np.clip(abs(direction), 0.1, 0.99))

    # P2-2: sizing delegado en position_sizing.size_ppo_position — la cabeza
    # `volume` modula el riesgo (0…settings.ppo_risk_pct del equity) con el
    # pip_value centralizado por símbolo. Reemplaza: tope degenerado
    # min(0.5, 3%·equity/dist) SIEMPRE activo (cabeza `volume` muerta) y la
    # heurística "mín. 3€" con 0.1 €/pip/lot (error ×100 enmascarado por el
    # clip). El mismo cálculo lo aplica el env de entrenamiento (paridad).
    final_lots = size_ppo_position(
        volume_norm=volume_norm,
        sl_pips=sl_pips,
        equity=equity_val,
        pip_value=pip_value_per_lot(sym, current_price),
        risk_pct=settings.ppo_risk_pct,
        max_lot=settings.max_volume,
        min_lot=settings.min_volume,
    )

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

    # Paquete 1-4 (ítem 2): calendario económico en el prompt — ventana
    # −120…+240 min (lo reciente ya reaccionado + lo próximo), orden
    # ascendente y tope 3 (presupuesto de tokens). `None` con el filtro de
    # noticias deshabilitado (el prompt lo dice explícitamente). Fetch DESPUÉS
    # del bloque MT5: httpx está garantizado parcheado en la suite.
    calendar_events: Optional[list[dict]] = None
    if settings.news_filter_enabled:
        upcoming = await get_upcoming_news([req.symbol])
        calendar_events = [
            ev for ev in upcoming
            if -120.0 <= float(ev["minutes_until"]) <= 240.0
        ]
        calendar_events.sort(key=lambda ev: float(ev["minutes_until"]))
        calendar_events = calendar_events[:3]

    # R1: contexto de re-entrada para el juicio del cerebro — sólo dentro
    # de la ventana y con entrada propuesta (lo único que el veto puede
    # bloquear); así la consulta extra al último cierre ocurre únicamente
    # cuando su resultado puede cambiar la decisión. `last_exit=None`
    # (cierre fuera de la ventana de velas) → el prompt instruye juzgar
    # sólo con las velas.
    reentry = None
    if reentry_window and decision in ("BUY", "SELL"):
        reentry = {
            "bars_since_close": bars_since_close,
            "last_exit": await fetch_last_exit(
                sym, df=df, current_price=current_price, pip_size=pip_size,
            ),
        }

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
        hour=utc_hour_from_candle(pd.to_datetime(df['time'].iloc[-1])),
        news=live_news,
        candle_context=candle_context,
        cycle_id=cycle_id,
        logger=logger,
        recent_insights=recent_insights,
        # Fase 2: Context injection for Qwen
        adx=float(df['adx'].iloc[-1]) if 'adx' in df.columns else None,
        # Paquete 1-4: estructura de largo plazo + niveles S/R + calendario
        sma200_dist=sma200_dist,
        sma200_slope=sma200_slope,
        support_level=support_level,
        resistance_level=resistance_level,
        calendar_events=calendar_events,
        # Foco ampliado H4/D: marcos superiores (bloque ya formateado)
        higher_tf_context=higher_tf_context,
        # R1: juicio de re-entrada con el contexto del cierre reciente
        reentry=reentry,
    )

    final_decision = decision
    # P1 / H4 (Félix 2026-10-08): TIME-STOP — posición abierta ≥ 12 velas H1
    # (edad convertida a H1, ver _M5_PER_H1) → cierre forzado. Es una SALIDA
    # mecánica: va POR DELANTE de toda la cadena de vetos (en el env sólo
    # SL/TP —precio intra-vela— compiten antes, secciones 1/1b). El EA ya
    # sabe ejecutar `CLOSE` (L358 → CloseAllPositions).
    time_stop = env_position != 0 and steps_h1 >= TIME_STOP_BARS
    if time_stop:
        logger.warning(
            "[%s] TIME-STOP ║ posición abierta %d H1 ≥ %d → CLOSE",
            cycle_id, steps_h1, TIME_STOP_BARS,
        )
        final_decision = "CLOSE"
    # R1 (Félix 2026-10-09): JUICIO DE RE-ENTRADA — el veto duro
    # `churn_cooldown` (constante de 2 velas) se sustituyó por el cerebro:
    # Qwen decide `reentry_ok` con el contexto del cierre (pnl, dirección,
    # razón, movimiento). La VENTANA (≤ CHURN_BARS, paridad con la fricción
    # del env) queda intacta: lo único que cambia es QUIÉN decide.
    # Con Qwen degradado manda la red de emergencia documentada (no lógica
    # primaria): sólo se bloquea la 1ª vela tras el cierre (bars=0), el
    # mínimo vital mientras el cerebro no responde — el fail-closed de
    # calidad (quality=0) bloquea la entrada de todas formas.
    reentry_ok: Optional[bool] = None
    if reentry_window:
        if qwen.get("ok", True):
            reentry_ok = bool(qwen.get("reentry_ok", True))
        else:
            reentry_ok = bars_since_close > 0
            logger.warning(
                "[%s] REENTRY-DEFAULT (Qwen degradado) → reentry_ok=%s (bars=%d)",
                cycle_id, reentry_ok, bars_since_close,
            )
    # P0 / H1 (Félix 2026-10-09): GATE DE CONFIANZA DE ENTRADA — mecanismo
    # construido DESARMADO: `settings.min_entry_confidence = 0.0` (default)
    # → la rama de abajo no se evalúa jamás (no-op, cero cambio de
    # comportamiento). Evidencia EN CONTRA pre-registrada en README (n=18,
    # bandas inversas); armar solo con revisión N≥50 post-fix Qwen o orden
    # explícita (env MIN_ENTRY_CONFIDENCE).
    low_confidence = False
    # Opción A (Félix 2026-10-12): GATE DE RECOMPENSA MÍNIMA — si el TP del
    # trade no alcanza MIN_WIN_EUR (3 €) al tamaño elegido, la entrada es
    # imposible de ganar por CONSTRUCCIÓN: el TP sella la salida por debajo
    # de la regla (ni CLOSE del modelo ni un manual pueden superar 3 € sin
    # rellenar el TP antes). Evidencia: trade #255 (09-12) quality 9.0 /
    # ml_prob 0.99 con sizing 0.01 lot → TP 21.6 p = 1.93 € máx → LOSS por
    # la propia regla; gate simulado sobre las 48 órdenes rellenadas = 1
    # bloqueo (no degrada entradas sanas). `reward_eur` se audita SIEMPRE
    # que hay entrada propuesta (snapshot replayable, patrón
    # min_entry_confidence). Mutuamente excluyente con sizing (exige
    # final_lots > 0 → el veto de abajo se queda la atribución si no cabe
    # lote). La conversión es honesta por diseño: expected_close_eur lanza
    # ValueError para símbolos sin tasa EUR (hoy sólo EURUSD operativo).
    reward_eur: Optional[float] = None
    reward_below_min = False
    if final_decision in ("BUY", "SELL") and final_lots > 0.0 and tp_pips_final > 0.0:
        reward_eur = expected_close_eur(req.symbol, tp_pips_final, final_lots, current_price)
    # A-1: flip con posición abierta → el EA lo rechaza siempre en ejecución
    # (L407/L440/L475: rejected_position_open) porque la salida es exclusiva
    # SL/TP. El backend lo declara AQUÍ, antes de la cadena Qwen y del sizing,
    # con la MISMA información pre-request (req.position) que usa el EA: misma
    # decisión, misma traza, 0 rejected_position_open. CLOSE/HOLD intactos.
    position_rejected = env_position != 0 and final_decision in ("BUY", "SELL")
    if position_rejected:
        logger.warning(
            "[%s] VETO POSICION ║ flip %s con posición abierta (pos=%d) → HOLD "
            "(coherente con EA: salida solo SL/TP)",
            cycle_id, final_decision, req.position,
        )
        final_decision = "HOLD"
    elif reward_eur is not None and reward_eur < MIN_WIN_EUR and final_decision in ("BUY", "SELL"):
        # Opción A: mecánico (la regla 3€ ya lo decide) → va ANTES del
        # juicio de re-entrada y de los inferidos de Qwen, detrás de los
        # mecánicos superiores (time-stop/position, vía cadena + _veto_reason).
        reward_below_min = True
        logger.warning(
            "[%s] VETO RECOMPENSA ║ TP %.1f pips × %.3f lot = %.2f€ < MIN_WIN_EUR=%.2f → HOLD "
            "(imposible de ganar: el TP sella por debajo de la regla 3€)",
            cycle_id, tp_pips_final, final_lots, reward_eur, MIN_WIN_EUR,
        )
        final_decision = "HOLD"
    elif reentry_window and reentry_ok is False and final_decision in ("BUY", "SELL"):
        logger.warning(
            "[%s] VETO RE-ENTRADA (cerebro) ║ reentry_ok=false, %d velas H1 desde el "
            "último cierre ≤ CHURN_BARS=%d → HOLD",
            cycle_id, bars_since_close, CHURN_BARS,
        )
        final_decision = "HOLD"
    elif (
        settings.min_entry_confidence > 0.0
        and qwen["effective_prob"] < settings.min_entry_confidence
        and final_decision in ("BUY", "SELL")
    ):
        # P0 / H1: solo con el umbral ARMADO (> 0.0). `effective_prob` es
        # ml_prob × confidence_modifier (Qwen): el modificador dejó de ser
        # constante 1.0 con el fix anti-ancla H2 (2026-10-09), así que este
        # número es el candidato con poder discriminador real. Solo BUY/SELL
        # (entradas): FLAT/HOLD/CLOSE y los vetos mecánicos de arriba mandan.
        low_confidence = True
        logger.warning(
            "[%s] VETO CONFIANZA ║ effective_prob %.3f < umbral %.2f → HOLD",
            cycle_id, qwen["effective_prob"], settings.min_entry_confidence,
        )
        final_decision = "HOLD"
    elif RANGING_VETO and qwen.get("regime") == "RANGING" and final_decision in ("BUY", "SELL"):
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

    # P2-2/S-1: sizing no operable (ni el lote mínimo cabe en el tope de riesgo)
    # → no existe orden válida que enviar: degrada a HOLD y SE DECLARA en la
    # auditoría (`sizing_below_min` + `veto_reason=sizing_veto`). S-2: el ruido
    # pasa de 21 ERROR "sizing inviable" a 1 WARNING / 600 s por símbolo (el
    # literal prohibido ya no se emite en ningún nivel). La respuesta se queda
    # en HOLD: es el único valor no-comprado que el EA entiende (un valor nuevo
    # como "WAIT" caería en silencio en el EA y no reportaría ea_decision).
    sizing_below_min = False
    if final_lots <= 0.0 and final_decision in ("BUY", "SELL"):
        sizing_below_min = True
        final_decision = "HOLD"
        now_ts = time.time()
        if now_ts - _SIZING_WARN_TS.get(req.symbol, 0.0) >= _SIZING_WARN_INTERVAL_S:
            _SIZING_WARN_TS[req.symbol] = now_ts
            logger.warning(
                "[%s] sizing_below_min: vol_norm=%s sl=%s pips equity=%s → HOLD declarado "
                "(veto sizing, 1 aviso/%ss por símbolo)",
                cycle_id, volume_norm, sl_pips_final, equity_val, int(_SIZING_WARN_INTERVAL_S),
            )
        else:
            logger.debug("[%s] sizing_below_min (rate-limited) → HOLD", cycle_id)

    # Fase 2 / D-5: CAPA ESTRATEGA (shadow) — estado narrativo + criterios
    # C1–C8 (config versionada, TODOS OFF) evaluados en CADA ciclo y trazados
    # en `deliberation`. El verdict se REGISTRA, no se aplica: la decisión
    # final sigue siendo exclusiva de esta cadena (autonomía ≠ opacidad;
    # activación solo con replay aprobado — plan §6).
    veto_reason = _veto_reason(
        decision, final_decision, qwen,
        sizing_rejected=sizing_below_min,
        position_rejected=position_rejected,
        time_stop=time_stop,
        reentry_rejected=reentry_window and reentry_ok is False,
        low_confidence=low_confidence,
        reward_rejected=reward_below_min,
    )
    try:
        deliberation = await deliberate(
            symbol=sym,
            candidate=decision,
            veto_reason=veto_reason,
            qwen=qwen,
            qwen_degraded=not qwen.get("ok", True),
            direction_head=float(direction),
            lots=float(final_lots),
            sizing_below_min=sizing_below_min,
            env_position=env_position,
            df=df,
            current_price=current_price,
            current_atr=current_atr,
            pip_size=pip_size,
            equity=equity_val,
            quality_threshold=QUALITY_THRESHOLD,
        )
    except Exception as exc:
        # Contención TESTEADA (test_strategist_shadow): la capa es shadow con
        # cero poder de decisión — su fallo no tumba el ciclo de decisión; la
        # verdad se propaga A LA TRAZA (verdict=ERROR) + WARNING en log.
        logger.warning(
            "[%s] deliberación falló (%s: %s) → traza ERROR",
            cycle_id, type(exc).__name__, exc,
        )
        deliberation = {
            "verdict": "ERROR",
            "shadow": True,
            "config_version": STRATEGIST_VERSION,
            "error": f"{type(exc).__name__}: {exc}",
        }

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
            "veto_reason": veto_reason,
            "sizing_below_min": sizing_below_min,
            # P1 → R1: traza de las salidas/vetos — `time_stop` es SALIDA
            # (aunque el veto mecánico de arriba sea true: el modelo dijo otra
            # cosa); `reentry_window` = condición cruda de ventana (posición
            # plana y ≤ CHURN_BARS velas desde el cierre — misma medición que
            # el antiguo `churn_cooldown`, continuidad de la traza);
            # `reentry_ok` = juicio del cerebro (None = no evaluado: fuera de
            # ventana o sin entrada propuesta); `steps_in_trade_h1` = edad de
            # la posición en el dominio entrenado (H1, post-conversión).
            "time_stop": time_stop,
            "reentry_window": reentry_window,
            "reentry_ok": reentry_ok,
            # P0 / H1: flag = el veto de confianza OPERÓ este ciclo (la rama
            # solo evalúa BUY/SELL propuestos); umbral = snapshot por ciclo
            # para el replay N≥50 si más adelante se (des)arma vía env.
            "low_confidence": low_confidence,
            "min_entry_confidence": settings.min_entry_confidence,
            # Opción A: flag = el veto de recompensa mínima OPERÓ este ciclo
            # (sólo BUY/SELL con lote válido); `reward_eur` = snapshot del
            # beneficio del TP al tamaño elegido (None sin entrada
            # propuesta) → replay del gate sin reconstruir la config
            # histórica (patrón min_entry_confidence).
            "reward_below_min": reward_below_min,
            "reward_eur": round(reward_eur, 4) if reward_eur is not None else None,
            "bars_since_close": bars_since_close,
            "steps_in_trade_h1": steps_h1,
            "deliberation": deliberation,
            "mt5_time": mt5_ts,
        },
        cycle_id=cycle_id,
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
async def force_retrain_endpoint(
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(verify_token),
):
    """
    Fuerza un retrain inmediato del modelo PPO (parámetro opcional `symbol`;
    por defecto TRAINING_SYMBOL — P3-3: pipeline por par).
    Útil para when market regime cambia (e.g., central bank intervention).
    """
    sym = symbol or _TRAINING_SYMBOL
    state = _get_retrain_state(sym)
    result = await _force_retrain(symbol=symbol)
    await log_to_audit(
        "retrain_forced",
        {
            "symbol": sym,
            "status": result.get("status"),
            "filled_count": state.filled_count,
            "in_progress": state.retrain_in_progress,
            "last_retrain_time": state.last_retrain_time,
            "outcomes_count": len(state.outcomes),
        },
    )
    return {"status": result["status"], "filled_count": state.filled_count, "in_progress": state.retrain_in_progress}


@router.post("/retrain/approve")
async def approve_retrain_endpoint(
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(verify_token),
):
    """Aprueba el retrain pendiente de `symbol` (umbral alcanzado o calendario
    con retrain_auto_approve=false). `symbol` opcional → TRAINING_SYMBOL.

    No salta el gate de promoción: ese se aplica dentro de _do_retrain.
    """
    result = await _approve_retrain(symbol=symbol)
    await log_to_audit(
        "retrain_approved",
        {
            "symbol": symbol or _TRAINING_SYMBOL,
            "status": result.get("status"),
            "filled_count": result.get("filled_count"),
            "min_trades_for_retrain": result.get("min_trades_for_retrain"),
        },
    )
    return result


def _sole_enabled_symbol() -> Optional[str]:
    """Símbolo del único par habilitado en PAIRS.

    `None` si hay 0 o varios habilitados: la ambigüedad no se resuelve por
    heurística — el caller debe elegir explícitamente.

    Lee `config.settings` en el momento de la llamada (mismo criterio que
    `core.container.build_registry_from_settings`): TODOS los lectores de
    `pairs` comparten la misma fuente canónica.
    """
    from config import settings
    enabled = [p.get("symbol") for p in settings.pairs if p.get("enabled")]
    return enabled[0] if len(enabled) == 1 else None


@router.get("/model/status")
async def model_status(symbol: Optional[str] = None, _: None = Depends(verify_token)):
    """P1-2: qué bundle corre EN MEMORIA vs qué certifica EL DISCO (in_sync).

    P3-1b: responde por par (`?symbol=`); con un único par habilitado se
    resuelve solo. Varios (o cero) pares habilitados sin elegir → 400: la
    ambigüedad no se resuelve por heurística.

    `in_sync=false` = hubo una promoción que no se recargó (o un cambio en disco
    no certificado): el operador ve de inmediato disco ≠ memoria.
    """
    from services.model_registry import load_manifest

    sym = symbol or _sole_enabled_symbol()
    if not sym:
        raise HTTPException(
            status_code=400,
            detail="Varios (o cero) pares habilitados en PAIRS: especifique ?symbol=",
        )
    if _get_registry().get(sym) is None:
        # Observabilidad sí, pero no se lee el disco de un par que no existe:
        # 404 con detalle en vez de un 500 sin explicación.
        raise HTTPException(
            status_code=404,
            detail=f"Par {sym!r} no configurado en PAIRS",
        )

    brain = _BRAINS.get(sym)
    memory = dict(brain.bundle) if brain is not None else {}
    disk = load_manifest(default_model_path(sym))  # RuntimeError si no está certificado
    in_sync = (
        memory.get("model_sha256") == disk["model_sha256"]
        and memory.get("vec_norm_sha256") == disk.get("vec_norm_sha256")
    )
    return {
        "symbol": sym,
        "memory": memory,
        "disk": {
            "model_sha256": disk["model_sha256"],
            "vec_norm_sha256": disk.get("vec_norm_sha256"),
            "source": disk["source"],
            "created_at": disk.get("created_at"),
        },
        "in_sync": in_sync,
    }


@router.get("/model/drift")
async def model_drift(_: None = Depends(verify_token)):
    """P3-2c: PSI de features vivas vs baseline del entreno + decay de métricas.

    Devuelve SIEMPRE el reporte (los fallos parciales quedan reflejados y
    alertados en lugar de romper el endpoint de observabilidad).
    """
    from services.drift_monitor import run_drift_check

    return await run_drift_check()


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

    # El EA (OnTradeTransaction) reporta también el deal de APERTURA:
    # DEAL_ENTRY_IN tiene beneficio 0 por definición y entry==exit (no hay
    # duración). No es un cierre → no se fabrican timestamps ni se persiste.
    # Fix raíz en EA v13.2 (filtra DEAL_ENTRY_IN); este guard protege la BD
    # mientras la versión compilada siga siendo anterior.
    if entry_ts == exit_ts and float(req.pnl) == 0.0 and float(req.pnl_pct) == 0.0:
        logger.warning(
            "[TRADE-FILLED] Reporte de APERTURA ignorado (entry==exit, pnl=0): "
            "symbol=%s entry=%s deal=%s position=%s",
            req.symbol, entry_ts, req.deal_ticket, req.position_id,
        )
        # O-1: la apertura confirma la ejecución → la orden sale del pool de
        # `duplicate_order` (pending/placed → filled). Si este reporte se
        # pierde, la rama de cierre hace la misma transición (idempotente).
        await mark_order_filled(req.symbol, "BUY" if req.direction == "LONG" else "SELL")
        return {"status": "ignored_open_deal"}

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
    # O-1: catch-all de transición — si el reporte de apertura no llegó (el EA
    # v13.2 filtra DEAL_ENTRY_IN), este cierre saca la orden del pool de
    # `duplicate_order`. Idempotente: si la apertura ya la marcó, no matchea.
    await mark_order_filled(req.symbol, "BUY" if direction == "LONG" else "SELL")
    return {"status": "recorded"}

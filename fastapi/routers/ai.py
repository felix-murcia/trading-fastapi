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
    HIGHER_TF_MARKET_FEATURES,
    MARKET_FEATURES,
    TIME_STOP_BARS,
    TREND_FEATURE_WINDOW_MIN,
    TREND_MARKET_FEATURES,
    build_v3_observation,
    decode_v3_direction,
    derive_obs_contract,
    engineer_market_features,
    get_market_features,
    join_higher_tf_channels,
    production_stops,
)
from services.auto_retrain import (
    approve_retrain as _approve_retrain,
    force_retrain as _force_retrain,
    get_state as _get_retrain_state,
    record_trade_filled,
    TRAINING_SYMBOL as _TRAINING_SYMBOL,
)
# T-28 (REQ-1): alineación direccional PPO↔LLM — registro por ciclo + gate.
from services.alignment_metric import pair_state, rolling_alignment
from services.feed_breaker import h4d_breaker_is_open, h4d_breaker_trip
from services.feed_patcher import fill_feed_hole as _fill_feed_hole
from services.market_time import utc_hour_from_candle
from services.quorum import collect_votes, detect_lateral, evaluate
from services.memory_client import build_memory_query, fetch_relevant_memories
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


async def _qwen_veto_authorized(symbol: str, cycle_id: str) -> bool:
    """¿Tiene el LLM autoridad para revertir una señal con su veto direccional?

    T-31 (orden Félix 2026-10-19): la autoridad se gana con SKILL DEMOSTRADA
    en datos reales (services/qwen_authority.py), no por lo que el modelo
    afirme de sí mismo. Sin muestra suficiente o sin skill, el veto queda
    INERTE y la señal pasa — los vetoes mecánicos siguen mandando.

    El motivo se deja SIEMPRE en el log (nunca una supresión silenciosa).
    """
    from services import qwen_authority
    try:
        auth = await qwen_authority.get_authority(symbol)
    except Exception as exc:  # noqa: BLE001 — el veto NUNCA tumba el ciclo
        logger.warning(
            "[%s] VETO BIAS ║ autoridad del LLM no verificable (%s: %s) → SIN veto",
            cycle_id, type(exc).__name__, exc,
        )
        return False
    if auth.get("granted"):
        return True
    logger.warning(
        "[%s] VETO BIAS ║ autoridad del LLM REVOCADA (%s) → SIN veto direccional; "
        "la señal sigue a riesgo (SL/TP/sizing/quality/riesgo intactos)",
        cycle_id, auth.get("reason", "sin veredicto"),
    )
    return False


def _parse_times_utc(series) -> pd.Series:
    """Sellos de vela → datetime UTC, tolerando epoch (s) e ISO-8601.

    El feed entrega ISO ("...Z"); `services.mt5_client` entrega epoch en
    segundos. `pd.to_datetime` sobre epoch SIN `unit` interpreta nanosegundos
    → serie corrupta ⇒ el desplazamiento salía None.
    """
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="s", errors="coerce", utc=True)
    return pd.to_datetime(series, errors="coerce", utc=True)


def _displacement_24h_atr(df: pd.DataFrame) -> Optional[float]:
    """Desplazamiento del precio en 24 h REALES, normalizado por ATR14.

    T-31 (2026-10-19): el ADX mide el ratio de movimiento direccional sobre
    volatilidad, NO cuánto se ha movido el precio — por eso un rango con
    micro-estructura direccional marcaba ADX 33 ("tendencia") con el precio
    inmóvil. Este es el criterio con poder predictivo medido (separación
    +1,8 ATR en el movimiento futuro a +24 h).

    Se mide por TIEMPO (no por nº de barras): la serie H1 tiene huecos de
    fin de semana y pausa diaria ⇒ "24 barras" ≠ 24 h. Sin columna `time`,
    sin cobertura o sin ATR → None (el detector decide sin este dato).
    """
    if "time" not in df.columns or "atr" not in df.columns:
        return None
    try:
        times = _parse_times_utc(df["time"])
        close = df["close"].astype(float)
        atr = float(df["atr"].iloc[-1])
        if times.isna().any() or not (atr > 0):
            return None
        last_t = times.iloc[-1]
        target = last_t - pd.Timedelta(hours=24)
        # último cierre en o antes de (ahora − 24 h)
        mask = times <= target
        if not mask.any():
            return None
        past = float(close[mask].iloc[-1])
        return abs(float(close.iloc[-1]) - past) / atr
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def _adx_higher_tf(res, tf: str, offset_h: Optional[int] = None) -> Optional[float]:
    """ADX normalizado [0,1] del marco superior (H4), o None.

    Se calcula con el MISMO motor de features que el resto del sistema
    (`engineer_market_features`) sobre la serie ya normalizada a UTC. Es la
    confirmación de "no hay tendencia" en el marco que ve el LLM.
    """
    if res is None or isinstance(res, Exception):
        return None
    if getattr(res, "status_code", None) != 200:
        return None
    try:
        data = res.json()
    except ValueError:
        return None
    raw = data if isinstance(data, list) else (data.get("candles") or [])
    if len(raw) < 30:
        return None
    try:
        from ml.trading_env_v2 import engineer_market_features
        raw = _shift_to_utc(raw, f"{tf}", offset_h=offset_h)
        frame = pd.DataFrame(raw)
        if "time" not in frame.columns:
            return None
        feats = engineer_market_features(frame)
        if "adx" not in feats.columns or len(feats) == 0:
            return None
        return float(feats["adx"].iloc[-1])
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def _broker_offset_hours(candles: list) -> int:
    """Desfase broker→UTC deducido de la serie H1 (la única fiable).

    T-31: la detección por «vela formándose» sólo es exacta en H1 — la
    rejilla de 4 h de H4 no es múltiplo del desfase medido (3 h) y la
    vela formándose de H4 puede estar a 2 h de «now» ⇒ la detección sobre
    H4 devolvía +1 h (falso). El desfase es del SERVIDOR, no del marco:
    se deduce de H1 y se aplica a todos.
    """
    from services.market_time import broker_offset_hours_from_candles
    try:
        return broker_offset_hours_from_candles(candles)
    except Exception:  # noqa: BLE001
        return 0


def _shift_to_utc(candles: list, cycle_id: str = "", offset_h: Optional[int] = None) -> list:
    """Normaliza los sellos de vela al UTC real (desfase broker→UTC).

    T-31 (2026-10-19): el servidor MT5 sella las velas con su propio reloj
    (+3 h medidos) marcándolas "Z". Se normalizan a UTC real antes de tocar
    nada más:
      (a) la costura con los CSV de `resultadosBT/` (que son UTC puro)
          deja de tener un salto fantasma de 3 h;
      (b) las marcas de vela quedan comparables con las del EA/DB.
    Corrector auto (services/market_time, P2-3): cero hardcode, sigue solo
    si el broker cambia de huso.

    `offset_h` permite REUTILIZAR el desfase ya deducido de H1 (obligatorio
    para H4/D1, donde la autodetección no es fiable).
    """
    from services.market_time import shift_candle_times
    if offset_h is None:
        offset_h = _broker_offset_hours(candles)
    if not offset_h:
        return candles
    logger.warning(
        "[%s] Sello de velas con desfase broker→UTC %+d h → normalizado a UTC real",
        cycle_id, offset_h,
    )
    return shift_candle_times(candles, offset_h)


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
    bias_veto_fired: bool = False,
    quorum_rejected: bool = False,
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
    # T-31 (2026-10-20): el veto de QUÓRUM (mercado lateral sin consenso)
    # se declara cuando BLOQUEÓ de verdad — antes el HOLD caía en
    # "unknown_veto" o se atribuía a un motivo de Qwen que no había actuado.
    if quorum_rejected:
        return "quorum_veto"
    if final == "HOLD" and qwen.get("regime") == "RANGING":
        return "ranging_veto"
    if final == "HOLD" and qwen.get("quality", 10) < QUALITY_THRESHOLD:
        return "quality_veto"
    # El veto de SESGO sólo se declara si REALMENTE revirtió la decisión.
    # Antes se infería del mero hecho de que el sesgo se opusiera ⇒ un HOLD
    # bloqueado por el quórum o por la falta de autoridad del LLM se
    # registraba como "bias_veto_bullish" (atribución FALSA, medida el 20-10).
    if bias_veto_fired and original == "BUY" and final == "HOLD" and qwen.get("bias") == "BEARISH":
        return "bias_veto_bearish"
    if bias_veto_fired and original == "SELL" and final == "HOLD" and qwen.get("bias") == "BULLISH":
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
    # T-22: bloque de tendencia compartido PPO↔LLM (REQ-1) — True = obs con
    # 20 canales de mercado (27/28: dist_sma200, sma200_slope, adx) y ventana
    # de features ≥ TREND_FEATURE_WINDOW_MIN (SMA200 honesto); False = 17 base
    # y ventana tail(120) byte-idéntica al contrato vivo (20,25).
    trend_features: bool = False
    # T-30: bloque de horizonte lento H4/D1 (31/32: +dist/slope SMA200 de H4
    # y D1) — los canales se UNEN al df desde el fetch P3-4 existente (join
    # asof de vela cerrada); con el fetch degradado (breaker/Unavailable/<205
    # velas) /predict emite HOLD fail-closed, nunca obs de relleno.
    higher_tf_features: bool = False


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

    # Fase 3 / T-22: el ancho del observation_space del bundle decide el
    # contrato (canal de memoria y bloque de tendencia) vía
    # derive_obs_contract — única fuente de verdad con el retrain. Ancho
    # desconocido = RuntimeError: nunca se sirve un contrato que no se
    # construye.
    obs_width = None
    obs_shape = getattr(obs_space, "shape", None)
    if obs_shape:
        obs_width = obs_shape[-1]
    contract = derive_obs_contract(obs_width)
    if contract is None:
        base_width = FOREX_MARKET_FEATURE_COUNT + EXTRA_STATE_FEATURES
        trend_width = base_width + len(TREND_MARKET_FEATURES)
        hf_width = trend_width + len(HIGHER_TF_MARKET_FEATURES)
        raise RuntimeError(
            f"[AI-STARTUP] Contrato de observación desconocido para {symbol}: "
            f"{obs_shape} (esperado {base_width} pre-memoria, {base_width + 1} "
            f"con memoria, {trend_width} trend, {trend_width + 1} trend+memoria, "
            f"{hf_width} T-30 o {hf_width + 1} T-30+memoria)"
        )
    trend_features, higher_tf_features, memory_channel = contract

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
        trend_features=trend_features,
        higher_tf_features=higher_tf_features,
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


def _hf_candles_for_obs(res, symbol: str, tf: str, offset_h: Optional[int] = None) -> Optional[list]:
    """Velas H4/D1 UTILIZABLES para los canales del obs T-30 (o None).

    Paridad REQ-1 de CALIDAD (no sólo de fórmula): el obs sólo ve una serie
    que la línea del prompt también consideraría válida — mismo parche de
    agujero (`_fill_feed_hole`), misma continuidad (gap ≤168 h) y ≥205 velas
    (SMA200 + 5 de margen para la pendiente, barras cerradas incluidas).

    None = no servible (breaker abierto → res None, Exception de fetch,
    HTTP ≠ 200, JSON inválido, serie corta o rota). El caller de T-30 decide
    HOLD — jamás canales de relleno; la línea del prompt degrada sola a
    «Unavailable» (esquema constante intacto).
    """
    if res is None or isinstance(res, Exception):
        return None
    if getattr(res, "status_code", None) != 200:
        return None
    try:
        data = res.json()
    except ValueError:
        return None
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("candles") or []
    else:
        return None
    raw = _shift_to_utc(raw, f"{symbol}:{tf}", offset_h=offset_h)
    raw = _fill_feed_hole(raw, symbol, tf)
    if len(raw) < 205 or not raw or "time" not in raw[0]:
        return None
    times = pd.to_datetime([c.get("time") for c in raw], errors="coerce", utc=True)
    if times.isna().any():
        return None
    gap_h = float(pd.Series(times).sort_values().diff()
                  .dt.total_seconds().div(3600).max())
    if gap_h > 168.0:
        return None
    return raw


def _higher_tf_context_line(tf: str, res, symbol: str, offset_h: Optional[int] = None) -> str:
    """Línea de contexto del marco superior H4/D para el prompt de Qwen.

    Foco ampliado H4/D (Félix 2026-10-12, backlog P2 «Contexto H4/D»): SMA200
    (estructura: posición + pendiente), RSI14 y ADX del timeframe superior —
    el «contexto global» que la Fase 2 nunca cableó (solo `adx` H1) y que el
    pack 1-4 sólo aproximó con un SMA200 DERIVADO de 240 H1.

    `symbol` manda el parche de agujero del feed (`_fill_feed_hole`): si el
    MCP vuelve a cortar la historia, la serie se repara con el CSV del símbolo
    ANTES de las guardas de calidad (orden Félix 2026-10-13, recomendación 2).
    Explícito — jamás por defecto: EURUSD y GBPUSD tienen CSV propios.

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
    if res is None:
        # T-07: breaker abierto → sin fetch. Sin warning: la apertura ya se
        # registró (BREAKER-H4D) y el audit lleva `h4d_context=breaker_open`;
        # warnar en cada ciclo del cooldown haría ruido inútil.
        return f"- {tf}: Unavailable"
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
    # T-31: sellos de vela al UTC real (desfase broker→UTC) antes del parche
    # con los CSV (UTC puro) — si no, la costura arrastra un salto de 3 h.
    # El desfase lo aporta H1 (`offset_h`): la autodetección sobre H4/D1 no
    # es fiable (rejilla de 4 h ≠ múltiplo del desfase de 3 h).
    raw = _shift_to_utc(raw, f"{symbol}:{tf}", offset_h=offset_h)
    # Parche de agujero con CSV (mismo parche del obs H1): si el feed volvió
    # a romperse, la serie se repara ANTES de las guardas de calidad — sin
    # CSV o sin continuidad posterior, "Unavailable" sigue mandando.
    raw = _fill_feed_hole(raw, symbol, tf)
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
        # T-03 (2026-10-15): traza en audit_log ANTES del return. El hueco
        # del CPI 14/10 (12:15→12:40 sin fila) era indistinguible de «EA no
        # llamó» desde BBDD — el warning de log no sobrevive a reinicios de
        # contenedor. Mismas claves que el camino normal para que las
        # queries de ciclos lo cubran sin casos especiales.
        await log_to_audit(
            "ai_predict_cycle",
            {
                "symbol": req.symbol,
                "timeframe": req.timeframe,
                "position": req.position,
                "decision": "HOLD",
                "ml_prob": 0.5,
                "effective_prob": 0.5,
                "eff_prob_raw": None,
                "llm_bias": "NEUTRAL",
                "quality_score": 0.0,
                "quality_reason": f"news_blackout:{news_event}",
                # LR-1: false = nada falló ni se omitió por posición — el
                # motivo es exclusivamente la ventana de noticias.
                "qwen_degraded": False,
                "qwen_skipped": False,
                "confidence_modifier": 1.0,
                "regime": "NEWS_BLACKOUT",
                "volume": None,
                "sl_pips": None,
                "tp_pips": None,
                "latency_ms": round((time.time() - t_start) * 1000, 2),
                "veto": True,
                "veto_reason": "news_blackout",
                "news_blackout": True,
                "news_event": news_event,
                "mt5_time": mt5_ts,
            },
            cycle_id=cycle_id,
        )
        return PredictResponse(
            cycle_id=cycle_id,
            ml_prob=0.5,
            llm_bias="NEUTRAL",
            decision="HOLD",
            quality_reason=f"news_blackout:{news_event}",
        )

    equity_val = 0.0
    candles = []
    # T-07 (2026-10-15): breaker H4/D1 — con el feed wedgado (incidente
    # 14/10) cada petición H4 volvía a tumbar la sesión del MCP. Con el
    # breaker abierto los ciclos del cooldown NO fetchan H4/D1 (la línea
    # del prompt se renderiza «Unavailable», esquema constante) y el
    # servidor queda solo para los críticos equity/H1.
    h4d_skipped = h4d_breaker_is_open()
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
            _tasks = [equity_task, candles_task]
            if not h4d_skipped:
                # Foco ampliado H4/D (Félix 2026-10-12): marcos SUPERIORES para
                # el contexto de Qwen — 250 velas por TF (SMA200 + 5 de margen).
                # Van en el MISMO gather (sin latencia secuencial) y con
                # return_exceptions: su fallo degrada la línea a «Unavailable»
                # (son contexto, no críticos); equity/H1 conservan su camino
                # HOLD. El fallo además ABRE el breaker (T-07, ver más abajo).
                _tasks.append(client.get(
                    f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe=H4&count=250",
                    timeout=3.0,
                ))
                _tasks.append(client.get(
                    f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe=D1&count=250",
                    timeout=3.0,
                ))
            _results = await asyncio.gather(*_tasks, return_exceptions=True)
    except (httpx.ConnectTimeout, httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("[%s] MT5 connection error in predict: %s", cycle_id, exc)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    equity_res, candles_res = _results[0], _results[1]
    if h4d_skipped:
        h4_res = d1_res = None  # breaker en cooldown → «Unavailable», sin fetch
    else:
        h4_res, d1_res = _results[2], _results[3]

    # equity y H1 son CRÍTICOS: el return_exceptions del gather no puede
    # enmascarar su tratamiento de siempre (mismo camino HOLD / re-lanzado).
    for _critico in (equity_res, candles_res):
        if isinstance(_critico, Exception):
            if isinstance(_critico, httpx.HTTPError):
                logger.warning("[%s] MT5 connection error in predict: %s", cycle_id, _critico)
                return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
            raise _critico

    # T-07: el PRIMER fallo de fetch H4/D1 abre el breaker — durante el
    # cooldown los ciclos no los fetchan y el MCP deja de recibir el golpe
    # (semántica half-open: sonda única al expirar; fallo de la sonda =
    # reabre otro cooldown). La línea del prompt ya degrada sola a
    # «Unavailable» con res Exception/None (esquema constante intacto).
    for _h4d_tf, _h4d_res in (("H4", h4_res), ("D1", d1_res)):
        if isinstance(_h4d_res, Exception):
            h4d_breaker_trip(f"{_h4d_tf}: {type(_h4d_res).__name__}: {str(_h4d_res)[:80]}")
            logger.warning(
                "[%s] BREAKER-H4D abierto %d min (%s) — los ciclos del "
                "cooldown omiten H4/D1 (equity/H1 intactos)",
                cycle_id, settings.h4d_breaker_cooldown_minutes, _h4d_tf,
            )
            break
    if h4d_skipped:
        h4d_context = "breaker_open"
    elif isinstance(h4_res, Exception) or isinstance(d1_res, Exception):
        h4d_context = "unavailable"
    else:
        h4d_context = "ok"

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
    # T-31 (2026-10-19): el feed sella las velas con el reloj del BROKER
    # (+3 h medidos). Se normalizan a UTC REAL antes de tocar nada más:
    #  (a) la costura con los CSV de `resultadosBT/` (que son UTC puro)
    #      deja de tener un salto fantasma de 3 h;
    #  (b) las marcas de vela quedan comparables con las del EA/DB.
    # El desfase se deduce de H1 (única serie fiable) y se REUTILIZA para
    # H4/D1 (su autodetección no es fiable: rejilla de 4 h vs desfase 3 h).
    _offset_h = _broker_offset_hours(candles)
    candles = _shift_to_utc(candles, cycle_id, offset_h=_offset_h)
    # Parche del agujero del feed (Félix 2026-10-12): si el MCP corta la
    # historia (salto >168 h), el bloque viejo se sustituye por los CSV de
    # `resultadosBT/` (2026 contiguo) + el día vivo del MCP. El obs deja de
    # mezclar dic-2025 con hoy (salto fantasma −4.72 % dentro de la ventana).
    candles = _fill_feed_hole(candles, req.symbol, "H1")

    raw_df = pd.DataFrame(candles)
    if raw_df.empty or len(raw_df) < 30:
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    # T-22: el brain se resuelve ANTES de elegir la ventana de features — el
    # contrato del bundle (trend_features) decide cuántas velas alimentan el
    # obs. (Antes vivía tras el contexto de Qwen; nada entre aquí y su sitio
    # anterior depende del brain.)
    brain = _BRAINS.get(sym)
    if brain is None:
        load_inference_artifacts(sym)
        brain = _BRAINS.get(sym)
    if brain is None:
        logger.error("[%s] No hay modelo PPO disponible para %s.", cycle_id, sym)
        return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")

    # Paquete 1-4 / T-22: la serie COMPLETA (hasta 240 velas) alimenta el
    # contexto de estructura (SMA200 del prompt); las features del MODELO se
    # calculan sobre una ventana FIJA — tail(120) con el contrato vivo
    # (byte-idéntico al contrato de siempre) o tail(TREND_FEATURE_WINDOW_MIN)
    # con contrato trend (SMA200 honesto en las últimas 20 velas del obs).
    # Sin columna `time` → previo (sin orden fiable no hay SMA200 honesto) y,
    # con contrato trend, HOLD: jamás se sirve tendencia de relleno.
    if "time" in raw_df.columns:
        raw_sorted = raw_df.sort_values("time").reset_index(drop=True)
        sma200_dist, sma200_slope = _sma200_context(raw_sorted)
        if brain.trend_features:
            if len(raw_sorted) < TREND_FEATURE_WINDOW_MIN:
                logger.error(
                    "[%s] T-22: %d velas H1 insuficientes para el contrato "
                    "trend (>= %d: warm-up SMA200 + ventana del obs) → HOLD",
                    cycle_id, len(raw_sorted), TREND_FEATURE_WINDOW_MIN,
                )
                return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
            # Ventana FIJA = el guard (224): con cualquier profundidad de
            # fetch ≥224 las features se calculan sobre las MISMAS 224 velas
            # recientes → obs byte-idéntico (el EWM del macd tiene memoria
            # infinita: con ventanas de distinto tamaño sus ULPs divergen).
            # Sus últimas 20 velas (filas 204..223) tienen SMA200 real.
            features_input = raw_sorted.tail(TREND_FEATURE_WINDOW_MIN)
        else:
            features_input = raw_sorted.tail(120)
    else:
        sma200_dist, sma200_slope = None, None
        if brain.trend_features:
            logger.error(
                "[%s] T-22: serie H1 sin columna `time` — sin orden no hay "
                "SMA200 honesto para el contrato trend → HOLD",
                cycle_id,
            )
            return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
        features_input = raw_df

    df = engineer_market_features(features_input)
    # T-30 (orden Félix 2026-10-17): los 4 canales del horizonte lento se
    # unen desde el MISMO fetch P3-4 que alimenta el prompt del LLM (cero
    # HTTP nuevo). Serie no servible (breaker, <205 velas, gap >168 h) o
    # ventana del obs sin canales formados → HOLD fail-closed: un obs de
    # horizonte lento en cero/relleno sería «sin tendencia» = mentira de
    # estado (pre-registrado §7.4). Para el contrato vivo (20,25) este bloque
    # no se ejecuta: byte-idéntico.
    if brain.higher_tf_features:
        h4_obs = _hf_candles_for_obs(h4_res, req.symbol, "H4", offset_h=_offset_h)
        d1_obs = _hf_candles_for_obs(d1_res, req.symbol, "D1", offset_h=_offset_h)
        if h4_obs is None or d1_obs is None:
            logger.error(
                "[%s] T-30: H4/D1 no servibles para los canales del obs "
                "(breaker=%s h4=%s d1=%s) → HOLD",
                cycle_id, h4d_skipped,
                "ok" if h4_obs else "no_servible",
                "ok" if d1_obs else "no_servible",
            )
            return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
        try:
            df = join_higher_tf_channels(df, h4_obs, d1_obs)
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("[%s] T-30: join H4/D1 imposible (%s: %s) → HOLD",
                         cycle_id, type(exc).__name__, exc)
            return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
        hf_window = df[list(HIGHER_TF_MARKET_FEATURES)].iloc[-20:]
        hf_bad = int(hf_window.isna().any(axis=1).sum())
        if hf_bad:
            logger.error(
                "[%s] T-30: %d/20 filas del obs sin canales H4/D1 formados "
                "(cobertura de vela cerrada corta) → HOLD",
                cycle_id, hf_bad,
            )
            return PredictResponse(cycle_id=cycle_id, ml_prob=0.5, llm_bias="ERROR", decision="HOLD")
    features_list = get_market_features(
        df, include_trend=brain.trend_features,
        include_higher_tf=brain.higher_tf_features,
    )
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
        f"{_higher_tf_context_line('H4', h4_res, req.symbol, offset_h=_offset_h)}\n"
        f"{_higher_tf_context_line('D1', d1_res, req.symbol, offset_h=_offset_h)}"
    )

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

    # ── LR-1 (Félix 2026-10-14): SKIP DE QWEN CON POSICIÓN ABIERTA ──────────
    # Con `env_position != 0` el juicio del LLM es decisionalmente neutro:
    # (a) las entradas BUY/SELL están vetadas por `position_rejected`
    # (backend, autoridad absoluta en `_veto_reason`) Y por el auto-rechazo
    # del EA (`rejected_position_open`); (b) la ventana de re-entrada exige
    # `env_position == 0`; (c) el CLOSE del PPO (dead-zone K=2) y el
    # time-stop pasan la cadena SIN veto LLM. La llamada al 8B (~40 s)
    # sólo consumía GPU: 769/1303 ciclos (59 %) de los 7 días previos.
    # Flag `QWEN_SKIP_WHILE_OPEN` (default true; rollback por env sin
    # redeploy). Efectos del stub neutro:
    #   - contadores de salud NO avanzan (ni ok ni failed): la función
    #     query_qwen_unified no se invoca,
    #   - estado anti-parrota intacto (no se actualiza durante el skip),
    #   - audit marca `qwen_skipped=true` + `quality_reason` explícito
    #     (nada degradado: `qwen_degraded` queda false),
    #   - veto de calidad NO evalúa con la quality sintética 0.0 (guard
    #     `not qwen_skipped`) — la atribución real (position_veto/time_stop)
    #     manda en `_veto_reason` y el log no se llena de falsos VETO CALIDAD.
    qwen_skipped = bool(settings.qwen_skip_while_open) and env_position != 0
    reentry: Optional[dict] = None
    # T-10: traza de memoria — inicializada AQUÍ (antes del if/else) para que
    # el payload de audit tenga la clave también en la rama skip LR-1 (null:
    # sin fetch ni inyección — no hay prompt que enriquecer).
    memory_audit: Optional[dict] = None
    if qwen_skipped:
        logger.info(
            "[%s] QWEN-SKIP ║ posición abierta → juicio LLM neutro omitido; "
            "PPO+dead-zone+time-stop siguen activos",
            cycle_id,
        )
        qwen = {
            "ok": True,
            "abstained": False,
            "quality": 0.0,
            "reason": "position_open: qwen skipped",
            "bias": "NEUTRAL",
            "bias_prob": 0.5,
            "confidence": 0.0,
            "confidence_modifier": 1.0,
            "regime": "SKIPPED_POSITION_OPEN",
            "reentry_ok": None,
            "effective_prob": ml_prob,
        }
    else:
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
        # ascendente y tope 3 (presupuesto de tokens). `None` con el filtro
        # de noticias deshabilitado (el prompt lo dice explícitamente). Fetch
        # DESPUÉS del bloque MT5: httpx está garantizado parcheado en suite.
        calendar_events: Optional[list[dict]] = None
        if settings.news_filter_enabled:
            upcoming = await get_upcoming_news([req.symbol])
            calendar_events = [
                ev for ev in upcoming
                if -120.0 <= float(ev["minutes_until"]) <= 240.0
            ]
            calendar_events.sort(key=lambda ev: float(ev["minutes_until"]))
            calendar_events = calendar_events[:3]

        # T-10 (Félix 2026-10-15): MEMORIA A LARGO PLAZO — inyección
        # determinista (memory como dato, no como tool; el proxy :8090 nunca
        # toca este camino). Sólo con llamada real a Qwen (el skip LR-1 no
        # fetcha: no hay prompt que enriquecer). shadow = fetch + candidatos
        # trazados en audit con CERO inyección (patrón estratega D-5: se arma
        # con evidencia de N días, no con diseño). Fallo de memory_api → []
        # con WARNING (enriquecedor, nunca crítico — misma clase que H4/D1).
        memories: Optional[list[dict]] = None
        if settings.memory_injection_mode != "off":
            _memory_q = build_memory_query(
                symbol=req.symbol,
                decision=decision,
                rsi=float(df['rsi14'].iloc[-1]) * 100.0,
                adx=float(df['adx'].iloc[-1]) if 'adx' in df.columns else None,
                hour=utc_hour_from_candle(pd.to_datetime(df['time'].iloc[-1])),
            )
            _mem_cands = await fetch_relevant_memories(_memory_q, req.symbol)
            logger.info("[%s] MEMORY query=%r | n=%d top_score=%s",
                        cycle_id, _memory_q, len(_mem_cands),
                        (round(_mem_cands[0]["score"],4) if _mem_cands else None))

            _mem_inject = settings.memory_injection_mode == "on" and bool(_mem_cands)
            if _mem_inject:
                memories = _mem_cands
            memory_audit = {
                "mode": settings.memory_injection_mode,
                "candidates": len(_mem_cands),
                "injected": _mem_inject,
                # Sólo la distancia del más cercano: la lista completa vive
                # en QWEN-PROMPT cuando se inyecta (traza del prompt).
                "top_distance": (
                    round(_mem_cands[0]["score"], 4) if _mem_cands else None
                ),
            }

        # R1: contexto de re-entrada para el juicio del cerebro — sólo
        # dentro de la ventana y con entrada propuesta (lo único que el veto
        # puede bloquear); así la consulta extra al último cierre ocurre
        # únicamente cuando su resultado puede cambiar la decisión.
        # `last_exit=None` (cierre fuera de la ventana de velas) → el prompt
        # instruye juzgar sólo con las velas.
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
            # T-10: memorias a largo plazo ([]/None = prompt byte-idéntico)
            memories=memories,
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
    bias_veto_fired = False
    if final_decision == "BUY" and qwen["bias"] == "BEARISH" and qwen.get("bias_prob", 0.5) <= 0.3:
        # T-31: el veto direccional sólo tiene autoridad si el juez la ha
        # demostrado con datos reales (services/qwen_authority.py). Un juez
        # que dice «0.85» y acierta el 22 % NO puede revertir señales.
        if not await _qwen_veto_authorized(sym, cycle_id):
            pass  # sin autoridad → la señal pasa (log explícito en el helper)
        else:
            logger.warning("[%s] VETO BIAS ║ BUY revertido por LLM BEARISH (conf=%.2f)", cycle_id, qwen.get("bias_prob", 0.5))
            final_decision = "HOLD"
            bias_veto_fired = True
    elif final_decision == "SELL" and qwen["bias"] == "BULLISH" and qwen.get("bias_prob", 0.5) >= 0.7:
        if not await _qwen_veto_authorized(sym, cycle_id):
            pass  # sin autoridad → la señal pasa (log explícito en el helper)
        else:
            logger.warning("[%s] VETO BIAS ║ SELL revertido por LLM BULLISH (conf=%.2f)", cycle_id, qwen.get("bias_prob", 0.5))
            final_decision = "HOLD"
            bias_veto_fired = True

    # T-31 (orden Félix 2026-10-19) — QUÓRUM ENTRE MODELOS EN MERCADO
    # LATERAL. En rango, una entrada direccional sostenida por un solo
    # modelo es una moneda al aire (el PPO se entrenó en un contexto
    # tendencial y su edge en lateral NO está demostrado). El régimen se
    # detecta de forma DETERMINISTA (ADX + bb_pos/atr_pct/macd_hist) — nunca
    # con la opinión del LLM (su campo `regime` es una salida copiada por
    # plantilla, RCA T-31). Fuentes de voto independientes: PPO, LLM (sólo
    # con evidencia) y estructura SMA200. Se exigen 2 de 3 coincidentes.
    # En régimen NO lateral el quorum NO se aplica (cero cambios de
    # comportamiento fuera del lateral).
    quorum_audit: Optional[dict] = None
    quorum_rejected = False
    if decision in ("BUY", "SELL"):
        _adx_val = float(df['adx'].iloc[-1]) if 'adx' in df.columns else None
        # T-31: desplazamiento por TIEMPO (24 h reales, no 24 barras: la
        # serie tiene huecos de fin de semana) normalizado por ATR14.
        _disp24_atr = _displacement_24h_atr(df)
        # Confirmación del marco superior: el ADX de H4 (el marco que ve el
        # LLM). Se calcula sobre la serie H4 ya normalizada a UTC.
        _adx_h4 = _adx_higher_tf(h4_res, "H4", offset_h=_offset_h)
        # Pendiente de la estructura H1 en RATIO (ΔSMA200/close) — la misma
        # escala que REWARD_V2_S0_SLOPE (0,0002) del env. `sma200_slope` viene
        # como diferencia ABSOLUTA de precio (routers._sma200_context).
        _slope_ratio = (sma200_slope / current_price) if (
            sma200_slope is not None and current_price) else None
        _dist_ratio = (sma200_dist / current_price) if (
            sma200_dist is not None and current_price) else None
        _lateral = detect_lateral(
            bb_pos=float(df['bb_pos'].iloc[-1]) if 'bb_pos' in df.columns else None,
            atr_pct=(current_atr / current_price) if current_price else None,
            macd_hist=float(df['macd_hist'].iloc[-1]) if 'macd_hist' in df.columns else None,
            adx=_adx_val,
            disp24_atr=_disp24_atr,
            adx_h4=_adx_h4,
            slope_h1_ratio=_slope_ratio,
        )
        if _lateral:
            _collected = collect_votes(
                ppo_direction=decision,
                llm_bias=qwen.get("bias"),
                llm_bias_prob=qwen.get("bias_prob"),
                llm_abstained=bool(qwen.get("abstained", False)),
                sma200_dist=_dist_ratio,
                sma200_slope=_slope_ratio,
            )
            _verdict = evaluate(_collected, direction=decision)
            quorum_audit = {
                "lateral": True,
                "granted": _verdict["granted"],
                "direction": _verdict["direction"],
                "votes": _verdict["votes"],
                "tally": _verdict["tally"],
                "required": _verdict["required"],
                "reason": _verdict["reason"],
                "adx_h1": _adx_val,
                "adx_h4": _adx_h4,
                "disp24_atr": _disp24_atr,
                "slope_h1_ratio": _slope_ratio,
            }
            # Sólo bloquea si la decisión final sigue siendo direccional (si
            # un veto previo ya la dejó en HOLD, el quórum se declara pero no
            # añade nada).
            if final_decision in ("BUY", "SELL") and not _verdict["granted"]:
                logger.warning(
                    "[%s] QUÓRUM ║ mercado lateral sin consenso (%s) → HOLD",
                    cycle_id, _verdict["reason"],
                )
                final_decision = "HOLD"
                quorum_rejected = True
        else:
            # T-31: la trazabilidad del veredicto — aunque NO sea lateral se
            # declaran las ENTRADAS del detector (si no, un "no lateral" sin
            # números es indescifrable: fue la duda que Surge el 19-10).
            quorum_audit = {
                "lateral": False,
                "granted": None,
                "reason": "régimen no lateral: quorum no aplica",
                "adx_h1": _adx_val,
                "adx_h4": _adx_h4,
                "disp24_atr": _disp24_atr,
                "slope_h1_ratio": _slope_ratio,
            }

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
        bias_veto_fired=bias_veto_fired,
        quorum_rejected=quorum_rejected,
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

    # ── T-28 (REQ-1, 2026-10-17): ALINEACIÓN PPO↔LLM POR CICLO ─────────────
    # Registro por ciclo en audit (acceptance (b); pre-registrado D5/D6):
    # `ppo_direction` = decisión CRUDA del PPO (tras histeresis K=2, ANTES de
    # la cadena de vetos — lo que el LLM debe compartir), `pair_state` =
    # clasificación del par de ESTE ciclo y `alignment_rolling` = reporte T-28
    # sobre la ventana PREVIA (patrón snapshot de min_entry_confidence:
    # reproducible sin recompute; el ciclo actual se inserta después).
    # Capa observacional → contención como en la deliberación: su fallo NO
    # tumba el ciclo; el error queda EXPLÍCITO en el payload (nada silencioso).
    t28_pair = pair_state(decision, qwen["bias"])
    # T-31: snapshot de la autoridad del veto (medida en vivo, cacheada) — la
    # auditoría deja constancia de si el LLM tenía o no poder de veto.
    try:
        from services import qwen_authority as _qa
        _auth = _qa.peek(req.symbol)
        qwen_authority_snapshot = (
            {
                "granted": bool(_auth.get("granted")),
                "n": _auth.get("n"),
                "acc": _auth.get("acc"),
                # Límite inferior de Wilson: la skill es "demostrada" sólo si
                # se distingue del azar (T-31, fix 2026-10-20).
                "wilson_lower": _auth.get("wilson_lower"),
                "reason": _auth.get("reason"),
                # Curva de calibración por bucket de bias_prob (T-31): es la
                # medida que responde «¿conf 0,75 acierta más que 0,60?».
                "calibration": _auth.get("calibration") or {},
            }
            if _auth
            else {"granted": False, "n": 0, "acc": None, "reason": "sin medir",
                  "calibration": {}}
        )
    except Exception as exc:  # noqa: BLE001 — telemetría NUNCA tumba el ciclo
        logger.warning(
            "[%s] T-31 snapshot de autoridad falló (%s: %s) → declarado en audit",
            cycle_id, type(exc).__name__, exc,
        )
        qwen_authority_snapshot = {"granted": False, "n": 0, "acc": None, "reason": "error"}
    try:
        t28_rolling = await rolling_alignment(req.symbol)
    except Exception as exc:
        logger.warning(
            "[%s] T-28 rolling alignment falló (%s: %s) → error declarado en audit",
            cycle_id, type(exc).__name__, exc,
        )
        t28_rolling = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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
            # T-05 (2026-10-15): producto sin recortar (ver qwen_client) —
            # None en degradados/skip (no hay juicio que saturar).
            "eff_prob_raw": (
                round(qwen["effective_prob_raw"], 4)
                if qwen.get("effective_prob_raw") is not None
                else None
            ),
            "llm_bias": qwen["bias"],
            # T-31: probabilidad direccional real del LLM (0-1) — permite
            # medir su calibración ex-post (Fase A: exactitud por bucket).
            "llm_bias_prob": round(qwen.get("bias_prob", 0.5), 3),
            # T-31: autoridad del veto direccional (medida en vivo): el veto
            # solo revierte con skill demostrada. `granted=false` = el juez
            # no ha acreditado skill → su sesgo NO revierte señales.
            "qwen_veto_authority": qwen_authority_snapshot,
            # T-28 (REQ-1, 2026-10-17): acuerdo direccional PPO↔LLM por ciclo —
            # `decision` (arriba) es la FINAL del ciclo; `ppo_direction` es la
            # cruda del PPO. `pair_state` clasifica este par; `alignment_rolling`
            # es el reporte de la ventana previa (o {"error": ...} explícito).
            "ppo_direction": decision,
            "pair_state": t28_pair,
            "alignment_rolling": t28_rolling,
            "quality_score": round(qwen["quality"], 2),
            "quality_reason": qwen["reason"],
            "qwen_degraded": not qwen.get("ok", True),
            # T-31: abstención declarada — el LLM respondió sin evidencia
            # (razón sin número del contexto) o repitió plantilla. NO vota:
            # el PPO decide con los controles de riesgo intactos.
            "qwen_abstained": bool(qwen.get("abstained", False)),
            # T-31: quórum entre modelos en mercado lateral (PPO + LLM con
            # evidencia + estructura SMA200). `null` = régimen no lateral
            # (el quorum no aplica); `granted=false` = HOLD por falta de
            # consenso entre fuentes independientes.
            "quorum": quorum_audit,
            # LR-1 (2026-10-14): true = el juicio LLM se omitió a propósito
            # (posición abierta, decisiónalmente neutro) — distingue el
            # skip deliberado de cualquier degradación real (`qwen_degraded`
            # queda false: nada falló).
            "qwen_skipped": qwen_skipped,
            "confidence_modifier": round(qwen["confidence_modifier"], 2),
            "regime": qwen["regime"],
            # T-07: estado del feed de marcos superiores en este ciclo —
            # "ok" (fetch respondió) | "unavailable" (fetch falló → breaker
            # abierto) | "breaker_open" (omitido por cooldown del breaker).
            "h4d_context": h4d_context,
            # T-10: memoria a largo plazo en este ciclo — None (off / skip
            # LR-1 sin fetch) | {mode, candidates, injected, top_distance}.
            # En modo `on` la lista completa de memorias inyectadas vive en
            # el log QWEN-PROMPT (traza del prompt, fuente única de verdad).
            "memory": memory_audit,
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

    # T-31/Promo (2026-10-19): el decodificador v3 devuelve "FLAT" cuando la
    # cabeza de dirección cae en el dead-band y NO hay posición (semántica:
    # "no hacer nada"). Pero el CONTRATO de la respuesta del backend (y lo que
    # el EA entiende) es CLOSE/HOLD/BUY/SELL: el EA v13.8 no tiene catch-all
    # para un valor desconocido ⇒ con "FLAT" el ciclo terminaba SIN orden y
    # SIN reportar (silencioso). El bundle promovido (4cd9abb6) cae en el
    # dead-band mucho más (16 operaciones en el holdout vs 137 del anterior),
    # así que "FLAT" pasó de raro a frecuente ⇒ hay que normalizarlo.
    # En la RESPUESTA: FLAT → HOLD (equivalente exacto: no hacer nada).
    # En la AUDITORÍA se conserva el valor CRUDO ("FLAT") porque la métrica
    # T-28 lo interpreta como señal neutra (polarity FLAT = 0) y el ALIGNMENT
    # debe seguir viendo lo que el modelo decidió de verdad.
    _ea_decision = "HOLD" if final_decision == "FLAT" else final_decision
    if _ea_decision != final_decision:
        logger.info(
            "[%s] Decisión FLAT (dead-band sin posición) → HOLD en la respuesta "
            "(contrato del EA: CLOSE/HOLD/BUY/SELL); la auditoría guarda el crudo",
            cycle_id,
        )
    return PredictResponse(
        cycle_id=cycle_id,
        ml_prob=round(ml_prob, 4),
        llm_bias=qwen["bias"],
        decision=_ea_decision,
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


@router.get("/cross-audit")
async def cross_audit_endpoint(symbol: Optional[str] = Query(default=None)):
    """AUDITORÍA CRUZADA (P2) — recomputa INDEPENDIENTEMENTE los números
    publicados y compara. Sólo lectura: no muta el trading.

    Es la red de seguridad del requisito «fiabilidad absoluta»: el 20-10 tres
    mediciones de las MISMAS llamadas dieron 87 %, 69,6 % y 10 % por fallos
    del MEDIDOR. Con esta comprobación, un error así se ve en minutos.
    """
    from services.cross_audit import run_cross_audit

    sym = (symbol or _TRAINING_SYMBOL).upper()
    return await run_cross_audit(sym)


@router.get("/ledger")
async def ledger_endpoint(symbol: Optional[str] = Query(default=None)):
    """LEDGER DE FIABILIDAD (L4) — el estado del mundo con la vara correcta.

    Por cada fuente: métricas de TRADING OOS (la vara que decide), track
    record vivo, skill de clasificación (diagnóstico) con n y CI, y su
    ADMISIÓN con el motivo. Sin token: es un informe de estado, no un
    disparador (no muta nada).

    Orden Félix 2026-10-19/20: «lo no medido no tiene poder» y «nada que
    quede al azar» ⇒ aquí se ve, con números, qué sabe el sistema de cada
    fuente y quién tiene derecho a decidir.
    """
    from services.ledger import build_ledger

    sym = (symbol or _TRAINING_SYMBOL).upper()
    return await build_ledger(sym)


@router.post("/retrain/promote-candidate")
async def promote_candidate_endpoint(
    exp_id: int = Query(...),
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(verify_token),
):
    """Promueve un CANDIDATO YA MEDIDO (artefactos de un experimento cerrado).

    Orden Félix 2026-10-19: *«quiero que cuando el candidato sea
    drásticamente mejor que el modelo vivo, se promocione el drásticamente
    mejor»*.

    NO se salta la regla: el informe del experimento se vuelve a evaluar con
    `services/promotion_rule.py` (mismos 8 criterios congelados) y sólo se
    promueve si la regla lo concede. Devuelve el detalle de cada criterio
    para que la decisión sea auditable.
    """
    from services.candidate_promotion import CandidatePromotionError, promote_measured_candidate

    sym = (symbol or _TRAINING_SYMBOL).upper()
    try:
        result = await promote_measured_candidate(sym, exp_id)
    except CandidatePromotionError as exc:
        logger.error("[PROMO-CAND] exp%d NO promoted: %s", exp_id, exc)
        await log_to_audit("candidate_promotion_failed", {"symbol": sym, "exp_id": exp_id, "error": str(exc)})
        return {"promoted": False, "exp_id": exp_id, "error": str(exc)}
    await log_to_audit(
        "candidate_promoted" if result.get("promoted") else "candidate_promotion_rejected",
        {
            "symbol": sym,
            "exp_id": exp_id,
            "promoted": result.get("promoted"),
            "reason": (result.get("promotion_path") or {}).get("reason"),
            "path": (result.get("promotion_path") or {}).get("path"),
            "checks": ((result.get("promotion_path") or {}).get("drastic") or {}).get("checks"),
            "model_sha256": result.get("model_sha256"),
        },
    )
    return result


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

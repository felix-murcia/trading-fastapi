"""
Auto-Retraining Service — reentrena PPO cada N trades completados.

Funciona así:
1. Cada vez que una orden cambia a 'filled', incrementamos un contador en memoria
2. Cuando el contador alcanza TRADES_BEFORE_RETRAIN, disparamos retrain
3. El retrain usa los últimos datos de velas + feedback de rendimiento
4. Promueve el BUNDLE (zip + VecNormalize + manifest) y reinicia el contador de ciclo
"""
import asyncio
import logging
import json
import os
import pickle
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import ClassVar, Optional

import numpy as np
import pandas as pd

from config import settings
from core.pair import default_model_path
from core.rules import is_winning_close
from services import data_snapshot, drift_monitor, experiment_tracker
from services.alerting import AlertLevel, send_alert
# T-28 (REQ-1, c): condición de alineación PPO↔LLM del gate de retrain —
# pre-registrada en audits/t28-alignment-metric-2026-10-17.md (D6).
from services.alignment_metric import alignment_gate
from services.walk_forward import (
    build_walk_forward_split,
    evaluate_walk_forward,
    gate_walk_forward,
)

logger = logging.getLogger(__name__)

# P3-1b/P3-3: símbolo POR DEFECTO del pipeline de retrain. El pipeline está
# parametrizado por símbolo (P3-3): cada par entrena su propio bundle; este
# es el default de force/approve/scheduled cuando el caller no elige mundo.
TRAINING_SYMBOL = "EURUSD"
# P3-1c: el bundle que escribe cada corrida es SIEMPRE el de SU mundo — la
# ruta se deriva de core.pair (convención única de rutas, nada hardcodeado).
MODEL_PATH = default_model_path(TRAINING_SYMBOL)
RETRAIN_LOCK = asyncio.Lock()   # global: retrains de todos los pares serializados
SCHEDULE_CHECK_S = 3600   # P3-2d: el calendario se revisa cada hora


def _validate_symbol(symbol: str) -> str:
    """`symbol` debe estar listado en PAIRS para tener pipeline de retrain.

    Listado basta (habilitado o NO): entrenar el canario ANTES de activarlo
    es el flujo P3-3. Símbolo desconocido → ValueError explícito — jamás se
    inventa un mundo.
    """
    listed = {
        spec.get("symbol") for spec in settings.pairs
        if isinstance(spec, dict)
    }
    if symbol not in listed:
        raise ValueError(
            f"[RETRAIN] símbolo {symbol!r} no listado en PAIRS — sin pipeline "
            f"de retrain (listar el par primero, aunque esté enabled=false)"
        )
    return symbol


@dataclass
class RetrainConfig:
    trades_before_retrain: int = 10       # ← 50 era demasiado: 10 permite aprender rápido
    min_trades_for_retrain: int = 5      # ← mínimo razonable tras primer trade
    lookback_candles: int = 500           # Velas históricas a usar para retrain
    # P3-3: lookback POR PAR — el entrenamiento de un par necesita años de
    # H1 (walk-forward con masa estadística para el gate). Símbolos sin
    # entrada → lookback_candles. P0 G-2 (plan 2026-09-30): EURUSD sube de
    # 500 a 30000 — con 481 velas los pliegues eran de 19 barras y el gate
    # decidía sobre ruido (promoción de las 03:14 del 30 sep).
    # ClassVar: atributo de CLASE (RetrainConfig se usa sin instanciar).
    lookback_by_symbol: ClassVar[dict] = {"GBPUSD": 30000, "EURUSD": 30000}
    retrain_window_size: int = 20         # Window size del entorno
    initial_balance: float = 1000.0
    commission: float = 0.0              # comisión bróker €/lot/lado — O-2 (2026-10-01):
                                         # 271/271 deals sep→oct commission=0 y fee=0 → 0.0.
                                         # (era 0.0001 = convención fraccional v1; alimentaba
                                         # el campo €/lot de v2 → bug de unidades)
    churn_friction_per_lot: float = 7.0  # ancla anti-churn (decisión Félix "fricción completa";
                                         # plan: fricción 7 €/lot) — parámetro de política,
                                         # NO coste de bróker (separado desde O-2)
    reward_version: str = "v1"           # T-27 REVERTIDO (2026-10-17, orden «revert T-27»):
                                         # el gate del retrain único cayó (3/6) ⇒ el candidato
                                         # «Sat-Align» queda invalidado y el default vuelve a
                                         # "v1" byte-idéntico (antes: flip pre-registrado a
                                         # "v2_align"). Eval y train siguen compartiendo la
                                         # MISMA versión (env_cfg → _evaluate_model los copia
                                         # a ambos). Serve no lee reward_version.
    n_epochs: int = 3                    # Epochs de entrenamiento (compatibilidad/auditoría)
    total_timesteps: int = 100_000      # PPO steps por retrain; 1.500 era insuficiente
    learning_rate: float = 3e-4
    verbose: int = 0
    real_outcome_weight: float = 0.3      # Peso del feedback real vs sintético (0=ignorar, 1=dominante)
    min_validation_openings: int = 20     # P0 G-1: masa mínima de aperturas OOS (1 dejaba promover con 8)
    walk_forward_folds: int = 4           # P1-1: ≥4 pliegues OOS (exigencia de auditoría)
    walk_forward_train_frac: float = 0.70
    walk_forward_holdout_frac: float = 0.10  # holdout congelado: reporta, nunca decide
    walk_forward_min_fold_bars: int = 10  # pliegue más corto evaluable (si no, aborta)

    @classmethod
    def lookback_for(cls, symbol: str) -> int:
        """Velas históricas a pedir para el retrain de `symbol` (P3-3)."""
        return int(cls.lookback_by_symbol.get(symbol, cls.lookback_candles))


@dataclass
class TradeOutcome:
    """Resultado de un trade para retroalimentación."""
    symbol: str
    entry_time: float
    exit_time: float
    pnl: float          # USD positivo = gain, negativo = loss
    pnl_pct: float     # % del balance en el momento del entry
    direction: str      # "LONG" or "SHORT"
    sl_hit: bool
    tp_hit: bool
    exit_reason: str    # "sl", "tp", "manual", "news"


@dataclass
class RetrainState:
    filled_count: int = 0
    last_retrain_time: float = 0.0
    outcomes: list = field(default_factory=list)
    retrain_in_progress: bool = False
    # P3-2d: retrain programado por tiempo (calendario)
    scheduled_due: bool = False     # el calendario marca retrain (pendiente si auto_approve=false)
    schedule_anchor: float = 0.0    # ancla inicial cuando nunca hubo intento


def _parse_bool(value, default: bool = False) -> bool:
    """jsonb puede devolver bool, número o string según el escritor."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _retrain_state_row_key(key: str, symbol: str = TRAINING_SYMBOL) -> str:
    """P3-1c: namespace de claves POR PAR — `retrain_state.<SYMBOL>.<campo>`.

    Los contadores de un mundo nunca alimentan el disparo de otro.
    """
    return f"retrain_state.{symbol}.{key}"


async def _load_retrain_state(symbol: str = TRAINING_SYMBOL) -> RetrainState:
    from db.connection import get_pool

    pool = get_pool()
    keys = [
        _retrain_state_row_key(k, symbol)
        for k in ("filled_count", "last_retrain_time", "scheduled_due", "schedule_anchor")
    ]
    rows = await pool.fetch(
        "SELECT key, value FROM retrain_state WHERE key = ANY($1::text[])",
        keys,
    )
    payload = {row["key"]: row["value"] for row in rows}
    state = RetrainState(
        filled_count=int(payload.get(_retrain_state_row_key("filled_count", symbol), 0)),
        last_retrain_time=float(
            payload.get(_retrain_state_row_key("last_retrain_time", symbol), 0.0)
        ),
        outcomes=[],
        retrain_in_progress=False,
        scheduled_due=_parse_bool(
            payload.get(_retrain_state_row_key("scheduled_due", symbol))
        ),
        schedule_anchor=float(
            payload.get(_retrain_state_row_key("schedule_anchor", symbol), 0.0) or 0.0
        ),
    )
    if state.filled_count == 0 and state.last_retrain_time == 0.0:
        # Bootstrap solo si NUNCA hubo retrain: filled_count = cierres desde el inicio.
        # Si last_retrain_time > 0, filled_count=0 significa "contador reiniciado tras
        # un retrain promovido" (nuevo ciclo) y NO debe recomputarse desde la BD.
        state.outcomes = await _load_persisted_outcomes(symbol)
        state.filled_count = len(state.outcomes)
    return state


async def _save_retrain_state(
    state: RetrainState, symbol: str = TRAINING_SYMBOL
) -> None:
    from db.connection import get_pool

    pool = get_pool()
    rows = [
        (_retrain_state_row_key("filled_count", symbol), str(state.filled_count)),
        (_retrain_state_row_key("last_retrain_time", symbol), str(state.last_retrain_time)),
        (_retrain_state_row_key("scheduled_due", symbol), json.dumps(bool(state.scheduled_due))),
        (_retrain_state_row_key("schedule_anchor", symbol), json.dumps(float(state.schedule_anchor))),
    ]
    for key, value in rows:
        await pool.execute(
            "INSERT INTO retrain_state(key, value) VALUES($1, $2::jsonb) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
            key, value,
        )


# P3-1c: caché de estado POR PAR — cada mundo tiene su RetrainState en memoria.
_states: dict[str, RetrainState] = {}
_initialization_lock = asyncio.Lock()


async def _ensure_retrain_state(symbol: str = TRAINING_SYMBOL) -> RetrainState:
    async with _initialization_lock:
        if symbol not in _states:
            _states[symbol] = await _load_retrain_state(symbol)
    return _states[symbol]


def get_state(symbol: str = TRAINING_SYMBOL) -> RetrainState:
    return _states.get(symbol) or RetrainState()


async def _load_persisted_outcomes(
    symbol: str = TRAINING_SYMBOL,
) -> list[TradeOutcome]:
    """Load valid EA outcomes so feedback survives API/container restarts.

    P3-1c: solo los cierres de ESTE par — el bootstrap del contador de un
    mundo nunca cuenta trades de otro.
    """
    from db.connection import get_pool

    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT symbol, entry_time, exit_time, pnl, pnl_pct, direction,
               sl_hit, tp_hit, exit_reason
        FROM trade_outcomes
        WHERE symbol = $1
          AND entry_time > TIMESTAMPTZ '2000-01-01'
          AND exit_time > entry_time
        ORDER BY exit_time
        """,
        symbol,
    )
    outcomes = []
    for row in rows:
        outcomes.append(
            TradeOutcome(
                symbol=str(row["symbol"]),
                entry_time=row["entry_time"].timestamp(),
                exit_time=row["exit_time"].timestamp(),
                pnl=float(row["pnl"]),
                pnl_pct=float(row["pnl_pct"]),
                direction=str(row["direction"]).upper(),
                sl_hit=bool(row["sl_hit"]),
                tp_hit=bool(row["tp_hit"]),
                exit_reason=str(row["exit_reason"]),
            )
        )
    return outcomes


def _parse_timestamp(ts: float | str) -> float:
    """Acepta Unix timestamp (float or numeric string) o ISO string, devuelve Unix timestamp (float)."""
    if isinstance(ts, str):
        return float(ts)
    return float(ts)


async def _persist_trade_outcome(
    symbol: str,
    entry_time: float | str,
    exit_time: float | str,
    pnl: float,
    pnl_pct: float,
    direction: str,
    sl_hit: bool,
    tp_hit: bool,
    exit_reason: str,
    deal_ticket: int | None = None,
    position_id: int | None = None,
) -> int | None:
    """Persiste el trade outcome a la tabla trade_outcomes en PostgreSQL. Retorna el trade_id.

    Retorna None si el deal_ticket ya existía (dedup: webhook reintentado o backfill EA).
    """
    from db.connection import get_pool
    pool = get_pool()
    entry_ts = _parse_timestamp(entry_time)
    exit_ts = _parse_timestamp(exit_time)
    trade_id = await pool.fetchval(
        """
        INSERT INTO trade_outcomes
            (symbol, direction, entry_time, exit_time, pnl, pnl_pct, exit_reason, sl_hit, tp_hit,
             deal_ticket, position_id)
        VALUES ($1,$2,to_timestamp($3::double precision),to_timestamp($4::double precision),$5,$6,$7,$8,$9,$10,$11)
        ON CONFLICT (deal_ticket) WHERE deal_ticket IS NOT NULL DO NOTHING
        RETURNING id
        """,
        symbol, direction,
        entry_ts, exit_ts,
        pnl, pnl_pct,
        exit_reason, sl_hit, tp_hit,
        deal_ticket, position_id,
    )
    return trade_id


# P2-3 (2026-10-09, decisión de Félix — «Cooldown 24h post-rechazo»):
# tras un experimento 'rejected' el auto-trigger trade_count se suprime
# durante 24 h. #30/#31 (ambos trade_count → rejected el 09-10) quemaron
# ~5 min de cómputo por cierre EURUSD; el gate decide, la cadencia es
# orquestación — ningún umbral del gate se toca.
RETRAIN_COOLDOWN_HOURS = 24


async def _cooldown_active(now: float | None = None) -> bool:
    """True si hubo un experimento 'rejected' en las últimas RETRAIN_COOLDOWN_HOURS.

    Fuente de verdad = ledger `training_experiments` (max finished_at de los
    rechazos); sin estado propio que desincronizar. Los disparos
    manuales/forzados/aprobados no lo consultan (decisión humana).
    """
    from db.connection import get_pool
    last_rejected = await get_pool().fetchval(
        "SELECT max(finished_at) FROM training_experiments WHERE status='rejected'"
    )
    if last_rejected is None:
        return False
    if last_rejected.tzinfo is None:
        last_rejected = last_rejected.replace(tzinfo=timezone.utc)
    ts = last_rejected.timestamp()
    return (now if now is not None else time.time()) - ts < RETRAIN_COOLDOWN_HOURS * 3600


async def record_trade_filled(
    symbol: str,
    entry_time: float | str,
    exit_time: float | str,
    pnl: float,
    pnl_pct: float,
    direction: str,
    sl_hit: bool,
    tp_hit: bool,
    exit_reason: str,
    deal_ticket: int | None = None,
    position_id: int | None = None,
) -> None:
    """
    Llamar cada vez que una orden se cierra como 'filled'.
    Incrementa el contador y dispara retrain si corresponde.
    """
    entry_ts = _parse_timestamp(entry_time)
    exit_ts = _parse_timestamp(exit_time)

    trade_id = await _persist_trade_outcome(
        symbol, entry_ts, exit_ts, pnl, pnl_pct, direction, sl_hit, tp_hit, exit_reason,
        deal_ticket, position_id,
    )

    if trade_id is None:
        # deal_ticket ya registrado: webhook reintentado o backfill EA. No contar dos veces.
        logger.warning(
            "[TRADE-FILLED] deal_ticket=%s duplicado en %s — ignorado (ya registrado)",
            deal_ticket, symbol,
        )
        return

    # P2-4: racha del par en el guardián — clasificado con la convención
    # >= 3 EUR (is_winning_close) y persistida en risk_pair_state. Solo para
    # cierres reales (el duplicado de arriba no toca la racha).
    from services.risk_guardian import record_trade
    from services.risk_state_store import flush_risk_state
    record_trade(is_winning_close(pnl), symbol=symbol)
    await flush_risk_state(symbol=symbol)

    # Opción 5: Post-Trade Journal — análisis asíncrono con Qwen
    from services.trade_journal import record_trade_closed
    await record_trade_closed(
        trade_id=trade_id or 0,
        symbol=symbol,
        direction=direction,
        entry_time=entry_ts,
        exit_time=exit_ts,
        pnl=pnl,
        pnl_pct=pnl_pct,
        exit_reason=exit_reason,
        sl_hit=sl_hit,
        tp_hit=tp_hit,
    )

    outcome = TradeOutcome(
        symbol=symbol,
        entry_time=entry_ts,
        exit_time=exit_ts,
        pnl=pnl,
        pnl_pct=pnl_pct,
        direction=direction,
        sl_hit=sl_hit,
        tp_hit=tp_hit,
        exit_reason=exit_reason,
    )
    state = await _ensure_retrain_state(symbol)
    state.outcomes.append(outcome)
    state.filled_count += 1
    await _save_retrain_state(state, symbol)

    logger.info(
        "[RETRAIN] Trade #%d cerrado: %s %s pnl=%.2f (%s) — %d/%d trades para retrain",
        state.filled_count,
        symbol,
        direction,
        pnl,
        exit_reason,
        state.filled_count % RetrainConfig.trades_before_retrain,
        RetrainConfig.trades_before_retrain,
    )

    # Trigger retrain si corresponde (requiere aprobación humana)
    if state.filled_count >= RetrainConfig.min_trades_for_retrain:
        logger.warning(
            "[RETRAIN] Trigger automático para %s: filled_count=%d >= min_trades=%d — requiere aprobación humana",
            symbol, state.filled_count, RetrainConfig.min_trades_for_retrain,
        )
        if not settings.retrain_auto_approve:
            logger.warning(
                "[RETRAIN] Auto-retrain bloqueado: retrain_auto_approve=false. "
                "Usar POST /api/v1/ai/retrain/approve?symbol=%s para aprobar el retrain pendiente.",
                symbol,
            )
            return
        # P2-3 (2026-10-09, decisión Félix): cooldown de 24 h tras un
        # rechazo — #30/#31 quemaron ~5 min de cómputo por cierre EURUSD.
        # filled_count NO se reinicia: el retrain queda pendiente y el
        # primer cierre tras el cooldown lo dispara. Forzado/aprobado/calendario
        # (168 h) no se tocan: solo el disparo automático por cierre.
        if await _cooldown_active():
            logger.warning(
                "[RETRAIN] Auto-retrain en cooldown (≤%dh desde el último "
                "rechazo): pendiente con filled_count=%d — el próximo cierre "
                "tras el cooldown lo disparará.",
                RETRAIN_COOLDOWN_HOURS, state.filled_count,
            )
            return
        _spawn_retrain_task("trade_count", symbol=symbol)


async def _trigger_retrain(trigger: str = "manual", symbol: str = TRAINING_SYMBOL) -> None:
    """
    Dispara el retrain de `symbol` en background.
    Solo una instancia a la vez (Lock GLOBAL: pares serializados, sin
    carrera entre entrenamientos concurrentes).
    """
    logger.warning("[RETRAIN] ===== RETRAIN AUTOMÁTICO INICIADO (%s) =====", symbol)
    async with RETRAIN_LOCK:
        state = await _ensure_retrain_state(symbol)
        state.retrain_in_progress = True
        state.scheduled_due = False   # el intento consume el disparador del calendario
        await _save_retrain_state(state, symbol)
        try:
            await _do_retrain(trigger=trigger, symbol=symbol)
        finally:
            state.retrain_in_progress = False
            await _save_retrain_state(state, symbol)


_RETRAIN_TASK: asyncio.Task | None = None


def _spawn_retrain_task(trigger: str = "manual", symbol: str = TRAINING_SYMBOL) -> asyncio.Task | None:
    """Lanza _trigger_retrain(trigger, symbol) en background con referencia y
    manejo de fallo.

    Sin referencia, el task puede recolectarse y sus excepciones mueren en
    silencio (violación de 'no silent failures'): aquí se conserva la
    referencia, no se duplica si ya hay uno vivo y se alerta en el
    done-callback si termina con error. Un solo task vivo = retrains de
    distintos pares serializados; el trigger no rechazado sigue pendiente
    en SU contador (umbral se re-evalúa en cada cierre / approve?symbol=).
    """
    global _RETRAIN_TASK
    if _RETRAIN_TASK is not None and not _RETRAIN_TASK.done():
        logger.warning(
            "[RETRAIN] Ya hay un retrain en curso — se reutiliza el task vivo "
            "(el trigger '%s' de %s NO se encola: sigue pendiente en su contador)",
            trigger, symbol,
        )
        return _RETRAIN_TASK

    task = asyncio.create_task(_trigger_retrain(trigger, symbol=symbol))
    _RETRAIN_TASK = task

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            send_alert(AlertLevel.ERROR, "auto_retrain", "Retrain cancelado (task)")
            return
        exc = t.exception()
        if exc is not None:
            send_alert(
                AlertLevel.ERROR,
                "auto_retrain",
                f"Retrain fallido: {type(exc).__name__}: {exc}",
                exc=exc,
                context={"stage": "retrain_pipeline"},
            )

    task.add_done_callback(_on_done)
    return task


def _load_obs_rms(model_path: str):
    """obs_rms congeladas del bundle certificado en su manifest (None = bundle crudo).

    Se extraen con `pickle.load` directo (SB3 descarta `venv` al empaquetar):
    así las stats del bundle activo se obtienen SIN atarlas a un env — la
    igualdad de contrato se exige después, en `_evaluate_model`, construyendo
    el env con el contrato del modelo que se evalúa (Fase 3: baseline 24
    canales vs candidato 25).
    """
    from services.model_registry import load_manifest

    try:
        manifest = load_manifest(model_path)
    except RuntimeError as exc:
        logger.warning("[RETRAIN] Manifest no disponible (%s) — baseline evaluado en crudo", exc)
        return None
    vec_norm_file = manifest.get("vec_norm_file")
    if not vec_norm_file:
        return None

    pkl_path = os.path.join(os.path.dirname(model_path), vec_norm_file)
    with open(pkl_path, "rb") as fh:
        frozen = pickle.load(fh)
    logger.info("[RETRAIN] Baseline se evaluará con stats del bundle: %s", pkl_path)
    return frozen.obs_rms


def _make_train_env(env_cfg: dict, model_path: str):
    """Entorno de entrenamiento SIEMPRE con VecNormalize (P1-3):

    - bundle activo declarando vec_norm_file → continúa con ESAS stats
      (train/serve coherente entre ciclos);
    - bundle crudo o sin manifest → stats nuevas desde cero;
    - stats con contrato de obs distinto al del env (Fase 3: bundle (20,24)
      entrenando un env (20,25)) → stats nuevas desde cero. SB3 rechazaría
      el `set_venv` con un AssertionError críptico; el contrato cambia, la
      normalización se reconstruye (mismo precedente que la cabeza de acción
      P2-1). La comparación se hace sobre el pickle directo: el `observation_
      space` guardado es el del env con el que se entrenó ese bundle.
    """
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    from ml.trading_env_v2 import ForexTradingEnvV2
    from services.model_registry import load_manifest

    vec_norm_file = None
    try:
        vec_norm_file = load_manifest(model_path).get("vec_norm_file")
    except RuntimeError as exc:
        logger.warning("[RETRAIN] Manifest no disponible (%s) — VecNormalize nuevo desde cero", exc)

    dummy = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
    if vec_norm_file:
        pkl_path = os.path.join(os.path.dirname(model_path), vec_norm_file)
        with open(pkl_path, "rb") as fh:
            frozen = pickle.load(fh)
        stored_shape = tuple(getattr(frozen.observation_space, "shape", ()))
        env_shape = tuple(dummy.observation_space.shape)
        if stored_shape == env_shape:
            train_env = VecNormalize.load(pkl_path, dummy)
            # P2-1: el pickle de VecNormalize arrastra el action_space del bundle
            # origen y set_venv() no lo refresca; sin esta línea PPO construiría
            # la política con la cabeza vieja (p.ej. 4 tras pasar a [dir, volume]).
            train_env.action_space = train_env.venv.action_space
            train_env.training = True
            train_env.norm_reward = True
            logger.warning("[RETRAIN] Continuando con stats de normalización del bundle activo: %s", vec_norm_file)
            return train_env
        logger.warning(
            "[RETRAIN] Stats del bundle en contrato %s ≠ env %s (Fase 3: memoria) "
            "— VecNormalize nuevo desde cero",
            stored_shape, env_shape,
        )
        return VecNormalize(dummy, norm_obs=True, norm_reward=True, clip_obs=10.0)

    logger.warning("[RETRAIN] Bundle activo sin normalización — VecNormalize nuevo (stats desde cero)")
    return VecNormalize(
        dummy,
        norm_obs=True, norm_reward=True, clip_obs=10.0,
    )


def _evaluate_model(model, df: pd.DataFrame, env_cfg: dict, obs_rms=None) -> dict:
    """Evalúa una política sin feedback real y devuelve métricas comparables.

    - Episodio DETERMINISTA (random_reset=False): candidato y baseline recorren
      el MISMO segmento → la comparación es válida.
    - `obs_rms`: estadísticas congeladas de VecNormalize (None = bundle crudo):
      cada política se evalúa en el espacio de entrada con el que fue entrenada.
    - Contrato de obs por modelo (Fase 3 / T-22): el env se construye con los
      canales que declara el observation_space del modelo evaluado — derive_obs_contract
      resuelve pre-memoria (20,24), vivo (20,25), trend (20,27) y trend+memoria (20,28).
      Contrato desconocido = RuntimeError (sin degradación silenciosa).
    - Devuelve además curvas (equity, rewards por paso) para que el harness
      walk-forward calcule Sharpe/Sortino/DD.
    - wins/losses derivan de info['trades'] e info['win_rate'] (el env NO
      expone claves "wins"/"losses": antes siempre daban 0 — bug P1-4).
    """
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    from ml.trading_env_v2 import (
        EXTRA_STATE_FEATURES,
        FOREX_MARKET_FEATURE_COUNT as _MARKET_FEATURE_COUNT,
        HIGHER_TF_MARKET_FEATURES,
        TREND_MARKET_FEATURES,
        ForexTradingEnvV2,
        derive_obs_contract,
    )

    eval_cfg = dict(env_cfg)
    eval_cfg["df"] = df
    eval_cfg["real_outcomes"] = []
    eval_cfg["random_reset"] = False  # episodio fijo: baseline y candidato idénticos

    # T-22: contrato por ancho vía derive_obs_contract — MISMA fuente que el
    # serve (routers.ai.load_inference_artifacts): trend_features (obs 27/28)
    # y memory_channel (obs 25/28) deciden igual en ambos lados. T-30: el
    # 3er elemento (higher_tf_features, obs 31/32) se deriva del MISMO ancho
    # y el df de evaluación ya trae las columnas H4/D1 unidas (mismo join que
    # train). Contrato desconocido = RuntimeError (sin degradación silenciosa).
    obs_shape = tuple(getattr(getattr(model, "observation_space", None), "shape", ()))
    obs_width = obs_shape[-1] if len(obs_shape) == 2 else None
    contract = derive_obs_contract(obs_width)
    if contract is None:
        base_width = _MARKET_FEATURE_COUNT + EXTRA_STATE_FEATURES
        trend_width = base_width + len(TREND_MARKET_FEATURES)
        hf_width = trend_width + len(HIGHER_TF_MARKET_FEATURES)
        raise RuntimeError(
            f"[RETRAIN] Contrato de observación desconocido: {obs_shape} "
            f"(esperado {base_width} pre-memoria, {base_width + 1} con "
            f"memoria, {trend_width} trend, {trend_width + 1} trend+memoria, "
            f"{hf_width} T-30 o {hf_width + 1} T-30+memoria)"
            " — no se evalúa"
        )
    (eval_cfg["trend_features"],
     eval_cfg["higher_tf_features"],
     eval_cfg["memory_channel"]) = contract

    dummy = DummyVecEnv([lambda: ForexTradingEnvV2(**eval_cfg)])
    if obs_rms is not None:
        env = VecNormalize(dummy, training=False, norm_reward=False, clip_obs=10.0)
        env.obs_rms = obs_rms
    else:
        env = dummy

    initial_balance = float(eval_cfg.get("initial_balance", 0.0))
    observation = env.reset()
    total_reward = 0.0
    info = {}
    done = False
    equity_curve: list = []
    step_rewards: list = []

    while not done:
        action, _ = model.predict(observation, deterministic=True)
        flat = np.asarray(action, dtype=np.float32).reshape(-1)

        observation, reward, done_arr, infos = env.step(flat.reshape(1, -1))
        total_reward += float(reward[0])
        info = infos[0]
        equity_curve.append(float(info.get("equity", initial_balance)))
        step_rewards.append(float(reward[0]))
        done = bool(done_arr[0])

    # Integridad métrica (fix 2026-10-01, O-4): aperturas REALES contadas por
    # el env en su sitio de apertura (info['openings']). Antes se contaba la
    # intención pre-step con umbral 0.33 (la env decodifica con zona muerta
    # 0.05) y sumaba intentos que el sizing denegaba → masa del gate inflada
    # (exp #14: 826 «aperturas» vs 413 trades). Key directa: si el env no la
    # expone, el error PROPAGA (sin .get silencioso a 0).
    openings = int(info["openings"])
    trades = int(info.get("trades", 0))
    win_rate = float(info.get("win_rate", 0.0))
    wins = int(round(win_rate * trades))
    return {
        "openings": openings,
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": win_rate,
        "reward": total_reward,
        "balance": float(info.get("balance", initial_balance)),
        "equity_curve": equity_curve,
        "step_rewards": step_rewards,
        # Fase 3 / R-1: pnl por trade — segment_metrics calcula la expectancy
        # con la regla 3 €; None si el env no lo expone (error allí, no aquí).
        "trade_pnls": info.get("trade_pnls"),
    }


def _load_or_create_model(model_path: str, train_env, cfg) -> "PPO":
    """Carga el modelo activo para continuar el entrenamiento, o crea uno nuevo.

    P2-1: si el contrato de acción del bundle activo no coincide con el env
    (p.ej. 4→2 cabezas al unificar train/serve), `PPO.load(..., env=)` lanza
    "Action spaces do not match" — en ese caso se reentrena DESDE CERO con la
    política nueva (nunca se arrastra una cabeza muerta) y se deja constancia
    en el log. Un zip ilegible o dañado propaga su error (sin fallos
    silenciosos): solo el cambio de contrato dispara el reinicio.
    """
    from stable_baselines3 import PPO

    def _fresh() -> PPO:
        return PPO("MlpPolicy", train_env, learning_rate=cfg.learning_rate,
                   verbose=cfg.verbose)

    if not os.path.exists(model_path):
        logger.warning("[RETRAIN] No hay modelo previo — creando nuevo")
        return _fresh()

    probe = PPO.load(model_path)  # sin env: solo para inspeccionar el contrato
    model_shape = tuple(getattr(probe.action_space, "shape", ()))
    env_shape = tuple(getattr(train_env.action_space, "shape", ()))
    if model_shape != env_shape:
        logger.warning(
            "[RETRAIN] Contrato de acción %s ≠ env %s (P2-1: [direction, volume]) "
            "— reentreno desde cero con política nueva",
            model_shape, env_shape,
        )
        return _fresh()

    # Fase 3: mismo precedente P2-1 para el contrato de OBSERVACIÓN — un
    # bundle pre-memoria (20,24) sobre un env (20,25) rompería el forward de
    # la política en mitad del entrenamiento con un error críptico de forma;
    # aquí se detecta antes y se reentrena desde cero (stats ya reconstruidos
    # por _make_train_env al no casar el contrato del pickle).
    model_obs = tuple(getattr(getattr(probe, "observation_space", None), "shape", ()))
    env_obs = tuple(getattr(train_env.observation_space, "shape", ()))
    if model_obs != env_obs:
        logger.warning(
            "[RETRAIN] Contrato de observación %s ≠ env %s (Fase 3: canal "
            "bars_since_close) — reentreno desde cero con contrato nuevo",
            model_obs, env_obs,
        )
        return _fresh()

    model = PPO.load(model_path, env=train_env)
    logger.info("[RETRAIN] Modelo existente cargado — continuando entrenamiento (con VecNormalize)")
    return model


async def _do_retrain(trigger: str = "manual", symbol: str = TRAINING_SYMBOL) -> None:
    """P3-2a: orquesta snapshot + experimento alrededor del pipeline de retrain.

    0. Valida que `symbol` esté listado en PAIRS (P3-3: mundo explícito)
    1. Recolecta velas históricas de ESE par (lookback_for(symbol)) y congela
       el snapshot del df que van a consumir train y gate (retrain reproducible)
    2. Abre la fila del experimento en training_experiments (traza auditable
       por corrida: disparador + snapshot + symbol)
    3. Ejecuta _do_retrain_core (entreno → gate walk-forward → promoción),
       que cierra la fila en sus puntos de decisión (promoted/rejected)
    4. Ante cualquier excepción del pipeline cierra la fila como failed
       (UPDATE condicional sobre status='running': no pisa un cierre ya hecho)

    Un fallo ANTES del snapshot (fetch MT5, escritura) no abre fila — no hay
    experimento sin datos — y queda alertado por _spawn_retrain_task._on_done.
    """
    _validate_symbol(symbol)
    cfg = RetrainConfig
    logger.warning("[RETRAIN] ===== INICIANDO RETRAIN (trigger=%s, symbol=%s) =====", trigger, symbol)

    candles = await _fetch_historical_candles(symbol=symbol, count=cfg.lookback_for(symbol))
    if len(candles) < 100:
        raise RuntimeError(f"[RETRAIN] No hay suficientes velas de {symbol}: {len(candles)}")
    df = _build_training_dataframe(candles)
    # T-30 (orden Félix 2026-10-17, pre-registrado §3): con el flag on se
    # fetchean las velas H4/D1 con lookback suficiente (≥250 barras de
    # formación del SMA200 ANTES del primer H1 de la ventana: sin ellas la
    # primera fila ya tendría canal) y se unen los 4 canales por asof de
    # vela CERRADA. Sin pérdida de filas (el trim sigue en 204+19) y cero
    # NaN tolerados: un feed que no cubre la ventana es error explícito —
    # nunca columnas de relleno en el snapshot del candidato.
    if settings.higher_tf_features:
        lookback_h1 = cfg.lookback_for(symbol)
        # H4: ≈lookback/4 barras dentro de la ventana (horas de trading) +
        # 250 de formación + margen; D1: la proporción diaria es ≈23 barras
        # H1/día — el +500 absorbe fines de semana y festivos.
        h4 = await _fetch_historical_candles(
            symbol=symbol, timeframe="H4", count=lookback_h1 // 4 + 300)
        d1 = await _fetch_historical_candles(
            symbol=symbol, timeframe="D1", count=lookback_h1 // 24 + 500)
        from ml.trading_env_v2 import HIGHER_TF_MARKET_FEATURES, join_higher_tf_channels
        df = join_higher_tf_channels(df, h4, d1)
        hf_nan = {c: int(df[c].isna().sum()) for c in HIGHER_TF_MARKET_FEATURES}
        if any(hf_nan.values()):
            raise RuntimeError(
                f"[RETRAIN] T-30: canales H4/D1 con NaN en {symbol} "
                f"(velas H4={len(h4)}, D1={len(d1)}, filas H1={len(df)}): "
                f"{hf_nan} — feed sin cobertura de la ventana, no se entrena "
                f"a ciegas"
            )
        logger.warning(
            "[RETRAIN] T-30: canales H4/D1 unidos al snapshot (H4=%d, D1=%d, "
            "%d filas H1, 0 NaN) — contrato del candidato (20,32)",
            len(h4), len(d1), len(df),
        )
    # T-27 (D1 pre-registrado): trim del warm-up de canales de tendencia ANTES
    # del snapshot → sha256 = df exacto que consumen train y gate.
    df = _trim_trend_warmup(df)
    if len(df) < 100:
        raise RuntimeError(
            f"[RETRAIN] Sólo {len(df)} filas tras trim de warm-up de {symbol} "
            f"({len(candles)} velas): insuficiente para train+gate"
        )

    # P3-2a: snapshot del df exacto que consume train+gate → reproducibilidad.
    snap = data_snapshot.save_snapshot(df, purpose="retrain")
    data_snapshot.prune_snapshots()
    exp_id = await experiment_tracker.start_experiment(
        trigger, snap.path, snap.sha256, symbol=symbol
    )

    try:
        await _do_retrain_core(df, exp_id, symbol)
    except BaseException as exc:
        try:
            await experiment_tracker.finish_experiment(
                exp_id, "failed", error=f"{type(exc).__name__}: {exc}"
            )
        except BaseException:
            # Preserva la excepción original del pipeline; el fallo de cierre
            # queda logueado (la fila 'running' hinchada también es visible).
            logger.exception("[EXPERIMENT] #%s: fallo al cerrar como failed", exp_id)
        raise


async def _do_retrain_core(df: pd.DataFrame, exp_id: int, symbol: str = TRAINING_SYMBOL) -> None:
    """Pipeline de retrain sobre un df ya congelado como snapshot.

    Todo artefacto (bundle, manifest, baseline, hot-reload, backup) pertenece
    al MUNDO de `symbol` (P3-3): la ruta sale de default_model_path(symbol).

    Terminaliza el experimento `exp_id` en los puntos de decisión del gate:
    'rejected' al rechazar el candidato, 'promoted' tras record_promotion.
    """
    from services.model_backup import backup_model as upload_model

    cfg = RetrainConfig
    model_path = default_model_path(symbol)
    # El namespace del par existe ANTES de cualquier escritura del bundle
    # (norm/zip/manifest): SB3 crea padres, write_manifest no.
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    split = build_walk_forward_split(
        len(df),
        window_size=cfg.retrain_window_size,
        n_folds=cfg.walk_forward_folds,
        train_frac=cfg.walk_forward_train_frac,
        holdout_frac=cfg.walk_forward_holdout_frac,
        min_fold_bars=cfg.walk_forward_min_fold_bars,
    )
    train_df = df.iloc[:split.train_end].reset_index(drop=True)
    logger.warning(
        "[RETRAIN-WF] split: train=%d | embargo=%d | folds=4×~%d barras | holdout@%d (congelado)",
        split.train_end,
        split.embargo,
        split.folds[0].bars,
        split.holdout.start,
    )

    real_outcomes = await _load_persisted_outcomes(symbol)
    n_real = len(real_outcomes)
    if n_real > 0:
        logger.warning(
            "[RETRAIN] Inyectando %d outcomes reales al env (peso=%.2f)",
            n_real, cfg.real_outcome_weight,
        )
    else:
        logger.warning("[RETRAIN] Sin cierres reales: entrenamiento solo histórico/sintético")

    env_cfg = dict(
        df=train_df,
        window_size=cfg.retrain_window_size,
        initial_balance=cfg.initial_balance,
        lot_size=100000.0,
        # P2-2: sizing PPO delegado en position_sizing.size_ppo_position —
        # mismos topes que /predict (settings: max_volume, ppo_risk_pct).
        max_lot=settings.max_volume,
        risk_pct=settings.ppo_risk_pct,
        pip_size=0.0001,
        # O-2 (2026-10-01): costes reales del bróker — spread medio 1.73 pt
        # (0.173 pip) sobre las 30000 H1 del propio snapshot vía rates del
        # bróker (p50=0, p90=8); antes 1.5 pips = 8.7× la real.
        spread_pips=0.17,
        commission_per_lot=cfg.commission,
        churn_friction_per_lot=cfg.churn_friction_per_lot,
        max_episode_steps=1000,
        max_holding_steps=120,
        reward_scale=100.0,
        random_reset=True,
        real_outcomes=real_outcomes,
        real_outcome_weight=cfg.real_outcome_weight,
        # P2-1: trailing espejo de los inputs del EA (mql5 §V); SL/TP salen
        # de production_stops() (misma fórmula que /predict) — sin max_sl/max_tp.
        trailing_enabled=True,
        trailing_percent=0.25,
        trailing_min_pips=15.0,
        trailing_atr_mult=2.0,
        min_stop_dist_pips=1.0,
        # Fase 3 (plan 2026-09-30): candidato SIEMPRE con el contrato nuevo —
        # canal 8º bars_since_close → obs (20,25). El eval de cada modelo
        # pisa este flag según el observation_space del modelo evaluado.
        memory_channel=True,
        # T-22/T-27: el bloque de tendencia NO entra en el obs — el retrain
        # pre-registrado T-27 (2026-10-17) lo activó y su GATE CAYÓ (3/6) →
        # revert por orden de Félix («revert T-27»). T-30 (orden 2026-10-17):
        # el ÚNICO canario que lo reactiva es `higher_tf_features` — el
        # candidato H4/D1 exige el bloque de tendencia (contrato 31/32 =
        # 20+3+4 market, validado en el env con ValueError). Default off =
        # contrato vivo (20,25) byte-idéntico.
        trend_features=settings.higher_tf_features,
        # T-30: bloque de horizonte lento — sólo para el run pre-registrado
        # report-only (flag en .env; serve lo ignora, decide por el ancho).
        higher_tf_features=settings.higher_tf_features,
        # T-24 (2026-10-17): versión de recompensa explícita igual que
        # trend_features — default "v1" inerte; T-27 lo cambia a "v2_align"
        # en LA MISMA línea revisable (eval y train heredan este env_cfg).
        reward_version=cfg.reward_version,
    )
    logger.warning(
        "[RETRAIN] Costes del env (O-2): spread=%.2f pips, comisión_bróker=%.4f €/lot, ancla_churn=%.2f €/lot",
        env_cfg["spread_pips"], env_cfg["commission_per_lot"],
        env_cfg["churn_friction_per_lot"],
    )

    from stable_baselines3 import PPO

    train_env = _make_train_env(env_cfg, model_path)

    # P2-1: carga continuable o política nueva si cambió el contrato de acción.
    model = _load_or_create_model(model_path, train_env, cfg)

    logger.info("[RETRAIN] Entrenando %d timesteps...", cfg.total_timesteps)
    model.learn(
        total_timesteps=cfg.total_timesteps,
        progress_bar=False,
    )

    # Evaluación walk-forward OOS (P1-1): 4 folds disjuntos con embargo +
    # holdout congelado. Mismo evaluate_fn para ambas políticas → comparación
    # emparejada; el holdout NO participa en el gate.
    baseline_model = None
    baseline_rms = None
    if os.path.exists(model_path):
        baseline_model = PPO.load(model_path)
        baseline_rms = _load_obs_rms(model_path)

    def _eval_segment(policy, segment_df, rms):
        return _evaluate_model(policy, segment_df, env_cfg, obs_rms=rms)

    report = evaluate_walk_forward(
        df,
        split,
        candidate=model,
        candidate_obs_rms=train_env.obs_rms,
        baseline=baseline_model,
        baseline_obs_rms=baseline_rms,
        evaluate_fn=_eval_segment,
        initial_balance=cfg.initial_balance,
    )
    decision = gate_walk_forward(report, min_openings=cfg.min_validation_openings)

    base_summary = (
        f"{report.baseline['sum_reward']:.3f}/{report.baseline['mean_sharpe']:.2f}"
        if report.baseline is not None else "none"
    )
    logger.warning(
        "[RETRAIN-WF] cand(sum_reward=%.3f sharpe=%.2f opens=%d) | base=%s | "
        "fold_wins=%s/%d holdout_cand=%.3f → %s (%s)",
        report.candidate["sum_reward"], report.candidate["mean_sharpe"],
        report.candidate["total_openings"],
        base_summary, report.fold_wins, report.n_folds,
        report.holdout["candidate"]["reward"],
        "APROBADO" if decision.ok else "RECHAZADO", decision.reason,
    )

    # T-28 (REQ-1, c — pre-registrado D6): segunda condición del gate de
    # retrain — ALINEACIÓN PPO↔LLM del par que SIRVE (ventana reciente de
    # ciclos auditados con juicio LLM existente). Fail-closed: muestra
    # insuficiente o error de cómputo = no promovible, con la causa explícita.
    #
    # T-28 opción A (orden Félix 2026-10-17): bootstrap de un mundo nuevo —
    # un par SIN bundle previo (el modelo fresco sólo se guarda al promover,
    # línea ~1070) y con 0 ciclos auditados no puede demostrar alineación
    # porque jamás sirvió: la métrica 7 se marca N/A y el gate cae sólo sobre
    # las métricas 1-6 (walk-forward + suelo absoluto + baseline), intactas.
    # Cualquier fila >0 reactiva el fail-closed estricto — incluido «bundle
    # borrado con historial vivo»: si hay muestra, se evalúa. El silencio de
    # un EA vivo lo cubre predict_silence, no esta rama. Queda constancia
    # explícita (status en report.json, manifest y eventos) para que N/A
    # jamás se confunda con un pase de la métrica.
    prior_bundle = os.path.exists(model_path)
    t28_alignment = await alignment_gate(symbol)
    if not prior_bundle and t28_alignment.get("n_window", 0) == 0:
        t28_alignment = {
            "ok": True,
            "status": "not_applicable_bootstrap",
            "reasons": ["not_applicable_no_prior_bundle"],
            "n_window": 0,
            "n_active": 0,
            "agreement_rate": None,
            "conflict_rate": None,
            "joint_entropy_bits": None,
        }
        logger.warning(
            "[RETRAIN] T-28 N/A (bootstrap %s): sin bundle previo y 0 ciclos "
            "auditados — gate sobre métricas 1-6; la alineación se aplicará "
            "estricta desde el 2º retrain", symbol,
        )

    # T-27 (D3 pre-registrado): artefactos del candidato para el gate de
    # aceptación (métricas 3-6) y para T-28 (métrica 7) SIN promover — bajo
    # RETRAIN_REPORT_ONLY el modelo se pierde en memoria al retornar. Paths
    # aparte del bundle activo (retrain_artifacts/): jamás tocan el sirviente.
    def _jsonable(o):
        return o.item() if hasattr(o, "item") else str(o)

    artifact_dir = os.path.join(
        os.path.dirname(model_path), "retrain_artifacts", f"exp{exp_id}"
    )
    os.makedirs(artifact_dir, exist_ok=True)
    model.save(os.path.join(artifact_dir, "candidate.zip"))
    train_env.save(os.path.join(artifact_dir, "candidate_vec_norm.pkl"))
    with open(os.path.join(artifact_dir, "report.json"), "w") as fh:
        json.dump({
            "exp_id": exp_id,   # triggered_by vive en training_experiments
            "symbol": symbol,
            "obs_shape": list(getattr(model.observation_space, "shape", ())),
            "cfg": {
                "reward_version": cfg.reward_version,
                "trend_features": env_cfg["trend_features"],
                "total_timesteps": cfg.total_timesteps,
                "warmup_trim_rows": WARMUP_TRIM_ROWS,
                "rows": int(len(df)),
            },
            "gate": {"ok": decision.ok, "reason": decision.reason},
            # T-28: condición de alineación PPO↔LLM congelada junto al
            # candidato (auditable aunque no se promueva).
            "t28_alignment": t28_alignment,
            "walk_forward": report.to_dict(),
        }, fh, indent=2, default=_jsonable)
    logger.warning("[RETRAIN] Candidato + informe en %s", artifact_dir)

    if not decision.ok:
        logger.warning(
            "[RETRAIN] Candidato rechazado (%s): modelo activo conservado",
            decision.reason,
        )
        await experiment_tracker.finish_experiment(
            exp_id, "rejected",
            gate_reason=decision.reason,
            gate_metrics=report.to_dict(),
        )
        # P3-2d: cada intento (aunque rechazado) ancla el calendario — sin esto
        # el programado reintentaría cada hora tras un rechazo (tormenta).
        state = await _ensure_retrain_state(symbol)
        state.last_retrain_time = time.time()
        await _save_retrain_state(state, symbol)
        await _log_retrain_event("retrain_rejected", {
            "reason": decision.reason,
            "gate": decision.details,
            "t28_alignment": t28_alignment,
            "report": report.to_dict(),
        }, symbol=symbol)
        return

    # T-28 (REQ-1, c — pre-registrado D6): gate wf OK pero el par PPO↔LLM no
    # demuestra alineación (o sin muestra suficiente — fail-closed) → rechazo
    # declarado con SU causa, misma mecánica anti-tormenta que el rechazo wf.
    if not t28_alignment.get("ok"):
        reasons_t28 = t28_alignment.get("reasons") or ["unknown"]
        reason_t28 = f"t28_alignment:{','.join(reasons_t28)}"
        logger.warning(
            "[RETRAIN] Candidato no promovible — alineación PPO↔LLM no demostrada "
            "(%s): active=%s agreement=%s conflict=%s entropy_bits=%s",
            reason_t28,
            t28_alignment.get("n_active"), t28_alignment.get("agreement_rate"),
            t28_alignment.get("conflict_rate"), t28_alignment.get("joint_entropy_bits"),
        )
        await experiment_tracker.finish_experiment(
            exp_id, "rejected",
            gate_reason=reason_t28,
            gate_metrics=report.to_dict(),
        )
        state = await _ensure_retrain_state(symbol)
        state.last_retrain_time = time.time()
        await _save_retrain_state(state, symbol)
        await _log_retrain_event("retrain_rejected", {
            "reason": reason_t28,
            "gate": decision.details,
            "t28_alignment": t28_alignment,
            "report": report.to_dict(),
        }, symbol=symbol)
        return

    # ─── T-09 (Félix 2026-10-16): holdout report-only — no escrituras si el
    # gateway se está validando. Promoción requiere Orden explícita (env
    # REHamilton report-only False + env REPORT_ONLY_ENABLED False).
    if settings.retrain_report_only:
        logger.warning(
            "[RETRACE][REPORT-ONLY] gate=%s (%s) -> NO se promociona (solo traza OOS)",
            decision.ok, decision.reason,
        )
        await experiment_tracker.finish_experiment(
            exp_id, "rejected",
            gate_reason=f"report_only:{decision.reason}",
            gate_metrics=report.to_dict(),
        )
        state = await _ensure_retrain_state(symbol)
        state.last_retrain_time = time.time()
        await _save_retrain_state(state, symbol)
        await _log_retrain_event("retrain_report_only", {
            "gate_ok": decision.ok,
            "gate_reason": decision.reason,
            "t28_alignment": t28_alignment,
            "report": report.to_dict(),
        }, symbol=symbol)
        return

    # Promoción del BUNDLE completo: stats + zip + manifest certifican el conjunto.
    # Si el proceso muere entre archivos, validate_bundle falla en el próximo arranque.
    norm_path = os.path.splitext(model_path)[0] + "_vec_norm.pkl"
    norm_tmp = norm_path + ".tmp"
    train_env.save(norm_tmp)
    os.replace(norm_tmp, norm_path)

    candidate_path = model_path.removesuffix(".zip") + ".candidate.zip"
    model.save(candidate_path)
    os.replace(candidate_path, model_path)

    # P0-5: certificar el bundle promovido. Sin manifest válido el próximo
    # arranque fallará en validate_bundle (fail-fast: nunca sirve binarios sucios).
    from services.model_registry import build_manifest, record_promotion, write_manifest
    # P3-2c: baseline de drift congelado sobre el df de ENTRENO — la referencia
    # contra la que el monitor PSI compara las velas vivas. Si no hay baseline
    # utilizable, no se promueve (mejor no promover que promover a ciegas).
    # P3-3: baseline POR PAR (junto a su bundle) — la promoción de un mundo
    # nunca pisa la referencia de otro.
    baseline_info = drift_monitor.save_baseline(train_df, symbol=symbol)
    manifest = build_manifest(
        model_path,
        obs_shape=getattr(model.observation_space, "shape", ()),
        vec_norm_file=os.path.basename(norm_path),
        source="auto_retrain_normalized",
        extra={
            "walk_forward": report.to_dict(),
            "gate": {"ok": decision.ok, "reason": decision.reason},
            # T-28: la promoción certifica TAMBIÉN la alineación PPO↔LLM
            # medida (la condición pasó — el dict completo queda en el bundle).
            "t28_alignment": t28_alignment,
            "drift_baseline": baseline_info,
        },
    )
    write_manifest(model_path, manifest)

    # P1-2: historial de promociones (model_registry) + hot-reload del bundle en
    # memoria. Sin la recarga, la promoción no surte efecto hasta reiniciar
    # (disco ≠ memoria). El swap en ai.py es atómico y valida antes de tocar la caché.
    await record_promotion(
        manifest,
        {"ok": decision.ok, "reason": decision.reason,
         "checks": decision.details.get("checks")},
        symbol=symbol,
    )
    # P3-1b: hot-reload vía ModelStorePort — el servicio ya no importa del
    # router (el adapter resuelve `routers.ai` en el momento de la llamada,
    # así que el patch de los tests sigue viéndose).
    from core.adapters import AiModelStoreAdapter
    AiModelStoreAdapter().reload(symbol)

    # P3-2a: la promoción es el punto de decisión del experimento — se cierra
    # aquí, antes de los pasos posteriores (state/backup/log): si esos fallan,
    # el wrapper intenta 'failed' pero el UPDATE condicional no lo pisa.
    await experiment_tracker.finish_experiment(
        exp_id, "promoted",
        gate_reason=decision.reason,
        gate_metrics=report.to_dict(),
        model_sha256=manifest["model_sha256"],
    )

    state = await _ensure_retrain_state(symbol)
    state.last_retrain_time = time.time()
    state.filled_count = 0          # nuevo ciclo: los próximos N cierres cuentan
    state.outcomes = []
    await _save_retrain_state(state, symbol)

    # 6. Backup a GCS
    backup_url = await upload_model(symbol=symbol)
    logger.warning(
        "[RETRAIN] ===== RETRAIN COMPLETADO ===== model=%s backup=%s sum_reward=%.3f gate=%s",
        model_path,
        backup_url,
        report.candidate["sum_reward"],
        decision.reason,
    )

    await _log_retrain_event("retrain_promoted", {
        "reason": decision.reason,
        "gate": decision.details,
        "t28_alignment": t28_alignment,
        "report": report.to_dict(),
        "bundle": {
            "model": os.path.basename(model_path),
            "vec_norm_file": os.path.basename(norm_path),
            "source": "auto_retrain_normalized",
        },
        "backup_url": backup_url,
    }, symbol=symbol)

    # M-1: la promoción queda marcada para el commit de cron (bundle completo
    # y registrado arriba — el marcador es lo último que se toca).
    _write_promotion_marker(
        symbol=symbol, model_path=model_path, reason=decision.reason
    )


async def _fetch_historical_candles(symbol: str, count: int,
                                    timeframe: str = "H1") -> list:
    """Obtiene velas históricas de MT5 MCP (`timeframe` por defecto H1)."""
    import httpx
    # 60s (P3-3): el lookback del canario (30000 H1) tarda ~18s desde el host
    # y el contenedor puede ser más lento — con 30s quedaba al filo.
    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.get(
            f"{settings.mt5_http_url}/api/v1/market/candles/latest",
            params={"symbol_name": symbol, "timeframe": timeframe, "count": count},
        )
        res.raise_for_status()
        data = res.json()
        return data if isinstance(data, list) else data.get("candles", [])


def _build_training_dataframe(candles: list) -> pd.DataFrame:
    """Build the canonical v3 feature dataframe used by training and inference."""
    from ml.trading_env_v2 import engineer_market_features

    df = pd.DataFrame(candles)
    return engineer_market_features(df)


# T-27 (2026-10-17, decisión pre-registrada D1): filas de calentamiento de los
# canales de tendencia (= TREND_FEATURE_WINDOW_MIN 224 − window 20). En ellas
# dist_sma200/sma200_slope llevan relleno neutro 0.0 (SMA200 aún no formado).
WARMUP_TRIM_ROWS = 204


def _trim_trend_warmup(df: pd.DataFrame) -> pd.DataFrame:
    """Recorta las primeras `WARMUP_TRIM_ROWS` filas y REINDEXA a 0..n-1.

    Con `trend_features=True` esas filas serían estados falsos en el obs ("en
    la media, pendiente plana") que el serve jamás emite (guarda
    `TREND_FEATURE_WINDOW_MIN` velas) — el trim ALIGNA train con serve.
    `reset_index(drop=True)` es OBLIGATORIO: `_index_real_outcomes` y el paso
    del env indexan por posición; sin él, off-by-204. Aplicado ANTES del
    snapshot para que sha256 = df exacto que consumen train y gate.
    """
    return df.iloc[WARMUP_TRIM_ROWS:].reset_index(drop=True)


async def _log_retrain_event(event: str, data: dict, symbol: str = TRAINING_SYMBOL) -> None:
    """Persiste un evento de ciclo de retrain en audit_log (P1-1).

    - `retrain_promoted` / `retrain_rejected`: decisión de gate con el informe
      walk-forward completo (métricas por pliego + agregados) — antes ambos
      caminos escribían el mismo `retrain_completed` sin métricas ni hash.
    - `symbol`: el estado (contadores/outcomes) que se adjunta es el del par
      cuyo ciclo se registró (P3-3) — jamás el de otro mundo.
    """
    from db.connection import get_pool

    pool = get_pool()
    state = await _ensure_retrain_state(symbol)
    payload = dict(data)
    payload.setdefault("state", {
        "filled_count": state.filled_count,
        "last_retrain_time": state.last_retrain_time,
        "outcomes": len(state.outcomes),
        "avg_pnl": float(np.mean([o.pnl for o in state.outcomes])) if state.outcomes else 0.0,
        "outcomes_win_rate": float(np.mean([is_winning_close(o.pnl) for o in state.outcomes])) if state.outcomes else 0.0,
    })
    payload.setdefault("config", {
        "walk_forward_folds": RetrainConfig.walk_forward_folds,
        "total_timesteps": RetrainConfig.total_timesteps,
        "learning_rate": RetrainConfig.learning_rate,
        "real_outcome_feedback": bool(state.outcomes),
    })

    await pool.execute(
        "INSERT INTO audit_log(cycle_id, event, data) VALUES($1, $2, $3)",
        f"retrain_{int(time.time())}",
        event,
        json.dumps(payload, default=float, ensure_ascii=False),
    )


def _write_promotion_marker(symbol: str, model_path: str, reason: str) -> None:
    """M-1 (plan 2026-09-30): marcador de commit pendiente junto al bundle.

    Su presencia con contenido distinto de HEAD (`git status` lo marca) significa
    "artefactos de promoción sin versionar". El script de cron
    `scripts/commit_promotion.py` valida el sha256 del zip contra el marcador,
    versiona `fastapi/ml` y deja el propio marcador como evidencia permanente
    del evento en el historial de git.

    Se escribe al FINAL de la promoción (bundle certificado + registro en
    model_registry + evento de auditoría ya hechos): si el proceso muere antes,
    el marcador no existe y los archivos sucios siguen visibles en `git status`
    — la pendencia nunca es silenciosa. Si la escritura del marcador falla, la
    promoción YA está viva (no hay rollback posible ni deseable): no se lanza,
    pero el fallo queda en alerta (rate-limited, B-1) con instrucción manual.
    """
    from services.model_registry import sha256_of

    marker = os.path.join(os.path.dirname(model_path), "last_promotion.json")
    payload = {
        "symbol": symbol,
        "model": os.path.basename(model_path),
        "model_sha256": sha256_of(model_path),
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        tmp = marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, marker)
        logger.info("[M-1] marcador de promoción escrito: %s", marker)
    except OSError as e:
        logger.error(
            "[M-1] marcador NO escrito (%s): %s — commitear fastapi/ml manualmente",
            marker, e,
        )
        send_alert(
            AlertLevel.ERROR, "auto_retrain",
            f"M-1: marcador de promoción no escrito — commitear fastapi/ml manualmente ({e})",
        )


# ─── HTTP Endpoint para forzar retrain manualmente ────────────────────────────

async def force_retrain(symbol: Optional[str] = None) -> dict:
    """Fuerza un retrain inmediato de `symbol` (para uso manual/debug).

    No salta el gate de promoción: eso vive dentro de _do_retrain.
    `symbol=None` → TRAINING_SYMBOL (compatibilidad con el caller actual).
    """
    sym = _validate_symbol(symbol or TRAINING_SYMBOL)
    state = await _ensure_retrain_state(sym)
    if state.retrain_in_progress:
        return {"status": "already_running"}

    _spawn_retrain_task("forced", symbol=sym)
    return {"status": "triggered", "filled_count": state.filled_count}


async def check_scheduled_retrain() -> bool:
    """P3-2d: decide si el calendario marca un retrain. Devuelve True si toca.

    Ancla = último intento (promoción O rechazo); si nunca hubo, la primera
    revisión fija `schedule_anchor` (primer hito a `retrain_interval_hours`).
    Con `retrain_auto_approve=false` deja `scheduled_due` para el flujo de
    aprobación (approve_retrain lo acepta sin alcanzar el umbral de trades);
    con true, dispara directamente con trigger='scheduled'.
    """
    state = await _ensure_retrain_state()
    if state.retrain_in_progress:
        return False
    now = time.time()
    interval_h = max(1, settings.retrain_interval_hours)
    anchor = state.last_retrain_time or state.schedule_anchor
    if anchor <= 0:
        state.schedule_anchor = now
        await _save_retrain_state(state)
        logger.info(
            "[RETRAIN-SCHED] Sin historial: primer check del calendario anclado "
            "ahora — primer retrain programado en %dh", interval_h,
        )
        return False
    elapsed_h = (now - anchor) / 3600.0
    if elapsed_h < interval_h:
        return False
    state.scheduled_due = True
    await _save_retrain_state(state)
    logger.warning(
        "[RETRAIN-SCHED] Retrain programado pendiente: último intento hace "
        "%.1fh (intervalo %dh)", elapsed_h, interval_h,
    )
    if not settings.retrain_auto_approve:
        logger.warning(
            "[RETRAIN-SCHED] auto_approve=false — aprobar vía "
            "POST /api/v1/ai/retrain/approve"
        )
        return True
    _spawn_retrain_task("scheduled")
    return True


async def _retrain_schedule_loop() -> None:
    """Calendario de retrain: revisa cada hora si toca (P3-2d)."""
    await asyncio.sleep(SCHEDULE_CHECK_S)   # warmup: dejar arrancar el sistema
    while True:
        try:
            await check_scheduled_retrain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            send_alert(
                AlertLevel.ERROR, "auto_retrain",
                f"Check de retrain programado falló: {type(exc).__name__}: {exc}",
                exc=exc,
            )
        await asyncio.sleep(SCHEDULE_CHECK_S)


async def approve_retrain(symbol: Optional[str] = None) -> dict:
    """Aprueba el retrain pendiente de `symbol` (umbral alcanzado o calendario).

    Solo aprueba si hay un disparo realmente pendiente (umbral de trades
    alcanzado O scheduled_due del calendario) y no hay otro en curso;
    el gate de promoción del candidato se aplica igual dentro de _do_retrain.
    `symbol=None` → TRAINING_SYMBOL. P3-3: un par con retrain_auto_approve
    false deja SU contador por encima del umbral — approve por símbolo es la
    única vía para que ese mundo entrene (sin él quedaría pendiente para
    siempre: fallo silencioso).
    """
    sym = _validate_symbol(symbol or TRAINING_SYMBOL)
    state = await _ensure_retrain_state(sym)
    if state.retrain_in_progress:
        return {"status": "already_running", "filled_count": state.filled_count}
    if (state.filled_count < RetrainConfig.min_trades_for_retrain
            and not state.scheduled_due):
        return {
            "status": "no_pending",
            "filled_count": state.filled_count,
            "min_trades_for_retrain": RetrainConfig.min_trades_for_retrain,
        }
    _spawn_retrain_task("approved", symbol=sym)
    return {
        "status": "approved",
        "filled_count": state.filled_count,
        "scheduled_due": state.scheduled_due,
    }

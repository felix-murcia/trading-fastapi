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
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from config import settings
from core.rules import is_winning_close
from services import data_snapshot, drift_monitor, experiment_tracker
from services.alerting import AlertLevel, send_alert
from services.walk_forward import (
    build_walk_forward_split,
    evaluate_walk_forward,
    gate_walk_forward,
)

logger = logging.getLogger(__name__)

MODEL_PATH = "/app/ml/ppo_trading_bot_v3.zip"   # ← EA usa v3, no v2
# P3-1b: el pipeline de retrain entrena HOY un único mundo. Fuente única del
# símbolo (fetch de velas + hot-reload del bundle tras promover); P3-1c lo
# parametriza por par (columnas symbol en model_registry/training_experiments
# y retrain_state por símbolo).
TRAINING_SYMBOL = "EURUSD"
RETRAIN_LOCK = asyncio.Lock()
SCHEDULE_CHECK_S = 3600   # P3-2d: el calendario se revisa cada hora


@dataclass
class RetrainConfig:
    trades_before_retrain: int = 10       # ← 50 era demasiado: 10 permite aprender rápido
    min_trades_for_retrain: int = 5      # ← mínimo razonable tras primer trade
    lookback_candles: int = 500           # Velas históricas a usar para retrain
    retrain_window_size: int = 20         # Window size del entorno
    initial_balance: float = 1000.0
    commission: float = 0.0001            # 0.01% por trade (MT5 typical)
    n_epochs: int = 3                    # Epochs de entrenamiento (compatibilidad/auditoría)
    total_timesteps: int = 100_000      # PPO steps por retrain; 1.500 era insuficiente
    learning_rate: float = 3e-4
    verbose: int = 0
    real_outcome_weight: float = 0.3      # Peso del feedback real vs sintético (0=ignorar, 1=dominante)
    min_validation_openings: int = 1      # Un candidato HOLD permanente no reemplaza al activo
    walk_forward_folds: int = 4           # P1-1: ≥4 pliegues OOS (exigencia de auditoría)
    walk_forward_train_frac: float = 0.70
    walk_forward_holdout_frac: float = 0.10  # holdout congelado: reporta, nunca decide
    walk_forward_min_fold_bars: int = 10  # pliegue más corto evaluable (si no, aborta)


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


def _retrain_state_row_key(key: str) -> str:
    return f"retrain_state.{key}"


async def _load_retrain_state() -> RetrainState:
    from db.connection import get_pool

    pool = get_pool()
    rows = await pool.fetch(
        "SELECT key, value FROM retrain_state WHERE key = ANY($1::text[])",
        ["retrain_state.filled_count", "retrain_state.last_retrain_time",
         "retrain_state.scheduled_due", "retrain_state.schedule_anchor"],
    )
    payload = {row["key"]: row["value"] for row in rows}
    state = RetrainState(
        filled_count=int(payload.get(_retrain_state_row_key("filled_count"), 0)),
        last_retrain_time=float(payload.get(_retrain_state_row_key("last_retrain_time"), 0.0)),
        outcomes=[],
        retrain_in_progress=False,
        scheduled_due=_parse_bool(payload.get(_retrain_state_row_key("scheduled_due"))),
        schedule_anchor=float(
            payload.get(_retrain_state_row_key("schedule_anchor"), 0.0) or 0.0
        ),
    )
    if state.filled_count == 0 and state.last_retrain_time == 0.0:
        # Bootstrap solo si NUNCA hubo retrain: filled_count = cierres desde el inicio.
        # Si last_retrain_time > 0, filled_count=0 significa "contador reiniciado tras
        # un retrain promovido" (nuevo ciclo) y NO debe recomputarse desde la BD.
        state.outcomes = await _load_persisted_outcomes()
        state.filled_count = len(state.outcomes)
    return state


async def _save_retrain_state(state: RetrainState) -> None:
    from db.connection import get_pool

    pool = get_pool()
    rows = [
        (_retrain_state_row_key("filled_count"), str(state.filled_count)),
        (_retrain_state_row_key("last_retrain_time"), str(state.last_retrain_time)),
        (_retrain_state_row_key("scheduled_due"), json.dumps(bool(state.scheduled_due))),
        (_retrain_state_row_key("schedule_anchor"), json.dumps(float(state.schedule_anchor))),
    ]
    for key, value in rows:
        await pool.execute(
            "INSERT INTO retrain_state(key, value) VALUES($1, $2::jsonb) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
            key, value,
        )


_state = RetrainState()
_initialized = False
_initialization_lock = asyncio.Lock()


async def _ensure_retrain_state() -> RetrainState:
    global _state, _initialized
    async with _initialization_lock:
        if not _initialized:
            _state = await _load_retrain_state()
            _initialized = True
    return _state


def get_state() -> RetrainState:
    return _state


async def _load_persisted_outcomes() -> list[TradeOutcome]:
    """Load valid EA outcomes so feedback survives API/container restarts."""
    from db.connection import get_pool

    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT symbol, entry_time, exit_time, pnl, pnl_pct, direction,
               sl_hit, tp_hit, exit_reason
        FROM trade_outcomes
        WHERE entry_time > TIMESTAMPTZ '2000-01-01'
          AND exit_time > entry_time
        ORDER BY exit_time
        """
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
    state = await _ensure_retrain_state()
    state.outcomes.append(outcome)
    state.filled_count += 1
    await _save_retrain_state(state)

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
            "[RETRAIN] Trigger automático: filled_count=%d >= min_trades=%d — requiere aprobación humana",
            state.filled_count,
            RetrainConfig.min_trades_for_retrain,
        )
        if not settings.retrain_auto_approve:
            logger.warning(
                "[RETRAIN] Auto-retrain bloqueado: retrain_auto_approve=false. "
                "Usar POST /api/v1/ai/retrain/approve para aprobar el retrain pendiente."
            )
            return
        _spawn_retrain_task("trade_count")


async def _trigger_retrain(trigger: str = "manual") -> None:
    """
    Dispara el retrain en background.
    Solo una instancia a la vez (Lock).
    """
    logger.warning("[RETRAIN] ===== RETRAIN AUTOMÁTICO INICIADO =====")
    async with RETRAIN_LOCK:
        state = await _ensure_retrain_state()
        state.retrain_in_progress = True
        state.scheduled_due = False   # el intento consume el disparador del calendario
        await _save_retrain_state(state)
        try:
            await _do_retrain(trigger=trigger)
        finally:
            state.retrain_in_progress = False
            await _save_retrain_state(state)


_RETRAIN_TASK: asyncio.Task | None = None


def _spawn_retrain_task(trigger: str = "manual") -> asyncio.Task | None:
    """Lanza _trigger_retrain en background con referencia y manejo de fallo.

    Sin referencia, el task puede recolectarse y sus excepciones mueren en
    silencio (violación de 'no silent failures'): aquí se conserva la
    referencia, no se duplica si ya hay uno vivo y se alerta en el
    done-callback si termina con error.
    """
    global _RETRAIN_TASK
    if _RETRAIN_TASK is not None and not _RETRAIN_TASK.done():
        logger.warning("[RETRAIN] Ya hay un retrain en curso — se reutiliza el task vivo")
        return _RETRAIN_TASK

    task = asyncio.create_task(_trigger_retrain(trigger))
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


def _load_obs_rms(model_path: str, env_cfg: dict):
    """obs_rms congeladas del bundle certificado en su manifest (None = bundle crudo)."""
    from services.model_registry import load_manifest

    try:
        manifest = load_manifest(model_path)
    except RuntimeError as exc:
        logger.warning("[RETRAIN] Manifest no disponible (%s) — baseline evaluado en crudo", exc)
        return None
    vec_norm_file = manifest.get("vec_norm_file")
    if not vec_norm_file:
        return None

    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    from ml.trading_env_v2 import ForexTradingEnvV2

    pkl_path = os.path.join(os.path.dirname(model_path), vec_norm_file)
    cfg = dict(env_cfg)
    cfg["real_outcomes"] = []
    venv = VecNormalize.load(pkl_path, DummyVecEnv([lambda: ForexTradingEnvV2(**cfg)]))
    venv.training = False
    venv.norm_reward = False
    logger.info("[RETRAIN] Baseline se evaluará con stats del bundle: %s", pkl_path)
    return venv.obs_rms


def _make_train_env(env_cfg: dict, model_path: str):
    """Entorno de entrenamiento SIEMPRE con VecNormalize (P1-3):

    - bundle activo declarando vec_norm_file → continúa con ESAS stats
      (train/serve coherente entre ciclos);
    - bundle crudo o sin manifest → stats nuevas desde cero.
    """
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    from ml.trading_env_v2 import ForexTradingEnvV2
    from services.model_registry import load_manifest

    vec_norm_file = None
    try:
        vec_norm_file = load_manifest(model_path).get("vec_norm_file")
    except RuntimeError as exc:
        logger.warning("[RETRAIN] Manifest no disponible (%s) — VecNormalize nuevo desde cero", exc)

    if vec_norm_file:
        pkl_path = os.path.join(os.path.dirname(model_path), vec_norm_file)
        train_env = VecNormalize.load(pkl_path, DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)]))
        # P2-1: el pickle de VecNormalize arrastra el action_space del bundle
        # origen y set_venv() no lo refresca; sin esta línea PPO construiría
        # la política con la cabeza vieja (p.ej. 4 tras pasar a [dir, volume]).
        train_env.action_space = train_env.venv.action_space
        train_env.training = True
        train_env.norm_reward = True
        logger.warning("[RETRAIN] Continuando con stats de normalización del bundle activo: %s", vec_norm_file)
        return train_env

    logger.warning("[RETRAIN] Bundle activo sin normalización — VecNormalize nuevo (stats desde cero)")
    return VecNormalize(
        DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)]),
        norm_obs=True, norm_reward=True, clip_obs=10.0,
    )


def _evaluate_model(model, df: pd.DataFrame, env_cfg: dict, obs_rms=None) -> dict:
    """Evalúa una política sin feedback real y devuelve métricas comparables.

    - Episodio DETERMINISTA (random_reset=False): candidato y baseline recorren
      el MISMO segmento → la comparación es válida.
    - `obs_rms`: estadísticas congeladas de VecNormalize (None = bundle crudo):
      cada política se evalúa en el espacio de entrada con el que fue entrenada.
    - Devuelve además curvas (equity, rewards por paso) para que el harness
      walk-forward calcule Sharpe/Sortino/DD.
    - wins/losses derivan de info['trades'] e info['win_rate'] (el env NO
      expone claves "wins"/"losses": antes siempre daban 0 — bug P1-4).
    """
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    from ml.trading_env_v2 import ForexTradingEnvV2

    eval_cfg = dict(env_cfg)
    eval_cfg["df"] = df
    eval_cfg["real_outcomes"] = []
    eval_cfg["random_reset"] = False  # episodio fijo: baseline y candidato idénticos
    dummy = DummyVecEnv([lambda: ForexTradingEnvV2(**eval_cfg)])
    if obs_rms is not None:
        env = VecNormalize(dummy, training=False, norm_reward=False, clip_obs=10.0)
        env.obs_rms = obs_rms
    else:
        env = dummy

    initial_balance = float(eval_cfg.get("initial_balance", 0.0))
    observation = env.reset()
    total_reward = 0.0
    openings = 0
    previous_position = 0
    info = {}
    done = False
    equity_curve: list = []
    step_rewards: list = []

    while not done:
        action, _ = model.predict(observation, deterministic=True)
        flat = np.asarray(action, dtype=np.float32).reshape(-1)
        if flat.size >= 1:
            target_position = 1 if flat[0] > 0.33 else (-1 if flat[0] < -0.33 else 0)
        else:
            target_position = 0
        if target_position != 0 and previous_position == 0:
            openings += 1

        observation, reward, done_arr, infos = env.step(flat.reshape(1, -1))
        total_reward += float(reward[0])
        info = infos[0]
        equity_curve.append(float(info.get("equity", initial_balance)))
        step_rewards.append(float(reward[0]))
        previous_position = int(info.get("position", 0))
        done = bool(done_arr[0])

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

    model = PPO.load(model_path, env=train_env)
    logger.info("[RETRAIN] Modelo existente cargado — continuando entrenamiento (con VecNormalize)")
    return model


async def _do_retrain(trigger: str = "manual") -> None:
    """P3-2a: orquesta snapshot + experimento alrededor del pipeline de retrain.

    1. Recolecta velas históricas y congela el snapshot del df que van a
       consumir train y gate (retrain reproducible)
    2. Abre la fila del experimento en training_experiments (traza auditable
       por corrida: disparador + snapshot)
    3. Ejecuta _do_retrain_core (entreno → gate walk-forward → promoción),
       que cierra la fila en sus puntos de decisión (promoted/rejected)
    4. Ante cualquier excepción del pipeline cierra la fila como failed
       (UPDATE condicional sobre status='running': no pisa un cierre ya hecho)

    Un fallo ANTES del snapshot (fetch MT5, escritura) no abre fila — no hay
    experimento sin datos — y queda alertado por _spawn_retrain_task._on_done.
    """
    cfg = RetrainConfig
    logger.warning("[RETRAIN] ===== INICIANDO RETRAIN (trigger=%s) =====", trigger)

    candles = await _fetch_historical_candles(symbol=TRAINING_SYMBOL, count=cfg.lookback_candles)
    if len(candles) < 100:
        raise RuntimeError(f"[RETRAIN] No hay suficientes velas: {len(candles)}")
    df = _build_training_dataframe(candles)

    # P3-2a: snapshot del df exacto que consume train+gate → reproducibilidad.
    snap = data_snapshot.save_snapshot(df, purpose="retrain")
    data_snapshot.prune_snapshots()
    exp_id = await experiment_tracker.start_experiment(trigger, snap.path, snap.sha256)

    try:
        await _do_retrain_core(df, exp_id)
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


async def _do_retrain_core(df: pd.DataFrame, exp_id: int) -> None:
    """Pipeline de retrain sobre un df ya congelado como snapshot.

    Terminaliza el experimento `exp_id` en los puntos de decisión del gate:
    'rejected' al rechazar el candidato, 'promoted' tras record_promotion.
    """
    from services.model_backup import backup_model as upload_model

    cfg = RetrainConfig
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

    real_outcomes = await _load_persisted_outcomes()
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
        spread_pips=1.5,
        commission_per_lot=cfg.commission,
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
    )

    from stable_baselines3 import PPO

    train_env = _make_train_env(env_cfg, MODEL_PATH)

    # P2-1: carga continuable o política nueva si cambió el contrato de acción.
    model = _load_or_create_model(MODEL_PATH, train_env, cfg)

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
    if os.path.exists(MODEL_PATH):
        baseline_model = PPO.load(MODEL_PATH)
        baseline_rms = _load_obs_rms(MODEL_PATH, env_cfg)

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
        state = await _ensure_retrain_state()
        state.last_retrain_time = time.time()
        await _save_retrain_state(state)
        await _log_retrain_event("retrain_rejected", {
            "reason": decision.reason,
            "gate": decision.details,
            "report": report.to_dict(),
        })
        return

    # Promoción del BUNDLE completo: stats + zip + manifest certifican el conjunto.
    # Si el proceso muere entre archivos, validate_bundle falla en el próximo arranque.
    norm_path = os.path.splitext(MODEL_PATH)[0] + "_vec_norm.pkl"
    norm_tmp = norm_path + ".tmp"
    train_env.save(norm_tmp)
    os.replace(norm_tmp, norm_path)

    candidate_path = MODEL_PATH.removesuffix(".zip") + ".candidate.zip"
    model.save(candidate_path)
    os.replace(candidate_path, MODEL_PATH)

    # P0-5: certificar el bundle promovido. Sin manifest válido el próximo
    # arranque fallará en validate_bundle (fail-fast: nunca sirve binarios sucios).
    from services.model_registry import build_manifest, record_promotion, write_manifest
    # P3-2c: baseline de drift congelado sobre el df de ENTRENO — la referencia
    # contra la que el monitor PSI compara las velas vivas. Si no hay baseline
    # utilizable, no se promueve (mejor no promover que promover a ciegas).
    baseline_info = drift_monitor.save_baseline(train_df)
    manifest = build_manifest(
        MODEL_PATH,
        obs_shape=getattr(model.observation_space, "shape", ()),
        vec_norm_file=os.path.basename(norm_path),
        source="auto_retrain_normalized",
        extra={
            "walk_forward": report.to_dict(),
            "gate": {"ok": decision.ok, "reason": decision.reason},
            "drift_baseline": baseline_info,
        },
    )
    write_manifest(MODEL_PATH, manifest)

    # P1-2: historial de promociones (model_registry) + hot-reload del bundle en
    # memoria. Sin la recarga, la promoción no surte efecto hasta reiniciar
    # (disco ≠ memoria). El swap en ai.py es atómico y valida antes de tocar la caché.
    await record_promotion(
        manifest,
        {"ok": decision.ok, "reason": decision.reason,
         "checks": decision.details.get("checks")},
    )
    # P3-1b: hot-reload vía ModelStorePort — el servicio ya no importa del
    # router (el adapter resuelve `routers.ai` en el momento de la llamada,
    # así que el patch de los tests sigue viéndose).
    from core.adapters import AiModelStoreAdapter
    AiModelStoreAdapter().reload(TRAINING_SYMBOL)

    # P3-2a: la promoción es el punto de decisión del experimento — se cierra
    # aquí, antes de los pasos posteriores (state/backup/log): si esos fallan,
    # el wrapper intenta 'failed' pero el UPDATE condicional no lo pisa.
    await experiment_tracker.finish_experiment(
        exp_id, "promoted",
        gate_reason=decision.reason,
        gate_metrics=report.to_dict(),
        model_sha256=manifest["model_sha256"],
    )

    state = await _ensure_retrain_state()
    state.last_retrain_time = time.time()
    state.filled_count = 0          # nuevo ciclo: los próximos N cierres cuentan
    state.outcomes = []
    await _save_retrain_state(state)

    # 6. Backup a GCS
    backup_url = await upload_model(MODEL_PATH)
    logger.warning(
        "[RETRAIN] ===== RETRAIN COMPLETADO ===== model=%s backup=%s sum_reward=%.3f gate=%s",
        MODEL_PATH,
        backup_url,
        report.candidate["sum_reward"],
        decision.reason,
    )

    await _log_retrain_event("retrain_promoted", {
        "reason": decision.reason,
        "gate": decision.details,
        "report": report.to_dict(),
        "bundle": {
            "model": os.path.basename(MODEL_PATH),
            "vec_norm_file": os.path.basename(norm_path),
            "source": "auto_retrain_normalized",
        },
        "backup_url": backup_url,
    })


async def _fetch_historical_candles(symbol: str, count: int) -> list:
    """Obtiene velas históricas de MT5 MCP."""
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{settings.mt5_http_url}/api/v1/market/candles/latest",
            params={"symbol_name": symbol, "timeframe": "H1", "count": count},
        )
        res.raise_for_status()
        data = res.json()
        return data if isinstance(data, list) else data.get("candles", [])


def _build_training_dataframe(candles: list) -> pd.DataFrame:
    """Build the canonical v3 feature dataframe used by training and inference."""
    from ml.trading_env_v2 import engineer_market_features

    df = pd.DataFrame(candles)
    return engineer_market_features(df)


async def _log_retrain_event(event: str, data: dict) -> None:
    """Persiste un evento de ciclo de retrain en audit_log (P1-1).

    - `retrain_promoted` / `retrain_rejected`: decisión de gate con el informe
      walk-forward completo (métricas por pliego + agregados) — antes ambos
      caminos escribían el mismo `retrain_completed` sin métricas ni hash.
    """
    from db.connection import get_pool

    pool = get_pool()
    state = await _ensure_retrain_state()
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


# ─── HTTP Endpoint para forzar retrain manualmente ────────────────────────────

async def force_retrain() -> dict:
    """Fuerza un retrain inmediato (para uso manual/debug).

    No salta el gate de promoción: eso vive dentro de _do_retrain.
    """
    state = await _ensure_retrain_state()
    if state.retrain_in_progress:
        return {"status": "already_running"}

    _spawn_retrain_task("forced")
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


async def approve_retrain() -> dict:
    """Aprueba el retrain pendiente (umbral alcanzado o calendario vencido).

    Solo aprueba si hay un disparo realmente pendiente (umbral de trades
    alcanzado O scheduled_due del calendario) y no hay otro en curso;
    el gate de promoción del candidato se aplica igual dentro de _do_retrain.
    """
    state = await _ensure_retrain_state()
    if state.retrain_in_progress:
        return {"status": "already_running", "filled_count": state.filled_count}
    if (state.filled_count < RetrainConfig.min_trades_for_retrain
            and not state.scheduled_due):
        return {
            "status": "no_pending",
            "filled_count": state.filled_count,
            "min_trades_for_retrain": RetrainConfig.min_trades_for_retrain,
        }
    _spawn_retrain_task("approved")
    return {
        "status": "approved",
        "filled_count": state.filled_count,
        "scheduled_due": state.scheduled_due,
    }

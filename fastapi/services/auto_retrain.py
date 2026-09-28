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

logger = logging.getLogger(__name__)

MODEL_PATH = "/app/ml/ppo_trading_bot_v3.zip"   # ← EA usa v3, no v2
RETRAIN_LOCK = asyncio.Lock()


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


def _retrain_state_row_key(key: str) -> str:
    return f"retrain_state.{key}"


async def _load_retrain_state() -> RetrainState:
    from db.connection import get_pool

    pool = get_pool()
    rows = await pool.fetch("SELECT key, value FROM retrain_state WHERE key = ANY($1::text[])", ["retrain_state.filled_count", "retrain_state.last_retrain_time"])
    payload = {row["key"]: row["value"] for row in rows}
    state = RetrainState(
        filled_count=int(payload.get(_retrain_state_row_key("filled_count"), 0)),
        last_retrain_time=float(payload.get(_retrain_state_row_key("last_retrain_time"), 0.0)),
        outcomes=[],
        retrain_in_progress=False,
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
            logger.warning("[RETRAIN] Auto-retrain bloqueado: retrain_auto_approve=false. Usar endpoint /admin/retrain/approve para aprobar.")
            return
        asyncio.create_task(_trigger_retrain())


async def _trigger_retrain() -> None:
    """
    Dispara el retrain en background.
    Solo una instancia a la vez (Lock).
    """
    logger.warning("[RETRAIN] ===== RETRAIN AUTOMÁTICO INICIADO =====")
    async with RETRAIN_LOCK:
        state = await _ensure_retrain_state()
        state.retrain_in_progress = True
        await _save_retrain_state(state)
        try:
            await _do_retrain()
        finally:
            state.retrain_in_progress = False
            await _save_retrain_state(state)


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
        train_env.training = True
        train_env.norm_reward = True
        logger.warning("[RETRAIN] Continuando con stats de normalización del bundle activo: %s", vec_norm_file)
        return train_env

    logger.warning("[RETRAIN] Bundle activo sin normalización — VecNormalize nuevo (stats desde cero)")
    return VecNormalize(
        DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)]),
        norm_obs=True, norm_reward=True, clip_obs=10.0,
    )


def _gate_decision(candidate: dict, baseline: dict | None, min_openings: int) -> tuple[bool, str]:
    """Gate de promoción: candidato válido + mejora ESTRICTA sobre el baseline.

    Los empates NO promueven: si el entrenamiento no mejora la recompensa, el
    modelo activo se conserva (sin churn de bundles certificados).
    """
    if not (candidate["openings"] >= min_openings and np.isfinite(candidate["reward"])):
        return False, "invalid_openings_or_reward"
    if baseline is not None and not (candidate["reward"] > baseline["reward"]):
        return False, "no_strict_improvement"
    return True, "promoted"


def _evaluate_model(model, df: pd.DataFrame, env_cfg: dict, obs_rms=None) -> dict:
    """Evalúa una política sin feedback real y devuelve métricas comparables.

    - Episodio DETERMINISTA (random_reset=False): candidato y baseline recorren
      el MISMO segmento → la comparación es válida.
    - `obs_rms`: estadísticas congeladas de VecNormalize (None = bundle crudo):
      cada política se evalúa en el espacio de entrada con el que fue entrenada.
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

    observation = env.reset()
    total_reward = 0.0
    openings = 0
    previous_position = 0
    info = {}
    done = False

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
        previous_position = int(info.get("position", 0))
        done = bool(done_arr[0])

    closed_trades = int(info.get("wins", 0)) + int(info.get("losses", 0))
    wins = int(info.get("wins", 0))
    return {
        "openings": openings,
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": int(info.get("losses", 0)),
        "win_rate": wins / closed_trades if closed_trades else 0.0,
        "reward": total_reward,
    }


async def _do_retrain() -> None:
    """
    Ejecuta el retrain efectivo.
    1. Recolecta velas históricas
    2. Construye dataset de training
    3. Entrena modelo PPO v3 con ForexTradingEnvV2 (3 pips spread)
    4. Reemplaza ppo_trading_bot_v3.zip
    5. Loguea resultado
    """
    from services.model_backup import backup_model as upload_model

    cfg = RetrainConfig
    logger.warning("[RETRAIN] ===== INICIANDO RETRAIN =====")

    candles = await _fetch_historical_candles(symbol="EURUSD", count=cfg.lookback_candles)
    if len(candles) < 100:
        raise RuntimeError(f"[RETRAIN] No hay suficientes velas: {len(candles)}")
    df = _build_training_dataframe(candles)

    split_idx = max(cfg.retrain_window_size + 1, int(len(df) * 0.8))
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    eval_df = df.iloc[split_idx - cfg.retrain_window_size:].reset_index(drop=True)

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
        max_lot=0.5,
        max_sl_pips=100.0,
        max_tp_pips=200.0,
        pip_size=0.0001,
        spread_pips=1.5,
        commission_per_lot=cfg.commission,
        max_episode_steps=1000,
        max_holding_steps=120,
        reward_scale=100.0,
        random_reset=True,
        real_outcomes=real_outcomes,
        real_outcome_weight=cfg.real_outcome_weight,
    )

    from stable_baselines3 import PPO

    train_env = _make_train_env(env_cfg, MODEL_PATH)

    if os.path.exists(MODEL_PATH):
        model = PPO.load(MODEL_PATH, env=train_env)
        logger.info("[RETRAIN] Modelo existente cargado — continuando entrenamiento (con VecNormalize)")
    else:
        logger.warning("[RETRAIN] No hay modelo previo — creando nuevo")
        model = PPO(
            "MlpPolicy",
            train_env,
            learning_rate=cfg.learning_rate,
            verbose=cfg.verbose,
        )

    logger.info("[RETRAIN] Entrenando %d timesteps...", cfg.total_timesteps)
    model.learn(
        total_timesteps=cfg.total_timesteps,
        progress_bar=False,
    )

    candidate_metrics = _evaluate_model(model, eval_df, env_cfg, obs_rms=train_env.obs_rms)
    baseline_metrics = None
    if os.path.exists(MODEL_PATH):
        baseline_metrics = _evaluate_model(
            PPO.load(MODEL_PATH), eval_df, env_cfg,
            obs_rms=_load_obs_rms(MODEL_PATH, env_cfg),
        )

    logger.warning(
        "[RETRAIN-EVAL] candidate openings=%d win_rate=%.3f reward=%.3f trades=%d | baseline=%s",
        candidate_metrics["openings"], candidate_metrics["win_rate"],
        candidate_metrics["reward"], candidate_metrics["closed_trades"],
        baseline_metrics,
    )

    gate_ok, gate_reason = _gate_decision(candidate_metrics, baseline_metrics, cfg.min_validation_openings)
    if not gate_ok:
        logger.warning(
            "[RETRAIN] Candidato rechazado (%s): modelo activo conservado | candidate=%s baseline=%s",
            gate_reason, candidate_metrics, baseline_metrics,
        )
        await _save_retrain_metrics(df, cfg)
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
    from services.model_registry import build_manifest, write_manifest
    write_manifest(MODEL_PATH, build_manifest(
        MODEL_PATH,
        obs_shape=getattr(model.observation_space, "shape", ()),
        vec_norm_file=os.path.basename(norm_path),
        source="auto_retrain_normalized",
        extra={"candidate_metrics": candidate_metrics, "baseline_metrics": baseline_metrics},
    ))

    state = await _ensure_retrain_state()
    state.last_retrain_time = time.time()
    state.filled_count = 0          # nuevo ciclo: los próximos N cierres cuentan
    state.outcomes = []
    await _save_retrain_state(state)

    # 6. Backup a GCS
    backup_url = await upload_model(MODEL_PATH)
    logger.warning(
        "[RETRAIN] ===== RETRAIN COMPLETADO ===== model=%s backup=%s candidates=%s",
        MODEL_PATH,
        backup_url,
        candidate_metrics,
    )

    await _save_retrain_metrics(df, cfg)


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


async def _save_retrain_metrics(df: pd.DataFrame, cfg: RetrainConfig) -> None:
    """Guarda métricas de retrain en audit_log."""
    from db.connection import get_pool

    pool = get_pool()
    state = await _ensure_retrain_state()
    metrics = {
        "event": "retrain_completed",
        "lookback_candles": len(df),
        "trades_since_last_retrain": state.filled_count,
        "last_retrain_time": state.last_retrain_time,
        "outcomes_summary": {
            "total": len(state.outcomes),
            "avg_pnl": np.mean([o.pnl for o in state.outcomes[-cfg.trades_before_retrain:]]) if state.outcomes else 0,
            "win_rate": np.mean([o.pnl > 0 for o in state.outcomes[-cfg.trades_before_retrain:]]) if state.outcomes else 0,
        },
        "config": {
            "trades_before_retrain": cfg.trades_before_retrain,
            "n_epochs": cfg.n_epochs,
            "total_timesteps": cfg.total_timesteps,
            "learning_rate": cfg.learning_rate,
            "real_outcome_feedback": bool(state.outcomes),
        },
    }

    await pool.execute(
        "INSERT INTO audit_log(cycle_id, event, data) VALUES($1, $2, $3)",
        f"retrain_{int(state.last_retrain_time)}",
        "retrain_completed",
        json.dumps(metrics),
    )


# ─── HTTP Endpoint para forzar retrain manualmente ────────────────────────────

async def force_retrain() -> dict:
    """Fuerza un retrain inmediato (para uso manual/debug)."""
    state = await _ensure_retrain_state()
    if state.retrain_in_progress:
        return {"status": "already_running"}

    asyncio.create_task(_trigger_retrain())
    return {"status": "triggered", "filled_count": state.filled_count}

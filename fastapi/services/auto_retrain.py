"""
Auto-Retraining Service — reentrena PPO cada N trades completados.

Funciona así:
1. Cada vez que una orden cambia a 'filled', incrementamos un contador en memoria
2. Cuando el contador alcanza TRADES_BEFORE_RETRAIN, disparamos retrain
3. El retrain usa los últimos datos de velas + feedback de rendimiento
4. Reemplaza el modelo en disco y notifica
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


# Estado global en memoria (se pierde en restart — aceptable para auto-retrain)
_state = RetrainState()


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
) -> int:
    """Persiste el trade outcome a la tabla trade_outcomes en PostgreSQL. Retorna el trade_id."""
    from db.connection import get_pool
    pool = get_pool()
    entry_ts = _parse_timestamp(entry_time)
    exit_ts = _parse_timestamp(exit_time)
    trade_id = await pool.fetchval(
        """
        INSERT INTO trade_outcomes
            (symbol, direction, entry_time, exit_time, pnl, pnl_pct, exit_reason, sl_hit, tp_hit)
        VALUES ($1,$2,to_timestamp($3::double precision),to_timestamp($4::double precision),$5,$6,$7,$8,$9)
        RETURNING id
        """,
        symbol, direction,
        entry_ts, exit_ts,
        pnl, pnl_pct,
        exit_reason, sl_hit, tp_hit,
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
) -> None:
    """
    Llamar cada vez que una orden se cierra como 'filled'.
    Incrementa el contador y dispara retrain si corresponde.
    """
    entry_ts = _parse_timestamp(entry_time)
    exit_ts = _parse_timestamp(exit_time)

    trade_id = await _persist_trade_outcome(
        symbol, entry_ts, exit_ts, pnl, pnl_pct, direction, sl_hit, tp_hit, exit_reason,
    )

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
    _state.outcomes.append(outcome)
    _state.filled_count += 1

    logger.info(
        "[RETRAIN] Trade #%d cerrado: %s %s pnl=%.2f (%s) — %d/%d trades para retrain",
        _state.filled_count,
        symbol,
        direction,
        pnl,
        exit_reason,
        _state.filled_count % RetrainConfig.trades_before_retrain,
        RetrainConfig.trades_before_retrain,
    )

    # Trigger retrain si corresponde
    if _state.filled_count >= RetrainConfig.min_trades_for_retrain:
        logger.warning(
            "[RETRAIN] Trigger automático: filled_count=%d >= min_trades=%d — disparando _trigger_retrain()",
            _state.filled_count,
            RetrainConfig.min_trades_for_retrain,
        )
        asyncio.create_task(_trigger_retrain())


async def _trigger_retrain() -> None:
    """
    Dispara el retrain en background.
    Solo una instancia a la vez (Lock).
    """
    if _state.retrain_in_progress:
        logger.warning("[RETRAIN] Retrain ya en progreso — skip")
        return

    logger.warning("[RETRAIN] ===== RETRAIN AUTOMÁTICO INICIADO =====")
    async with RETRAIN_LOCK:
        _state.retrain_in_progress = True
        try:
            await _do_retrain()
        finally:
            _state.retrain_in_progress = False


def _evaluate_model(model, df: pd.DataFrame, env_cfg: dict) -> dict:
    """Evalúa una política sin feedback real y devuelve métricas comparables."""
    from ml.trading_env_v2 import ForexTradingEnvV2

    eval_cfg = dict(env_cfg)
    eval_cfg["df"] = df
    eval_cfg["real_outcomes"] = []
    env = ForexTradingEnvV2(**eval_cfg)
    observation, _ = env.reset()
    total_reward = 0.0
    openings = 0
    previous_position = 0
    terminated = False
    truncated = False

    while not terminated and not truncated:
        action, _ = model.predict(observation, deterministic=True)
        action = np.asarray(action, dtype=np.float32).reshape(-1)
        if action.size >= 1:
            target_position = 1 if action[0] > 0.33 else (-1 if action[0] < -0.33 else 0)
        else:
            target_position = 0
        if target_position != 0 and previous_position == 0:
            openings += 1

        observation, reward, terminated, truncated, info = env.step(action)
        total_reward += float(reward)
        previous_position = int(info.get("position", 0))

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
    from ml.trading_env_v2 import ForexTradingEnvV2  # ← V2 con spread real

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
    from stable_baselines3.common.vec_env import DummyVecEnv

    if os.path.exists(MODEL_PATH):
        env = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
        model = PPO.load(MODEL_PATH, env=env)
        logger.info("[RETRAIN] Modelo existente cargado — continuando entrenamiento")
    else:
        logger.warning("[RETRAIN] No hay modelo previo — creando nuevo")
        env = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
        model = PPO(
            "MlpPolicy",
            env,
            learning_rate=cfg.learning_rate,
            verbose=cfg.verbose,
        )

        # 4. Fine-tune con datos recientes
        if "env" not in dir() or env is None:
            env = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
        model.set_env(env)

        logger.info("[RETRAIN] Entrenando %d timesteps...", cfg.total_timesteps)
        model.learn(
            total_timesteps=cfg.total_timesteps,
            progress_bar=False,
        )

        # 5. Evaluar el candidato y el modelo activo antes de reemplazarlo.
        candidate_metrics = _evaluate_model(model, eval_df, env_cfg)
        baseline_metrics = None
        if os.path.exists(MODEL_PATH):
            from stable_baselines3 import PPO
            baseline_metrics = _evaluate_model(PPO.load(MODEL_PATH), eval_df, env_cfg)

        logger.warning(
            "[RETRAIN-EVAL] candidate openings=%d win_rate=%.3f reward=%.3f trades=%d | baseline=%s",
            candidate_metrics["openings"], candidate_metrics["win_rate"],
            candidate_metrics["reward"], candidate_metrics["closed_trades"],
            baseline_metrics,
        )

        candidate_is_valid = (
            candidate_metrics["openings"] >= cfg.min_validation_openings
            and np.isfinite(candidate_metrics["reward"])
        )
        improves_baseline = (
            baseline_metrics is None
            or candidate_metrics["reward"] >= baseline_metrics["reward"]
        )
        if not candidate_is_valid or not improves_baseline:
            logger.warning(
                "[RETRAIN] Candidato rechazado: valid=%s improves_baseline=%s; modelo activo conservado",
                candidate_is_valid, improves_baseline,
            )
            return

        candidate_path = MODEL_PATH.removesuffix(".zip") + ".candidate.zip"
        model.save(candidate_path)
        os.replace(candidate_path, MODEL_PATH)
        _state.last_retrain_time = time.time()

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
    metrics = {
        "event": "retrain_completed",
        "lookback_candles": len(df),
        "trades_since_last_retrain": _state.filled_count,
        "last_retrain_time": _state.last_retrain_time,
        "outcomes_summary": {
            "total": len(_state.outcomes),
            "avg_pnl": np.mean([o.pnl for o in _state.outcomes[-cfg.trades_before_retrain:]]) if _state.outcomes else 0,
            "win_rate": np.mean([o.pnl > 0 for o in _state.outcomes[-cfg.trades_before_retrain:]]) if _state.outcomes else 0,
        },
        "config": {
            "trades_before_retrain": cfg.trades_before_retrain,
            "n_epochs": cfg.n_epochs,
            "total_timesteps": cfg.total_timesteps,
            "learning_rate": cfg.learning_rate,
            "real_outcome_feedback": bool(_state.outcomes),
        },
    }

    await pool.execute(
        "INSERT INTO audit_log(cycle_id, event, data) VALUES($1, $2, $3)",
        f"retrain_{int(_state.last_retrain_time)}",
        "retrain_completed",
        json.dumps(metrics),
    )


# ─── HTTP Endpoint para forzar retrain manualmente ────────────────────────────

async def force_retrain() -> dict:
    """Fuerza un retrain inmediato (para uso manual/debug)."""
    if _state.retrain_in_progress:
        return {"status": "already_running"}

    asyncio.create_task(_trigger_retrain())
    return {"status": "triggered", "filled_count": _state.filled_count}

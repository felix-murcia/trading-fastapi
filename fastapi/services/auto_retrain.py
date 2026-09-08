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
    retrain_window_size: int = 10         # Window size del entorno
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


def _parse_timestamp(ts: float | str) -> float:
    """Acepta Unix timestamp (float or numeric string) o ISO string, devuelve Unix timestamp (float)."""
    if isinstance(ts, str):
        # Try numeric string first (Unix timestamp as string like "1725450000")
        try:
            return float(ts)
        except ValueError:
            # Fall back to ISO format
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
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
    try:
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
    except Exception as exc:
        logger.error("[AUTO-RETRAIN] No se pudo persistir trade_outcome: %s — entry_ts=%.0f, exit_ts=%.0f", exc, entry_ts, exit_ts)
        raise  # Re-lanzar para no ocultar errores de la DB


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

    # Persistir a la tabla trade_outcomes (re-lanza excepciones)
    trade_id = None
    try:
        trade_id = await _persist_trade_outcome(
            symbol, entry_ts, exit_ts, pnl, pnl_pct, direction, sl_hit, tp_hit, exit_reason,
        )
    except Exception:
        logger.error("[RETRAIN] _persist_trade_outcome falló — el trade NO se guardó en DB")
        raise

    # Opción 5: Post-Trade Journal — análisis asíncrono con Qwen
    try:
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
    except Exception as j_err:
        from services.alerting import send_alert, AlertLevel
        send_alert(
            AlertLevel.WARNING,
            "TRADE-JOURNAL",
            f"Post-trade journal falló (no bloquea recording): {j_err}",
            exc=j_err,
            context={"trade_id": trade_id, "symbol": symbol}
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
        if _state.filled_count % RetrainConfig.trades_before_retrain == 0:
            asyncio.create_task(_trigger_retrain())


async def _trigger_retrain() -> None:
    """
    Dispara el retrain en background.
    Solo una instancia a la vez (Lock).
    """
    if _state.retrain_in_progress:
        logger.warning("[RETRAIN] Retrain ya en progreso — skip")
        return

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

    # 1. Recolectar velas del último período
    try:
        candles = await _fetch_historical_candles(symbol="EURUSD", count=cfg.lookback_candles)
        if len(candles) < 100:
            logger.error("[RETRAIN] No hay suficientes velas: %d", len(candles))
            return
        df = _build_training_dataframe(candles)
    except Exception as exc:
        logger.error("[RETRAIN] Error obteniendo velas: %s", exc)
        return

    # 2. Separar entrenamiento y validación para no reemplazar el modelo
    # activo sin medir aperturas, win rate y reward en datos no vistos.
    split_idx = max(cfg.retrain_window_size + 1, int(len(df) * 0.8))
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    eval_df = df.iloc[split_idx - cfg.retrain_window_size:].reset_index(drop=True)

    # Los outcomes reales solo son feedback real si existen cierres del EA.
    real_outcomes = list(_state.outcomes)  # copia
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
        commission=cfg.commission,
        real_outcomes=real_outcomes,
        real_outcome_weight=cfg.real_outcome_weight,
    )

    # 3. Cargar modelo existente o crear nuevo
    try:
        from stable_baselines3 import PPO
        from stable_baselines3.common.vec_env import DummyVecEnv

        if os.path.exists(MODEL_PATH):
            try:
                # Crear env primero para que SB3 valide dimensiones
                env = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
                model = PPO.load(MODEL_PATH, env=env)
                logger.info("[RETRAIN] Modelo existente cargado — continuando entrenamiento")
            except ValueError as dim_err:
                # Mismatch de dimensiones (modelo viejo con features diferentes)
                logger.warning(
                    "[RETRAIN] Dimensiones incompatibles (%s) — reentrenando desde cero",
                    dim_err,
                )
                env = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
                model = PPO(
                    "MlpPolicy",
                    env,
                    learning_rate=cfg.learning_rate,
                    verbose=cfg.verbose,
                )
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
            try:
                from stable_baselines3 import PPO
                baseline_metrics = _evaluate_model(PPO.load(MODEL_PATH), eval_df, env_cfg)
            except Exception as baseline_err:
                logger.warning("[RETRAIN] No se pudo evaluar baseline: %s", baseline_err)

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
        try:
            backup_url = await upload_model(MODEL_PATH)
            logger.warning("[RETRAIN] ===== RETRAIN COMPLETADO ===== model=%s backup=%s", MODEL_PATH, backup_url)
        except Exception as backup_err:
            logger.error("[RETRAIN] Backup falló: %s — modelo guardado localmente", backup_err)
            logger.warning("[RETRAIN] ===== RETRAIN COMPLETADO (sin backup) =====")

        # 7. Guardar métricas de retrain en DB
        await _save_retrain_metrics(df, cfg)

    except Exception as exc:
        logger.error("[RETRAIN] Error en entrenamiento: %s", exc)
        raise


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
    """Construye DataFrame con las 12 features que el modelo v3 necesita."""
    df = pd.DataFrame(candles)
    df['returns'] = df['close'].pct_change()
    df['range'] = df['high'] - df['low']
    df['sma20'] = df['close'].rolling(20).mean()
    df['dist_sma20'] = (df['close'] - df['sma20']) / df['sma20']

    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    df['tr'] = df['high'] - df['low']
    df['atr'] = df['tr'].rolling(14).mean()

    # ── RSI (14) ─────────────────────────────────────────────────────────────
    delta = df['close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df['rsi14'] = 100 - (100 / (1 + rs))

    # ── Bollinger Bands Position ─────────────────────────────────────────────
    bb_sma = df['close'].rolling(20).mean()
    bb_std = df['close'].rolling(20).std()
    df['bb_pos'] = (df['close'] - bb_sma) / (2 * bb_std.replace(0, 1e-10))

    # ── Lagged Returns ───────────────────────────────────────────────────────
    df['lag_return_1'] = df['returns'].shift(1)
    df['lag_return_2'] = df['returns'].shift(2)
    df['lag_return_3'] = df['returns'].shift(3)
    df['lag_return_5'] = df['returns'].shift(5)

    df = df.dropna()
    return df


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

    try:
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1, $2, $3)",
            f"retrain_{int(_state.last_retrain_time)}",
            "retrain_completed",
            json.dumps(metrics),
        )
    except Exception as exc:
        logger.warning("[RETRAIN] No se pudo guardar métricas: %s", exc)


# ─── HTTP Endpoint para forzar retrain manualmente ────────────────────────────

async def force_retrain() -> dict:
    """Fuerza un retrain inmediato (para uso manual/debug)."""
    if _state.retrain_in_progress:
        return {"status": "already_running"}

    asyncio.create_task(_trigger_retrain())
    return {"status": "triggered", "filled_count": _state.filled_count}

"""
Entrenamiento PPO v3 — ACCIÓN CONTINUA AUTÓNOMA (Refactorizado)
==============================================================
- Compatibilidad absoluta con ForexTradingEnvV3 (lotes y delta equity).
- Guarda estadísticas obligatorias de VecNormalize para inferencia.
- EvalCallback para selección del mejor modelo en datos Out-of-Sample.
- Backtest completo post-entrenamiento sobre todo el dataset de validación.
"""

from datetime import datetime
import json
import os
import pickle
import sys
import urllib.request
import gymnasium as gym
import numpy as np
import pandas as pd

# Añadir el path para poder importar trading_env_v2 / v3
sys.path.insert(0, '/app/ml')
from trading_env_v2 import (
    MARKET_FEATURES,
    ForexTradingEnvV2,
    engineer_market_features,
    get_market_features,
)

from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import EvalCallback, BaseCallback
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

MT5_HTTP_URL = os.getenv("MT5_HTTP_URL")
TOKEN = os.getenv("INTERNAL_TOKEN")
FEATURE_NAMES = list(MARKET_FEATURES)


def fetch_mt5_candles(symbol: str = "EURUSD", timeframe: str = "H1", count: int = 50000) -> pd.DataFrame:
    """Descarga el historial de velas desde el microservicio MT5."""
    if not MT5_HTTP_URL or not TOKEN:
        print("[Train V3] ERROR: MT5_HTTP_URL o INTERNAL_TOKEN no configurados.")
        sys.exit(1)

    url = f"{MT5_HTTP_URL}/api/v1/market/candles/latest"
    params = f"symbol_name={symbol}&timeframe={timeframe}&count={count}"
    req = urllib.request.Request(f"{url}?{params}")
    req.add_header("X-Internal-Token", TOKEN)

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        if not data:
            raise ValueError("No candles returned from server")
        df = pd.DataFrame(data)
        df['time'] = pd.to_datetime(df['time'])
        df = df.sort_values('time').reset_index(drop=True)
        print(f"[Train V3] {len(df)} velas cargadas ({df['time'].min()} → {df['time'].max()})")
        return df
    except Exception as e:
        print(f"[Train V3] Error fetching candles: {e}")
        sys.exit(1)


def run_out_of_sample_backtest(model: PPO, eval_env: VecNormalize, df_eval: pd.DataFrame) -> dict:
    """Ejecuta un backtest secuencial completo sobre el conjunto de evaluación."""
    print("\n" + "=" * 60)
    print("[Train V3] INICIANDO BACKTEST OUT-OF-SAMPLE (Evaluación completa)...")
    print("=" * 60)

    obs = eval_env.reset()
    done = False
    equity_curve = []
    
    while not done:
        # Predicción determinista para evaluación de política
        action, _ = model.predict(obs, deterministic=True)
        obs, rewards, dones, infos = eval_env.step(action)
        
        info = infos[0]
        equity_curve.append(info['equity'])
        done = dones[0]

    equity_series = pd.Series(equity_curve)
    returns = equity_series.pct_change().dropna()
    initial_balance = 10000.0
    final_equity = equity_series.iloc[-1]
    net_pnl = final_equity - initial_balance
    net_roi = (net_pnl / initial_balance) * 100.0

    # Métricas clave
    peak = equity_series.cummax()
    drawdown = (peak - equity_series) / peak
    max_drawdown = drawdown.max() * 100.0
    sharpe = (returns.mean() / (returns.std() + 1e-8)) * np.sqrt(24 * 252)  # Anualizado para H1

    metrics = {
        'total_trades': info.get('trades', 0),
        'win_rate': info.get('win_rate', 0.0) * 100.0,
        'initial_balance': initial_balance,
        'final_equity': final_equity,
        'net_pnl': net_pnl,
        'roi_pct': net_roi,
        'max_drawdown_pct': max_drawdown,
        'sharpe_ratio': sharpe,
    }

    print(f"Resultado Backtest:")
    print(f" - Trades totales:   {metrics['total_trades']}")
    print(f" - Win Rate:         {metrics['win_rate']:.2f}%")
    print(f" - Retorno Neto:     ${metrics['net_pnl']:.2f} ({metrics['roi_pct']:.2f}%)")
    print(f" - Max Drawdown:     {metrics['max_drawdown_pct']:.2f}%")
    print(f" - Ratio de Sharpe:  {metrics['sharpe_ratio']:.2f}")
    print("=" * 60)
    return metrics


def main():
    print("=" * 60)
    print("PPO V3 — PIPELINE DE ENTRENAMIENTO Y CALIBRACIÓN")
    print("=" * 60)

    # ── 1. Carga y Feature Engineering ─────────────────────────
    raw_df = fetch_mt5_candles(symbol="EURUSD", timeframe="H1", count=50000)
    df = engineer_market_features(raw_df)
    print(f"[Train V3] {len(df)} velas tras procesar indicadores técnicos.")

    # Validar presencia de columnas
    missing = set(FEATURE_NAMES) - set(df.columns)
    if missing:
        print(f"[Train V3] ERROR: Faltan features en el DataFrame: {missing}")
        sys.exit(1)

    # Split temporal secuencial (80% Train, 20% Validación)
    split_idx = int(len(df) * 0.8)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    eval_df = df.iloc[split_idx:].reset_index(drop=True)
    print(f"[Train V3] Train: {len(train_df)} velas | Eval: {len(eval_df)} velas")

    # ── 2. Configuración de Entornos ───────────────────────────
    # Configuración alineada al entorno refactorizado
    train_env_config = {
        'window_size': 20,
        'initial_balance': 10000.0,
        'lot_size': 100000.0,
        'max_lot': 0.5,
        'max_sl_pips': 100.0,
        'max_tp_pips': 200.0,
        'pip_size': 0.0001,
        'spread_pips': 1.5,
        'commission_per_lot': 7.0,
        'max_episode_steps': 1000,
        'max_holding_steps': 120,
        'random_reset': True,   # Descorrelaciona episodios en train
    }

    eval_env_config = train_env_config.copy()
    eval_env_config['random_reset'] = False  # Evaluación determinista y continua
    eval_env_config['max_episode_steps'] = len(eval_df) - 25

    # Instanciación con DummyVecEnv
    train_env = DummyVecEnv([lambda: ForexTradingEnvV2(df=train_df, **train_env_config)])
    eval_env = DummyVecEnv([lambda: ForexTradingEnvV2(df=eval_df, **eval_env_config)])

    # Normalización: entrenamos RMS en train, congelamos en evaluación
    train_env = VecNormalize(train_env, norm_obs=True, norm_reward=True, clip_obs=10.0)
    eval_env = VecNormalize(eval_env, norm_obs=True, norm_reward=False, clip_obs=10.0, training=False)
    eval_env.obs_rms = train_env.obs_rms

    # ── 3. Callbacks y Monitorización ──────────────────────────
    output_dir = os.getenv("PPO_OUTPUT_DIR", "/app/ml")
    best_model_dir = os.path.join(output_dir, "best_model_checkpoints")
    os.makedirs(best_model_dir, exist_ok=True)

    # EvalCallback evalúa periódicamente y guarda el mejor checkpoint
    eval_callback = EvalCallback(
        eval_env,
        best_model_save_path=best_model_dir,
        log_path=best_model_dir,
        eval_freq=10000,
        n_eval_episodes=3,
        deterministic=True,
        render=False,
        verbose=1
    )

    # ── 4. Inicialización del Modelo PPO ───────────────────────
    model = PPO(
        policy="MlpPolicy",
        env=train_env,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,         # Evita el colapso temprano de exploración
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=1,             # Muestra tablas de métricas en consola
        device="auto",
    )

    # ── 5. Entrenamiento ───────────────────────────────────────
    total_timesteps = 200_000
    print(f"\n[Train V3] Iniciando entrenamiento PPO ({total_timesteps:,} steps)...")
    model.learn(
        total_timesteps=total_timesteps,
        callback=eval_callback,
    )

    # ── 6. Guardado del Modelo y Estadísticas Críticas ─────────
    model_output_path = os.getenv("PPO_OUTPUT_PATH", os.path.join(output_dir, "ppo_trading_bot_v3.zip"))
    model.save(model_output_path)
    print(f"[Train V3] Modelo final guardado en: {model_output_path}")

    # GUARDADO CRÍTICO: Estadísticas de VecNormalize
    norm_output_path = os.path.splitext(model_output_path)[0] + "_vec_norm.pkl"
    train_env.save(norm_output_path)
    print(f"[Train V3] Estadísticas de normalización guardadas en: {norm_output_path}")

    # Metadata de configuración para ai.py
    metadata = {
        'feature_names': FEATURE_NAMES,
        'n_features': len(FEATURE_NAMES),
        'window_size': train_env_config['window_size'],
        'version': '3.0',
        'total_steps': total_timesteps,
        'action_type': 'continuous',
        'action_space': ['direction', 'volume', 'sl_pips', 'tp_pips'],
        'env_config': train_env_config,
        'train_date_range': (str(train_df['time'].min()), str(train_df['time'].max())),
        'eval_date_range': (str(eval_df['time'].min()), str(eval_df['time'].max())),
        'vec_normalize_file': os.path.basename(norm_output_path),
    }

    metadata_path = os.path.splitext(model_output_path)[0] + ".pkl"
    with open(metadata_path, "wb") as f:
        pickle.dump(metadata, f)
    print(f"[Train V3] Metadata guardada en: {metadata_path}")

    # ── 7. Validación Final Fuera de Muestra ───────────────────
    # Sincronizar estadísticas definitivas con el entorno de test
    eval_env.obs_rms = train_env.obs_rms
    eval_metrics = run_out_of_sample_backtest(model, eval_env, eval_df)

    train_env.close()
    eval_env.close()
    print("[Train V3] Pipeline completado con éxito.")


if __name__ == "__main__":
    main()
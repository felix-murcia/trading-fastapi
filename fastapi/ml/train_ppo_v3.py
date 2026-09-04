"""
Entrenamiento PPO v3 — ACCIÓN CONTINUA AUTÓNOMA.
================================================
El agente aprende dirección, volumen, SL y TP sin heurísticas externas.

Uso (desde dentro del contenedor):
  docker exec trading-fastapi python3 /app/ml/train_ppo_v3.py

El modelo se guarda en:
  /app/ml/ppo_trading_bot_v3.zip   (modelo PPO v3)
  /app/ml/model_v3.pkl             (metadata)

Parámetros clave:
  - window_size: 10 (mismo que v2 para compatibilidad de obs space)
  - n_steps: 2048 por update
  - n_epochs: 10
  - batch_size: 64
  - learning_rate: 3e-4
"""

import urllib.request
import json
import pandas as pd
import numpy as np
import os
import pickle
import sys

# Añadir el path para poder importar trading_env_v2
sys.path.insert(0, '/app/ml')
from trading_env_v2 import ForexTradingEnvV2

from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.utils import get_device
import gymnasium as gym

MT5_HTTP_URL = os.getenv("MT5_HTTP_URL")  # REQUIRED - no default
TOKEN = os.getenv("INTERNAL_TOKEN")  # REQUIRED - no default

# ════════════════════════════════════════════════════════════════
# 10 FEATURES exactas compatibles con ai.py producción
# ════════════════════════════════════════════════════════════════
FEATURE_NAMES = [
    'returns', 'range', 'dist_sma20', 'rsi14',
    'macd', 'macd_signal', 'macd_hist',
    'bb_pos', 'lag_return_1', 'lag_return_2',
]

# ════════════════════════════════════════════════════════════════
# Fetch datos de MT5
# ════════════════════════════════════════════════════════════════
def fetch_mt5_candles(symbol: str = "EURUSD", timeframe: str = "H1",
                      count: int = 50000) -> pd.DataFrame:
    url = f"{MT5_HTTP_URL}/api/v1/market/candles/latest"
    params = f"symbol_name={symbol}&timeframe={timeframe}&count={count}"
    req = urllib.request.Request(f"{url}?{params}")
    req.add_header("X-Internal-Token", TOKEN)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if not data:
            raise ValueError("No candles returned")
        df = pd.DataFrame(data)
        df['time'] = pd.to_datetime(df['time'])
        df = df.sort_values('time').reset_index(drop=True)
        print(f"[Train V3] {len(df)} candles cargados {df['time'].min()} → {df['time'].max()}")
        return df
    except Exception as e:
        print(f"[Train V3] Error fetching candles: {e}")
        sys.exit(1)


# ════════════════════════════════════════════════════════════════
# Feature Engineering — EXACTO igual que ai.py predict
# ════════════════════════════════════════════════════════════════
def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Exact same features as ai.py for production compatibility."""
    df = df.copy()

    close = df['close'].values
    high = df['high'].values
    low = df['low'].values

    # Returns
    df['returns'] = np.diff(close, prepend=close[0]) / close[0]

    # Range (High - Low)
    df['range'] = (high - low) / close

    # SMA 20
    sma20 = pd.Series(close).rolling(20).mean().values
    df['dist_sma20'] = (close - sma20) / close

    # RSI 14
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(14).mean().values
    avg_loss = pd.Series(loss).rolling(14).mean().values
    rs = avg_gain / (avg_loss + 1e-10)
    df['rsi14'] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    macd = ema12 - ema26
    signal = pd.Series(macd).ewm(span=9, adjust=False).mean().values
    df['macd'] = macd / close
    df['macd_signal'] = signal / close
    df['macd_hist'] = (macd - signal) / close

    # Bollinger Bands
    sma20_bb = pd.Series(close).rolling(20).mean().values
    std20 = pd.Series(close).rolling(20).std().values
    df['bb_pos'] = (close - sma20_bb) / (2 * std20 + 1e-10)

    # Lagged returns
    df['lag_return_1'] = df['returns'].shift(1).fillna(0)
    df['lag_return_2'] = df['returns'].shift(2).fillna(0)

    # Limpiar NaNs
    df = df.dropna().reset_index(drop=True)
    return df


# ════════════════════════════════════════════════════════════════
# Custom callback para logging
# ════════════════════════════════════════════════════════════════
class MetricsCallback(BaseCallback):
    def __init__(self, verbose=0):
        super().__init__(verbose)
        self.episode_rewards = []
        self.episode_lengths = []

    def _on_step(self) -> bool:
        return True  # Logging ya viene de PPO con verbose=1


# ════════════════════════════════════════════════════════════════
# Main training loop
# ════════════════════════════════════════════════════════════════
def main():
    print("=" * 60)
    print("PPO V3 — ACCIÓN CONTINUA AUTÓNOMA")
    print("=" * 60)

    # ── 1. Cargar datos ─────────────────────────────────────────
    df = fetch_mt5_candles(symbol="EURUSD", timeframe="H1", count=50000)
    df = engineer_features(df)
    print(f"[V3 Train] {len(df)} velas con featuresEngineered.")

    # Validar features
    missing = set(FEATURE_NAMES) - set(df.columns)
    if missing:
        print(f"[V3 Train] ERROR: features faltantes: {missing}")
        sys.exit(1)

    # ── 2. Crear entornos ───────────────────────────────────────
    env_config = {
        'window_size': 10,
        'initial_balance': 10000.0,
        'commission': 0.0001,
        'max_lot': 0.5,
        'max_sl_pips': 100.0,
        'max_tp_pips': 200.0,
        'pip_size': 0.0001,
        'max_leverage': 100.0,
    }

    def make_env():
        env = ForexTradingEnvV2(df=df, **env_config)
        return env

    train_env = DummyVecEnv([make_env])

    # Normalizar observaciones (importante para redes neuronales)
    train_env = VecNormalize(train_env, norm_obs=True, norm_reward=True)

    # ── 3. Crear modelo PPO ─────────────────────────────────────
    # Continuous action space: Box(4,)
    model = PPO(
        "MlpPolicy",
        train_env,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,       # exploration bonus
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=0,             # 0 = silent, 1 = progress bar (requires tqdm/rich)
        tensorboard_log=None,  # disabled - tensorboard not installed
        device="auto",
    )

    # ── 4. Callback simple para tracking de progreso ─────────────
    class ProgressCallback(BaseCallback):
        def __init__(self, total_steps: int, print_freq: int = 10000):
            super().__init__()
            self.total_steps = total_steps
            self.print_freq = print_freq

        def _on_step(self) -> bool:
            if self.num_timesteps % self.print_freq == 0:
                pct = self.num_timesteps * 100 // self.total_steps
                print(f"[V3 Train] {self.num_timesteps:,}/{self.total_steps:,} steps ({pct}%)")
            return True

    # ── 5. Entrenar ─────────────────────────────────────────────
    print("[V3 Train] Iniciando entrenamiento...")
    print(f"[V3 Train] Total steps: 200,000")
    print(f"[V3 Train] Action space: CONTINUOUS (direction, volume, sl_pips, tp_pips)")
    print(f"[V3 Train] Observation space: {train_env.observation_space}")
    sys.stdout.flush()

    progress_cb = ProgressCallback(total_steps=200_000, print_freq=10000)
    model.learn(
        total_timesteps=200_000,
        callback=progress_cb,
    )

    # ── 6. Guardar modelo ────────────────────────────────────────
    model_path = "/app/ml/ppo_trading_bot_v3.zip"
    model.save(model_path)
    print(f"[V3 Train] Modelo guardado: {model_path}")

    # Guardar metadata
    metadata = {
        'feature_names': FEATURE_NAMES,
        'n_features': len(FEATURE_NAMES),
        'window_size': env_config['window_size'],
        'version': '3.0',
        'total_steps': 200_000,
        'action_type': 'continuous',
        'action_space': ['direction', 'volume', 'sl_pips', 'tp_pips'],
        'env_config': env_config,
        'note': 'El agente aprende volumen, SL y TP de forma totalmente autónoma',
    }

    with open("/app/ml/model_v3.pkl", "wb") as f:
        pickle.dump(metadata, f)

    print("[V3 Train] Metadata guardada: /app/ml/model_v3.pkl")
    print("[V3 Train] ENTRENAMIENTO COMPLETADO")

    # ── 7. Test rápido de inferencia ────────────────────────────
    print("\n[V3 Train] Test de inferencia con últimos 10 candles...")
    test_env = ForexTradingEnvV2(df=df, **env_config)
    obs, _ = test_env.reset()
    total_r = 0
    for i in range(10):
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, term, trunc, info = test_env.step(action)
        total_r += reward
        print(f"  Step {i}: action={action}  reward={reward:.4f}  "
              f"pos={info['position']}  vol={info['volume']:.2f}  "
              f"sl={info['sl_pips']:.1f}  tp={info['tp_pips']:.1f}  "
              f"balance={info['balance']:.2f}")
    print(f"  Total reward: {total_r:.4f}")

    train_env.close()


if __name__ == "__main__":
    main()

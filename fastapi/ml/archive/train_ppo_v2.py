"""
Entrenamiento PPO de MÁXIMA CALIDAD con las 10 features exactas de ai.py.
Las 10 features: returns, range, dist_sma20, rsi14, macd, macd_signal,
                  macd_hist, bb_pos, lag_return_1, lag_return_2

Uso (desde dentro del contenedor):
  docker exec trading-fastapi python3 /app/ml/train_ppo_v2.py

El modelo se guarda en:
  /app/ml/ppo_trading_bot.zip   (modelo PPO)
  /app/ml/model.pkl             (metadata: feature names, versión)
"""

import urllib.request
import json
import pandas as pd
import numpy as np
import os
import pickle
import sys
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.callbacks import EvalCallback
from stable_baselines3.common.callbacks import BaseCallback
import gymnasium as gym

MT5_HTTP_URL = os.getenv("MT5_HTTP_URL")  # REQUIRED - no default
TOKEN = os.getenv("INTERNAL_TOKEN")  # REQUIRED - no default

# ══════════════════════════════════════════════════════════════
# 10 FEATURES EXACTAS que ai.py usa en producción
# ══════════════════════════════════════════════════════════════
FEATURE_NAMES = [
    'returns', 'range', 'dist_sma20', 'rsi14',
    'macd', 'macd_signal', 'macd_hist',
    'bb_pos', 'lag_return_1', 'lag_return_2',
]

class ForexTradingEnv(gym.Env):
    """Entorno de trading compatible con stable-baselines3."""
    metadata = {'render_modes': ['human']}

    def __init__(self, df: pd.DataFrame, window_size: int = 10,
                 initial_balance: float = 1000.0, commission: float = 0.0001):
        super().__init__()
        self.df = df.reset_index(drop=True)
        self.window_size = window_size
        self.initial_balance = initial_balance
        self.commission = commission

        # Usar SOLO las 10 features exactas
        self.features = FEATURE_NAMES

        # Validar que todas las features existen
        missing = set(self.features) - set(self.df.columns)
        if missing:
            raise ValueError(f"Features faltantes en df: {missing}")

        # Action: 0=FLAT, 1=LONG, 2=SHORT
        self.action_space = gym.spaces.Discrete(3)
        # Observación: (window_size, n_features + 1 posicion)
        self.observation_space = gym.spaces.Box(
            low=-np.inf, high=np.inf,
            shape=(self.window_size, len(self.features) + 1),
            dtype=np.float32
        )

        self.current_step = 0
        self.position = 0   # 0=flat, 1=long, 2=short
        self.entry_price = 0.0
        self.balance = self.initial_balance
        self.equity = self.initial_balance

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.current_step = self.window_size
        self.position = 0
        self.entry_price = 0.0
        self.balance = self.initial_balance
        self.equity = self.initial_balance
        return self._get_observation(), {}

    def _get_observation(self):
        obs = self.df[self.features].iloc[
            self.current_step - self.window_size : self.current_step
        ].values
        pos_matrix = np.full((self.window_size, 1), self.position, dtype=np.float32)
        obs = np.hstack([obs, pos_matrix])
        return obs.astype(np.float32)

    def step(self, action):
        current_price = self.df['close'].iloc[self.current_step]
        reward = 0.0

        # ── LONG ──────────────────────────────────────────────
        if action == 1:
            if self.position == 2:  # close short first
                pnl = (self.entry_price - current_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
            if self.position != 1:  # open long
                self.entry_price = current_price
                self.position = 1
                reward -= self.commission

        # ── SHORT ─────────────────────────────────────────────
        elif action == 2:
            if self.position == 1:  # close long first
                pnl = (current_price - self.entry_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
            if self.position != 2:  # open short
                self.entry_price = current_price
                self.position = 2
                reward -= self.commission

        # ── FLAT (close any open position) ────────────────────
        elif action == 0:
            if self.position == 1:
                pnl = (current_price - self.entry_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
                self.position = 0
            elif self.position == 2:
                pnl = (self.entry_price - current_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
                self.position = 0

        # ── Equity tracking ────────────────────────────────────
        self.equity = self.balance
        if self.position == 1:
            self.equity = self.balance * (1 + (current_price - self.entry_price) / self.entry_price)
        elif self.position == 2:
            self.equity = self.balance * (1 + (self.entry_price - current_price) / self.entry_price)

        # Drawdown penalty (5%)
        if self.equity < self.initial_balance * 0.95:
            reward -= 0.5

        # Holding cost (swap simulation)
        if self.position != 0:
            reward -= 0.00001

        self.current_step += 1

        terminated = False
        truncated = False
        if self.current_step >= len(self.df) - 1:
            terminated = True
        if self.equity <= self.initial_balance * 0.5:
            terminated = True
            reward -= 10.0

        return self._get_observation(), reward, terminated, truncated, \
            {"equity": self.equity, "balance": self.balance}


# ══════════════════════════════════════════════════════════════
# Descarga de datos desde MT5 MCP
# ══════════════════════════════════════════════════════════════
def load_candles(symbol="EURUSD", timeframe="H1", count=50000):
    """Descarga velas desde el MCP de MT5."""
    print(f"\n⬇️  Descargando {count:,} velas {timeframe} de {symbol}...")
    url = (f"{MT5_HTTP_URL}/api/v1/market/candles/latest"
           f"?symbol_name={symbol}&timeframe={timeframe}&count={count}")

    req = urllib.request.Request(url)
    req.add_header('X-Internal-Token', TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        candles = data if isinstance(data, list) else data.get("candles", [])
        df = pd.DataFrame(candles)
        print(f"   ✓ Recibidas {len(df):,} velas")
        return df
    except Exception as e:
        print(f"   ✗ Error descargando: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════
# Feature Engineering (las 10 exactas de ai.py)
# ══════════════════════════════════════════════════════════════
def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Computa las 10 features exactas para entrenamiento y producción."""
    print("\n🔧 Computando 10 features...")

    # 1. Returns
    df['returns'] = df['close'].pct_change()

    # 2. Range (H - L)
    df['range'] = df['high'] - df['low']

    # 3. Dist SMA20
    df['sma20'] = df['close'].rolling(20).mean()
    df['dist_sma20'] = (df['close'] - df['sma20']) / df['sma20']

    # 4. RSI(14)
    delta = df['close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df['rsi14'] = 100 - (100 / (1 + rs))

    # 5-7. MACD
    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp12 - exp26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    # 8. Bollinger Bands Position
    bb_sma = df['close'].rolling(20).mean()
    bb_std = df['close'].rolling(20).std()
    df['bb_pos'] = (df['close'] - bb_sma) / (2 * bb_std.replace(0, 1e-10))

    # 9-10. Lagged Returns
    df['lag_return_1'] = df['returns'].shift(1)
    df['lag_return_2'] = df['returns'].shift(2)

    # Limpiar NaN
    before = len(df)
    df = df.dropna()
    after = len(df)
    print(f"   ✓ Features listas: {after:,} filas válidas "
          f"({before - after:,} descartadas por NaN)")

    # Validación de features
    for f in FEATURE_NAMES:
        assert f in df.columns, f"Feature '{f}' no została creada"
        assert not df[f].isna().any(), f"Feature '{f}' tiene NaN"

    print(f"   ✓ Features: {FEATURE_NAMES}")
    return df


# ══════════════════════════════════════════════════════════════
# Callback de logging durante entrenamiento
# ══════════════════════════════════════════════════════════════
class TrainingMetricsCallback(BaseCallback):
    def __init__(self, verbose=0):
        super().__init__(verbose)
        self.episode_rewards = []
        self.ep_count = 0

    def _on_step(self):
        if self.n_calls % 10000 == 0 and self.n_calls > 0:
            print(f"   → {self.n_calls:,} steps completados...")
        return True


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def main():
    print("=" * 60)
    print("  PPO TRAINING v2 — 10 FEATURES (MÁXIMA CALIDAD)")
    print("=" * 60)

    # 1. Descargar datos máximos
    df_raw = load_candles(symbol="EURUSD", timeframe="H1", count=50000)
    if df_raw.empty:
        print("ERROR: No se pudieron descargar datos. Saliendo.")
        sys.exit(1)

    # 2. Features
    df = compute_features(df_raw)

    # 3. Split temporal (80% train, 20% eval)
    split_idx = int(len(df) * 0.8)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    eval_df  = df.iloc[split_idx:].reset_index(drop=True)
    print(f"\n📊 Train: {len(train_df):,} velas | Eval: {len(eval_df):,} velas")
    print(f"   Rango train: {train_df['time'].iloc[0]} → {train_df['time'].iloc[-1]}")
    print(f"   Rango eval:  {eval_df['time'].iloc[0]} → {eval_df['time'].iloc[-1]}")

    # 4. Crear entornos
    print("\n🏗️  Creando entornos...")
    train_env = DummyVecEnv([
        lambda: ForexTradingEnv(train_df, window_size=10,
                                initial_balance=1000.0, commission=0.0001)
    ])
    eval_env = DummyVecEnv([
        lambda: ForexTradingEnv(eval_df, window_size=10,
                                initial_balance=1000.0, commission=0.0001)
    ])

    # Verificar shape de observación
    test_obs = eval_env.reset()
    if isinstance(test_obs, tuple):
        test_obs = test_obs[0]
    print(f"   ✓ Observación: {test_obs.shape} "
          f"(esperado: (1, 10, {len(FEATURE_NAMES)+1}))")

    # 5. Configuración PPO (hiperparámetros robustos)
    print("\n🧠 Inicializando PPO...")
    model = PPO(
        policy="MlpPolicy",
        env=train_env,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,           # discount factor
        gae_lambda=0.95,     # GAE
        ent_coef=0.01,        # exploration bonus
        vf_coef=0.5,          # value function weight
        max_grad_norm=0.5,
        verbose=1,
        seed=42,
    )

    # 6. Callback de evaluación periódica
    eval_callback = EvalCallback(
        eval_env,
        best_model_save_path='/app/ml/',
        log_path='/app/ml/logs/',
        eval_freq=20000,       # evaluar cada 20k steps
        deterministic=True,
        render=False,
        n_eval_episodes=5,
    )

    metrics_callback = TrainingMetricsCallback()

    # 7. ENTRENAMIENTO
    TOTAL_STEPS = 200000   # 200k steps — suficiente para aprender
    print(f"\n🚀 Entrenando {TOTAL_STEPS:,} steps...")
    print(f"   (equivalente a ~{TOTAL_STEPS//(24*21):,} horas de mercado H1)")
    print("-" * 50)

    model.learn(
        total_timesteps=TOTAL_STEPS,
        callback=[eval_callback, metrics_callback],
        progress_bar=False,
    )

    print("-" * 50)

    # 8. Guardar modelo PPO
    ppo_path = "/app/ml/ppo_trading_bot"
    model.save(ppo_path)
    print(f"\n💾 Modelo guardado: {ppo_path}.zip")

    # 9. Guardar metadata (feature names para validación en ai.py)
    model_meta = {
        "feature_names": FEATURE_NAMES,
        "n_features": len(FEATURE_NAMES),
        "window_size": 10,
        "version": "2.0",
        "total_steps": TOTAL_STEPS,
        "train_rows": len(train_df),
        "eval_rows": len(eval_df),
    }
    meta_path = "/app/ml/model.pkl"
    with open(meta_path, 'wb') as f:
        pickle.dump(model_meta, f)
    print(f"💾 Metadata guardado: {meta_path}")
    print(f"   Features: {FEATURE_NAMES}")

    # 10. Test final de predicción
    print("\n🧪 Test final de predicción...")
    test_obs = eval_env.reset()
    if isinstance(test_obs, tuple):
        test_obs = test_obs[0]
    action, _ = model.predict(test_obs, deterministic=True)
    print(f"   Acción de prueba: {action} (0=FLAT, 1=LONG, 2=SHORT)")

    print("\n✅ ENTRENAMIENTO COMPLETADO")
    print("=" * 60)


if __name__ == "__main__":
    main()

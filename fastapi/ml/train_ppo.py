import urllib.request
import json
import pandas as pd
import numpy as np
import os
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.callbacks import EvalCallback
from trading_env import ForexTradingEnv

MT5_HTTP_URL = os.getenv("MT5_HTTP_URL")  # REQUIRED - no default

def load_data(symbol="EURUSD", timeframe="H1", count=40000):
    print(f"Buscando {count} velas {timeframe} de {symbol}...")
    url = f"{MT5_HTTP_URL}/api/v1/market/candles/latest?symbol_name={symbol}&timeframe={timeframe}&count={count}"
    
    try:
        req = urllib.request.Request(url)
        token = os.getenv("INTERNAL_TOKEN")  # REQUIRED - no default
        if not token:
            raise RuntimeError("INTERNAL_TOKEN env var not set")
        req.add_header('X-Internal-Token', token)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
        candles = data if isinstance(data, list) else data.get("candles", [])
        df = pd.DataFrame(candles)
        
        # Eliminamos ruido de findes e irregularidades basicas
        if df.empty:
            raise ValueError("No data returned")
            
        print("Calculando features base (EMA, MACD, Returns)...")
        df['returns'] = df['close'].pct_change()
        # SMA dist
        df['sma20'] = df['close'].rolling(20).mean()
        df['dist_sma20'] = (df['close'] - df['sma20']) / df['sma20']
        
        # MACD
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        
        # ATR simulado (simplificado)
        df['tr'] = df['high'] - df['low']
        df['atr'] = df['tr'].rolling(14).mean()
        
        df = df.dropna()
        print(f"Features listos. Total filas validas: {len(df)}")
        return df
        
    except Exception as e:
        print(f"Error fetching data: {e}")
        return pd.DataFrame()

def train_agent():
    # 1. Cargamos EURUSD H1
    df = load_data(symbol="EURUSD", timeframe="H1", count=10000) # Probamos con 10k H1 (approx 2 years)
    if df.empty:
        return
        
    # Split train/eval
    split_idx = int(len(df) * 0.8)
    train_df = df.iloc[:split_idx]
    eval_df = df.iloc[split_idx:]
    
    # 2. Entorno Vectorizado (Gym)
    print("Creando Entornos Gymnasium (Train y Eval)...")
    train_env = DummyVecEnv([lambda: ForexTradingEnv(train_df, window_size=10, initial_balance=1000.0, commission=0.0001)])
    eval_env = DummyVecEnv([lambda: ForexTradingEnv(eval_df, window_size=10, initial_balance=1000.0, commission=0.0001)])
    
    # 3. Inicializar el Cerebro (DQN / PPO)
    print("Inicializando Agente PPO (Stable Baselines3)...")
    model = PPO("MlpPolicy", train_env, verbose=1, learning_rate=0.0003, n_steps=2048, batch_size=64, ent_coef=0.01)
    
    # Callback para validacion
    eval_callback = EvalCallback(eval_env, best_model_save_path='/app/ml/',
                                 log_path='/app/ml/logs/', eval_freq=5000,
                                 deterministic=True, render=False)
                                 
    print("¡Empieza el Entrenamiento por Refuerzo! Simulando mercados...")
    # Entrenar de forma ligera primero para confirmar arquitectura
    model.learn(total_timesteps=20000, callback=eval_callback)
    
    model.save("/app/ml/ppo_trading_bot")
    print("Entrenamiento completado y guardado en /app/ml/ppo_trading_bot.zip")

if __name__ == "__main__":
    train_agent()

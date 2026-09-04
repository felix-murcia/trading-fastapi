import urllib.request
import json
import pandas as pd
import numpy as np
import os
import time
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.callbacks import EvalCallback
from train_ppo import load_data, ForexTradingEnv

MODEL_PATH = "/app/ml/ppo_trading_bot.zip"

def retrain_agent():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Iniciando ciclo semanal de reentrenamiento autónomo...")
    if not os.path.exists(MODEL_PATH):
        print("No existe modelo base. Abortando reentrenamiento.")
        return

    # Descargamos los ultimos 1000 H1 (aprox. la volatilidad de los ultimos 2 meses)
    df = load_data(symbol="EURUSD", timeframe="H1", count=1000)
    if df.empty:
        return
        
    print("Cargando modelo PPO previo...")
    model = PPO.load(MODEL_PATH)
    
    # Creamos el nuevo entorno con los datos frescos
    print("Mapeando entorno con volatilidad actual...")
    retrain_env = DummyVecEnv([lambda: ForexTradingEnv(df, window_size=10, initial_balance=1000.0, commission=0.0001)])
    model.set_env(retrain_env)
    
    # Aplicamos dosis adaptativa (10,000 pasos es suficiente para sesgar pesos hacia memoria reciente)
    print("Ejecutando inyeccion de refuerzo (10K pasos)...")
    model.learn(total_timesteps=10000)
    
    # Sobrescribimos
    model.save(MODEL_PATH)
    print("Modelo actualizado exitosamente. Listo para operar el Lunes.")

if __name__ == "__main__":
    retrain_agent()

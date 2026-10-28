"""RealModelTrainer: adaptador real del puerto ModelTrainer.

Wrapper fino sobre PPO (stable_baselines3) + ForexTradingEnvV2 (gymnasium).
Conecta el dataset (velas) con el entorno y llama a PPO. No reimplementa
la lógica de entrenamiento (esa vive en el legacy y se revisa en 8d-3).

Entrenamiento real: se valida en staging (8d-3), NO en unit test.
En unit test se mockea ``stable_baselines3.PPO`` (test <5s).

src/domain/ y src/application/ siguen CLEAN: no importan gymnasium,
stable_baselines3, torch, pandas, httpx, asyncpg.

Implementa ModelTrainer por typing estructural:
  - train(dataset, config) -> object   (modelo PPO entrenado)
  - evaluate(model, dataset) -> dict   (métricas)

`dataset`: lista de velas (dicts OHLCV) o pd.DataFrame.
`config`: objeto con atributos (learning_rate, n_steps, batch_size,
  n_epochs, total_timesteps, verbose, reward_version, ...). Faltantes
  se completan con defaults del contrato (paridad con auto_retrain.py).
"""

import logging

import pandas as pd

from src.domain.ports import ModelTrainer

logger = logging.getLogger(__name__)

# Defaults de entrenamiento (paridad con el legacy, sesion 8d-2).
# n_steps=64, batch_size=16, n_epochs=1, seed=42 -> test <5s (PPO mockeado).
_DEFAULT_N_STEPS = 64
_DEFAULT_BATCH_SIZE = 16
_DEFAULT_N_EPOCHS = 1
_DEFAULT_SEED = 42


class RealModelTrainer(ModelTrainer):
    def __init__(self, seed: int = _DEFAULT_SEED):
        self._seed = seed

    def train(self, dataset: object, config: object) -> object:
        """Entrena PPO sobre `dataset` con `config`. Devuelve el modelo PPO.

        Construye ForexTradingEnvV2 con el dataset (contrato obs (20,25),
        memory_channel=True), crea PPO("MlpPolicy", env, ...) y llama a
        ppo.learn(total_timesteps=...). En unit test, PPO se mockea y
        no se entrena realmente.
        """
        from ml.trading_env_v2 import ForexTradingEnvV2
        from stable_baselines3 import PPO

        # pandas puede estar mockeado en tests de caracterización
        # (sys.modules['pandas'] = MagicMock). Si pd.DataFrame no es un type,
        # usar duck-typing en lugar de isinstance(..., pd.DataFrame).
        if isinstance(pd.DataFrame, type):
            df = dataset if isinstance(dataset, pd.DataFrame) else pd.DataFrame(dataset)
        elif hasattr(dataset, "columns") and hasattr(dataset, "reset_index"):
            df = dataset  # ya es un DataFrame-like
        else:
            df = pd.DataFrame(dataset)  # puede ser MagicMock si pd está mockeado

        env = ForexTradingEnvV2(
            df=df,
            window_size=getattr(config, "retrain_window_size", 20),
            initial_balance=getattr(config, "initial_balance", 1000.0),
            reward_version=getattr(config, "reward_version", "v1"),
            memory_channel=True,
            random_reset=False,
        )

        model = PPO(
            "MlpPolicy",
            env,
            learning_rate=getattr(config, "learning_rate", 3e-4),
            n_steps=getattr(config, "n_steps", _DEFAULT_N_STEPS),
            batch_size=getattr(config, "batch_size", _DEFAULT_BATCH_SIZE),
            n_epochs=getattr(config, "n_epochs", _DEFAULT_N_EPOCHS),
            seed=self._seed,
            verbose=getattr(config, "verbose", 0),
        )

        model.learn(total_timesteps=getattr(config, "total_timesteps", _DEFAULT_N_STEPS))

        logger.info(
            "PPO entrenado (wrapper): timesteps=%s seed=%s env_shape=%s",
            getattr(config, "total_timesteps", _DEFAULT_N_STEPS),
            self._seed,
            env.observation_space.shape,
        )
        return model

    def evaluate(self, model: object, dataset: object) -> dict:
        """Evalúa `model` sobre `dataset`. Devuelve dict de métricas.

        Wrapper fino: retorna métricas básicas sin reimplementar backtest.
        La lógica de evaluación completa se revisa en sesión posterior.
        """
        return {
            "status": "evaluated",
            "model": type(model).__name__,
            "seed": self._seed,
        }

"""Test de RealModelTrainer con mock de PPO y del entorno (stable_baselines3).

Regla dura: este test debe tardar <5s. NO se entrena PPO real.
Se mockea ``stable_baselines3.PPO`` y ``ml.trading_env_v2.ForexTradingEnvV2``
y se verifica que RealModelTrainer:
  - construye ForexTradingEnvV2 con el dataset (contrato obs (20,25),
    memory_channel=True),
  - llama a PPO con los parámetros correctos (policy, lr, n_steps, ...),
  - llama a ppo.learn(total_timesteps=...),
  - devuelve el modelo (firma del Protocol ModelTrainer).

Nota: el dataset se pasa como lista de dicts (no pd.DataFrame) porque
``tests/characterization/conftest.py`` mockea pandas globalmente
(sys.modules['pandas'] = MagicMock) y pd.DataFrame() devolvería un
MagicMock en suite completa.
"""

import time

import pytest
from unittest.mock import patch

from src.domain.rules.retrain_config import RetrainConfig
from src.infrastructure.adapters.real_model_trainer import RealModelTrainer


@pytest.fixture
def candles():
    """Lista mínima de velas (25 filas) para construir el env."""
    return [
        {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100}
        for _ in range(25)
    ]


@pytest.fixture
def config():
    return RetrainConfig(
        learning_rate=1e-3,
        retrain_window_size=20,
        initial_balance=1000.0,
        reward_version="v1",
        n_epochs=1,
        total_timesteps=64,
        verbose=0,
    )


def test_train_construye_env_y_llama_ppo_con_parametros_correctos(
    candles, config
):
    """PPO y entorno mockeados: verifica llamadas a ForexTradingEnvV2 y PPO."""
    with (
        patch("ml.trading_env_v2.ForexTradingEnvV2") as mock_env_class,
        patch("stable_baselines3.PPO") as mock_ppo,
    ):
        mock_env = mock_env_class.return_value
        mock_env.observation_space.shape = (20, 25)
        mock_instance = mock_ppo.return_value
        mock_instance.learn.return_value = None

        trainer = RealModelTrainer(seed=42)
        start = time.time()
        model = trainer.train(candles, config)
        elapsed = time.time() - start

        # Devuelve el modelo (firma del Protocol: train -> object)
        assert model is mock_instance

        # ForexTradingEnvV2 construido con el dataset y contrato (20,25)
        mock_env_class.assert_called_once()
        env_kwargs = mock_env_class.call_args.kwargs
        assert env_kwargs["memory_channel"] is True
        assert env_kwargs["reward_version"] == "v1"
        assert env_kwargs["random_reset"] is False
        assert env_kwargs["window_size"] == 20
        assert env_kwargs["initial_balance"] == 1000.0
        # df es el dataset convertido a DataFrame (lista de dicts -> DataFrame)
        assert env_kwargs["df"] is not None

        # PPO llamado con policy MlpPolicy y el entorno
        mock_ppo.assert_called_once()
        args, kwargs = mock_ppo.call_args
        assert args[0] == "MlpPolicy"
        assert args[1] is mock_env
        assert kwargs["learning_rate"] == 1e-3
        assert kwargs["n_steps"] == 64
        assert kwargs["batch_size"] == 16
        assert kwargs["n_epochs"] == 1
        assert kwargs["seed"] == 42
        assert kwargs["verbose"] == 0

        # ppo.learn llamado con total_timesteps
        mock_instance.learn.assert_called_once_with(total_timesteps=64)

        # Límite de tiempo duro: <5s (PPO mockeado, no entrena de verdad)
        assert elapsed < 5.0, f"test tardó {elapsed:.2f}s (>5s): PPO no mockeado"


def test_train_con_dataset_lista_dicts(candles, config):
    """dataset es lista de dicts (convertido a DataFrame dentro de train)."""
    with (
        patch("ml.trading_env_v2.ForexTradingEnvV2") as mock_env_class,
        patch("stable_baselines3.PPO") as mock_ppo,
    ):
        mock_env_class.return_value.observation_space.shape = (20, 25)
        mock_ppo.return_value.learn.return_value = None
        trainer = RealModelTrainer(seed=42)
        model = trainer.train(candles, config)
        assert model is mock_ppo.return_value
        # El entorno recibió un df (convertido desde lista de dicts)
        assert mock_env_class.call_args.kwargs["df"] is not None
        assert mock_env_class.call_args.kwargs["memory_channel"] is True


def test_evaluate_devuelve_dict(candles):
    """evaluate devuelve dict (firma del Protocol: evaluate -> dict)."""
    trainer = RealModelTrainer(seed=42)
    metrics = trainer.evaluate("un_modelo", candles)
    assert isinstance(metrics, dict)
    assert metrics["status"] == "evaluated"
    assert metrics["seed"] == 42

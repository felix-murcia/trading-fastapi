"""Fake ModelTrainer en memoria (satisface ModelTrainer por typing estructural).

Devuelve un modelo predecible: no entrena PPO real, no usa gymnasium,
sin stable_baselines3. Para tests de caracterización de RunAutoRetrain.
"""


class FakeModel:
    """Modelo predecible: predict() devuelve una acción fija."""

    def __init__(self, observation_space=None, action_space=None):
        self.observation_space = observation_space or type("OS", (), {"shape": (20, 25)})()
        self.action_space = action_space or type("AS", (), {"shape": (4,)})()

    def predict(self, observation, deterministic=True):
        """Devuelve acción fija [direction=1, volume=0.01, sl=0.0, tp=0.0]."""
        import numpy as np
        return np.array([[1.0, 0.01, 0.0, 0.0]], dtype=np.float32), None


class FakeModelTrainer:
    """ModelTrainer falso. `train()` devuelve FakeModel; `evaluate()` devuelve métricas predecibles."""

    def __init__(self, should_fail: bool = False, promoted: bool = True):
        self.should_fail = should_fail
        self.promoted = promoted
        self.train_called = False
        self.evaluate_called = False

    def train(self, dataset: object, config: object) -> object:
        self.train_called = True
        if self.should_fail:
            raise RuntimeError("fake train error")
        return FakeModel()

    def evaluate(self, model: object, dataset: object) -> dict:
        self.evaluate_called = True
        sharpe = 2.0 if self.promoted else -1.0
        return {
            "sum_reward": 5.0 if self.promoted else -1.0,
            "mean_sharpe": sharpe,
            "sharpe": sharpe,
            "mean_sortino": 2.0 if self.promoted else -1.0,
            "worst_max_dd": 0.1,
            "total_openings": 250 if self.promoted else 100,
            "total_expectancy": 1.0 if self.promoted else -1.0,
            "expectancy": 1.0 if self.promoted else -1.0,
            "reward": 5.0 if self.promoted else -1.0,
        }

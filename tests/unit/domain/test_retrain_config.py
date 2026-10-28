"""Test de paridad: RetrainConfig value object."""

from src.domain.rules.retrain_config import RetrainConfig


def test_retrain_config_defaults():
    cfg = RetrainConfig()
    assert cfg.trades_before_retrain == 10
    assert cfg.min_trades_for_retrain == 5
    assert cfg.lookback_candles == 500
    assert cfg.retrain_window_size == 20
    assert cfg.initial_balance == 1000.0
    assert cfg.commission == 0.0
    assert cfg.churn_friction_per_lot == 7.0
    assert cfg.reward_version == "v1"
    assert cfg.total_timesteps == 100_000
    assert cfg.learning_rate == 3e-4
    assert cfg.real_outcome_weight == 0.3
    assert cfg.min_validation_openings == 20
    assert cfg.walk_forward_folds == 4
    assert cfg.walk_forward_train_frac == 0.70
    assert cfg.walk_forward_holdout_frac == 0.10
    assert cfg.walk_forward_min_fold_bars == 10


def test_retrain_config_lookback_for():
    cfg = RetrainConfig()
    assert cfg.lookback_for("EURUSD") == 30000
    assert cfg.lookback_for("GBPUSD") == 30000
    assert cfg.lookback_for("USDCHF") == 30000
    assert cfg.lookback_for("UNKNOWN") == 500  # default


def test_retrain_config_frozen():
    cfg = RetrainConfig()
    try:
        cfg.trades_before_retrain = 99  # type: ignore[misc]
        assert False, "debería ser frozen"
    except Exception:
        pass

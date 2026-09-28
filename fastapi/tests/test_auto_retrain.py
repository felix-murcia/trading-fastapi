"""
Tests for services/auto_retrain.py
"""
import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock
import numpy as np
import pandas as pd

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.auto_retrain import (
    _parse_timestamp,
    _persist_trade_outcome,
    _load_persisted_outcomes,
    TradeOutcome,
    get_state,
    RetrainConfig,
)
from ml.trading_env_v2 import ForexTradingEnvV2


class TestParseTimestamp:
    """Tests for _parse_timestamp function."""

    def test_parses_unix_timestamp_float(self):
        ts = 1725450000.0
        result = _parse_timestamp(ts)
        assert isinstance(result, float)
        assert result == 1725450000.0

    def test_parses_unix_timestamp_int(self):
        ts = 1725450000
        result = _parse_timestamp(ts)
        assert result == 1725450000.0

    def test_parses_unix_timestamp_string(self):
        """Numeric strings like MQL5 sends (e.g. '1725450000') must be handled."""
        ts = "1725450000"
        result = _parse_timestamp(ts)
        assert result == 1725450000.0

    def test_parses_iso_format_with_z(self):
        ts = "2024-09-04T10:00:00Z"
        with pytest.raises(ValueError):
            _parse_timestamp(ts)

    def test_parses_iso_format_with_timezone(self):
        ts = "2024-09-04T10:00:00+00:00"
        with pytest.raises(ValueError):
            _parse_timestamp(ts)

    def test_invalid_string_raises(self):
        ts = "not-a-timestamp"
        with pytest.raises(ValueError):
            _parse_timestamp(ts)

    def test_empty_string_raises(self):
        ts = ""
        with pytest.raises(ValueError):
            _parse_timestamp(ts)


class TestRetrainConfig:
    """Tests for RetrainConfig dataclass."""

    def test_default_values(self):
        config = RetrainConfig()
        assert config.trades_before_retrain == 10
        assert config.min_trades_for_retrain == 5
        assert config.lookback_candles == 500
        assert config.retrain_window_size == 20
        assert config.initial_balance == 1000.0
        assert config.n_epochs == 3

    def test_custom_values(self):
        config = RetrainConfig(trades_before_retrain=20, n_epochs=5)
        assert config.trades_before_retrain == 20
        assert config.n_epochs == 5


class TestGetState:
    """Tests for get_state function."""

    def test_returns_retrain_state(self):
        from services.auto_retrain import get_state, RetrainState
        state = get_state()
        assert isinstance(state, RetrainState)
        assert hasattr(state, "filled_count")
        assert hasattr(state, "outcomes")

    def test_filled_count_starts_at_zero(self):
        state = get_state()
        assert state.filled_count == 0


@pytest.mark.asyncio
async def test_load_persisted_outcomes_excludes_invalid_entry_times(monkeypatch):
    class FakePool:
        async def fetch(self, query):
            return [
                {
                    "symbol": "EURUSD",
                    "entry_time": datetime(2026, 9, 7, 10, 0),
                    "exit_time": datetime(2026, 9, 7, 11, 0),
                    "pnl": -15.5,
                    "pnl_pct": -0.0155,
                    "direction": "LONG",
                    "sl_hit": True,
                    "tp_hit": False,
                    "exit_reason": "sl",
                }
            ]

    monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
    outcomes = await _load_persisted_outcomes()

    assert len(outcomes) == 1
    assert outcomes[0].pnl == -15.5
    assert outcomes[0].direction == "LONG"


def test_real_loss_produces_negative_feedback():
    candles = pd.DataFrame({
        "time": pd.date_range("2026-09-07", periods=20, freq="h"),
        "open": np.full(20, 1.1),
        "high": np.full(20, 1.101),
        "low": np.full(20, 1.099),
        "close": np.full(20, 1.1),
    })
    outcome = TradeOutcome(
        symbol="EURUSD",
        entry_time=candles.iloc[10]["time"].timestamp(),
        exit_time=candles.iloc[11]["time"].timestamp(),
        pnl=-15.5,
        pnl_pct=-0.0155,
        direction="LONG",
        sl_hit=True,
        tp_hit=False,
        exit_reason="sl",
    )
    env = ForexTradingEnvV2(
        df=candles,
        window_size=10,
        real_outcomes=[outcome],
        real_outcome_weight=0.3,
    )
    env.reset()

    feedback = env._apply_real_outcome_feedback(
        np.array([1.0, 0.2, 0.2, 0.2], dtype=np.float32)
    )

    assert feedback < 0


# ─────────────────────────────────────────────────────────────────────────────
# Lote E — motor de retrain honesto (P0-1, P1-1, P1-3)
# ─────────────────────────────────────────────────────────────────────────────


def _candles(n: int = 150) -> list:
    """Velas sintéticas OHLC para _build_training_dataframe."""
    rng = np.random.default_rng(42)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0005, n))
    out = []
    for i in range(n):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        out.append({
            "time": (pd.Timestamp("2026-09-01") + pd.Timedelta(hours=i)).isoformat(),
            "open": o,
            "high": max(o, c) + 0.0008,
            "low": min(o, c) - 0.0008,
            "close": c,
        })
    return out


def _env_cfg(df: pd.DataFrame, **over) -> dict:
    """env_cfg mínimo con los mismos kwargs que usa _do_retrain en producción."""
    cfg = dict(
        df=df,
        window_size=20,
        initial_balance=1000.0,
        lot_size=100000.0,
        max_lot=0.5,
        max_sl_pips=100.0,
        max_tp_pips=200.0,
        pip_size=0.0001,
        spread_pips=1.5,
        commission_per_lot=0.0001,
        max_episode_steps=50,
        max_holding_steps=20,
        reward_scale=100.0,
        random_reset=True,
        real_outcomes=[],
        real_outcome_weight=0.3,
    )
    cfg.update(over)
    return cfg


class _ZerosModel:
    """Política determinista: acción [0,0,0,0] → nunca abre posición."""

    def predict(self, observation, deterministic=True):
        return np.zeros(4, dtype=np.float32), None


class TestGateDecision:
    """_gate_decision: solo promueve mejora ESTRICTA (empates no promueven)."""

    @staticmethod
    def _m(openings=3, reward=10.0):
        return {"openings": openings, "closed_trades": 2, "wins": 1, "losses": 1,
                "win_rate": 0.5, "reward": reward}

    def test_strict_improvement_promotes(self):
        from services.auto_retrain import _gate_decision
        ok, reason = _gate_decision(self._m(reward=10.1), self._m(reward=10.0), 1)
        assert ok is True and reason == "promoted"

    def test_tie_does_not_promote(self):
        from services.auto_retrain import _gate_decision
        ok, reason = _gate_decision(self._m(reward=10.0), self._m(reward=10.0), 1)
        assert ok is False and reason == "no_strict_improvement"

    def test_worse_does_not_promote(self):
        from services.auto_retrain import _gate_decision
        ok, reason = _gate_decision(self._m(reward=9.9), self._m(reward=10.0), 1)
        assert ok is False and reason == "no_strict_improvement"

    def test_first_model_without_baseline_promotes(self):
        from services.auto_retrain import _gate_decision
        ok, reason = _gate_decision(self._m(), None, 1)
        assert ok is True and reason == "promoted"

    def test_insufficient_openings_rejected(self):
        from services.auto_retrain import _gate_decision
        ok, reason = _gate_decision(self._m(openings=0), None, 1)
        assert ok is False and reason == "invalid_openings_or_reward"

    def test_non_finite_reward_rejected(self):
        from services.auto_retrain import _gate_decision
        ok, reason = _gate_decision(self._m(reward=float("nan")), None, 1)
        assert ok is False and reason == "invalid_openings_or_reward"


class TestEvaluateModelDeterminism:
    """Evaluación determinista: mismo modelo + mismo segmento = mismas métricas.

    (Antes candidate y baseline se evaluaban con random_reset=True → segmentos
    distintos → comparación inválida.)
    """

    @pytest.fixture
    def df(self):
        from services.auto_retrain import _build_training_dataframe
        return _build_training_dataframe(_candles(120))

    def test_two_runs_are_identical(self, df):
        from services.auto_retrain import _evaluate_model
        cfg = _env_cfg(df)
        m1 = _evaluate_model(_ZerosModel(), df, cfg)
        m2 = _evaluate_model(_ZerosModel(), df, cfg)
        assert m1 == m2

    def test_raw_run_returns_metrics(self, df):
        from services.auto_retrain import _evaluate_model
        metrics = _evaluate_model(_ZerosModel(), df, _env_cfg(df))
        assert set(metrics) == {"openings", "closed_trades", "wins", "losses",
                                "win_rate", "reward"}
        assert metrics["openings"] == 0  # política de ceros nunca abre

    def test_normalized_run_uses_obs_rms(self, df):
        from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
        from services.auto_retrain import _evaluate_model
        norm = VecNormalize(
            DummyVecEnv([lambda: ForexTradingEnvV2(**_env_cfg(df))]),
            norm_obs=True, norm_reward=True, clip_obs=10.0,
        )
        metrics = _evaluate_model(_ZerosModel(), df, _env_cfg(df), obs_rms=norm.obs_rms)
        assert metrics["openings"] == 0
        assert np.isfinite(metrics["reward"])


class TestRetrainStateBootstrap:
    """filled_count=0 solo bootstrap si NUNCA hubo retrain (tras promoción NO recomputa)."""

    @pytest.mark.asyncio
    async def test_no_bootstrap_after_retrain(self, monkeypatch):
        from services.auto_retrain import _load_retrain_state

        class FakePool:
            async def fetch(self, query, params):
                return [
                    {"key": "retrain_state.filled_count", "value": "0"},
                    {"key": "retrain_state.last_retrain_time", "value": "1725450000"},
                ]

        monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
        spy = AsyncMock()
        monkeypatch.setattr("services.auto_retrain._load_persisted_outcomes", spy)

        state = await _load_retrain_state()

        assert state.filled_count == 0       # NO recomputado desde trade_outcomes
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bootstrap_when_never_retrained(self, monkeypatch):
        from services.auto_retrain import _load_retrain_state

        class FakePool:
            async def fetch(self, query, params):
                return [{"key": "retrain_state.filled_count", "value": "0"}]

        monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
        monkeypatch.setattr(
            "services.auto_retrain._load_persisted_outcomes",
            AsyncMock(return_value=[object(), object()]),
        )

        state = await _load_retrain_state()

        assert state.filled_count == 2


class TestRetrainAutoApproveConfig:
    """P0-1: retrain_auto_approve declarado en config y congelado en False."""

    def test_declared_and_frozen_false(self):
        from config import settings
        assert hasattr(settings, "retrain_auto_approve")
        assert settings.retrain_auto_approve is False


class TestBundleNormHelpers:
    """_load_obs_rms / _make_train_env: continuidad de normalización según manifest."""

    @staticmethod
    def _df():
        return pd.DataFrame(_candles(80))

    @classmethod
    def _certified(cls, tmp_path, vec_norm: bool) -> str:
        from services.model_registry import build_manifest, write_manifest
        df = cls._df()
        zip_path = tmp_path / "ppo_trading_bot_v3.zip"
        zip_path.write_bytes(b"fake-ppo-zip")
        vec_file = None
        if vec_norm:
            from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
            pkl = tmp_path / "ppo_trading_bot_v3_vec_norm.pkl"
            VecNormalize(
                DummyVecEnv([lambda: ForexTradingEnvV2(**_env_cfg(df))]),
                norm_obs=True, norm_reward=True, clip_obs=5.0,
            ).save(str(pkl))
            vec_file = pkl.name
        obs_shape = ForexTradingEnvV2(**_env_cfg(df)).observation_space.shape
        write_manifest(str(zip_path), build_manifest(
            str(zip_path), obs_shape=obs_shape, vec_norm_file=vec_file, source="test",
        ))
        return str(zip_path)

    def test_load_obs_rms_missing_manifest_returns_none(self, tmp_path):
        from services.auto_retrain import _load_obs_rms
        zip_path = tmp_path / "ppo_trading_bot_v3.zip"
        zip_path.write_bytes(b"x")
        assert _load_obs_rms(str(zip_path), _env_cfg(self._df())) is None

    def test_load_obs_rms_raw_bundle_returns_none(self, tmp_path):
        from services.auto_retrain import _load_obs_rms
        model = self._certified(tmp_path, vec_norm=False)
        assert _load_obs_rms(model, _env_cfg(self._df())) is None

    def test_load_obs_rms_normalized_bundle_returns_stats(self, tmp_path):
        from services.auto_retrain import _load_obs_rms
        model = self._certified(tmp_path, vec_norm=True)
        assert _load_obs_rms(model, _env_cfg(self._df())) is not None

    def test_train_env_fresh_vecnorm_for_raw_bundle(self, tmp_path):
        from stable_baselines3.common.vec_env import VecNormalize
        from services.auto_retrain import _make_train_env
        model = self._certified(tmp_path, vec_norm=False)
        env = _make_train_env(_env_cfg(self._df()), model)
        assert isinstance(env, VecNormalize)
        assert env.training is True
        assert env.norm_reward is True
        assert env.clip_obs == 10.0  # VecNormalize nuevo, no cargado del pkl

    def test_train_env_continues_with_existing_stats(self, tmp_path):
        from stable_baselines3.common.vec_env import VecNormalize
        from services.auto_retrain import _make_train_env
        model = self._certified(tmp_path, vec_norm=True)
        env = _make_train_env(_env_cfg(self._df()), model)
        assert isinstance(env, VecNormalize)
        assert env.training is True
        assert env.norm_reward is True
        assert env.clip_obs == 5.0   # proviene del pkl certificado (no fresh)


class TestDoRetrainPromotion:
    """_do_retrain end-to-end (red/BD parcheadas): bundle normalizado + reset de ciclo."""

    @pytest.mark.asyncio
    async def test_promotes_normalized_bundle_and_resets_counter(self, tmp_path, monkeypatch):
        import stable_baselines3 as sb3
        from services import auto_retrain as ar
        from services.model_registry import validate_bundle

        monkeypatch.setattr(ar, "MODEL_PATH", str(tmp_path / "ppo_trading_bot_v3.zip"))
        monkeypatch.setattr(ar, "_fetch_historical_candles",
                            AsyncMock(return_value=_candles(150)))
        monkeypatch.setattr(ar, "_load_persisted_outcomes", AsyncMock(return_value=[]))
        monkeypatch.setattr(ar, "_save_retrain_metrics", AsyncMock())
        monkeypatch.setattr("services.model_backup.backup_model",
                            AsyncMock(return_value=None))

        state = ar.RetrainState(filled_count=7, last_retrain_time=1.0)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        monkeypatch.setattr(ar, "_save_retrain_state", AsyncMock())

        # Eval parcheado: candidato válido y sin baseline (primer modelo → gate OK).
        monkeypatch.setattr(ar, "_evaluate_model", lambda *a, **k: {
            "openings": 3, "closed_trades": 2, "wins": 1, "losses": 1,
            "win_rate": 0.5, "reward": 1.5,
        })

        # learn() sin-op: 100k steps no cabe en el suite. Se valida la mecánica
        # de promoción (zip + pkl + manifest + reset), no el entrenamiento.
        monkeypatch.setattr(sb3.PPO, "learn", lambda self, *a, **k: self)

        await ar._do_retrain()

        model_path = str(tmp_path / "ppo_trading_bot_v3.zip")
        norm_path = str(tmp_path / "ppo_trading_bot_v3_vec_norm.pkl")
        assert os.path.exists(model_path)
        assert os.path.exists(norm_path)

        manifest = validate_bundle(model_path)
        assert manifest["vec_norm_file"] == "ppo_trading_bot_v3_vec_norm.pkl"
        assert manifest["source"] == "auto_retrain_normalized"

        # Contador de ciclo reiniciado: los próximos N cierres cuentan para el
        # siguiente retrain (antes: filled_count nunca se reseteaba → storm).
        assert state.filled_count == 0
        assert state.last_retrain_time > 1.0
        assert state.outcomes == []

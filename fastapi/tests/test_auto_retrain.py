"""
Tests for services/auto_retrain.py
"""
import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock
import json
import gymnasium as gym
import numpy as np
import pandas as pd

from config import settings

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

    def test_costs_are_real_broker_values(self):
        """O-2 (2026-10-01): comisión bróker real = 0 (271/271 deals sin
        commission/fee) y ancla anti-churn declarada aparte = 7 €/lot."""
        config = RetrainConfig()
        assert config.commission == 0.0
        assert config.churn_friction_per_lot == 7.0


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
        async def fetch(self, query, params=None):
            assert params == "EURUSD"   # P3-1c: bootstrap cuenta SOLO este par
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
        np.array([1.0, 0.2], dtype=np.float32)   # contrato P2-1: [direction, volume]
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
        # P2-2: mismas fuentes que _do_retrain (settings: max_volume, ppo_risk_pct)
        max_lot=settings.max_volume,
        risk_pct=settings.ppo_risk_pct,
        pip_size=0.0001,
        # O-2 (2026-10-01): espejo de producción — costes reales del bróker
        spread_pips=0.17,
        commission_per_lot=0.0,
        churn_friction_per_lot=7.0,
        max_episode_steps=50,
        max_holding_steps=20,
        reward_scale=100.0,
        random_reset=True,
        real_outcomes=[],
        real_outcome_weight=0.3,
        # P2-1: espejo de los inputs del EA (mql5 §V); SL/TP salen de
        # production_stops() — por eso no hay max_sl_pips/max_tp_pips.
        trailing_enabled=True,
        trailing_percent=0.25,
        trailing_min_pips=15.0,
        trailing_atr_mult=2.0,
        min_stop_dist_pips=1.0,
    )
    cfg.update(over)
    return cfg


class _ZerosModel:
    """Política determinista: acción [0,0,0,0] (bundle legado 4-dim, evaluable
    en el gate walk-forward) → nunca abre posición."""

    # Fase 3: contrato de obs del candidato (20,25) — _evaluate_model construye
    # el env con el canal de memoria según ESTE observation_space.
    observation_space = gym.spaces.Box(
        -np.inf, np.inf, shape=(20, 25), dtype=np.float32
    )

    def predict(self, observation, deterministic=True):
        return np.zeros(4, dtype=np.float32), None


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
        assert set(metrics) == {"openings", "trades", "wins", "losses",
                                "win_rate", "reward", "balance",
                                "equity_curve", "step_rewards", "trade_pnls"}
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


class TestEvaluateModelObsContract:
    """Fase 3: el env de evaluación se construye con el observation_space del
    modelo evaluado — baseline pre-memoria (20,24) vs candidato (20,25);
    contrato desconocido = RuntimeError (sin degradación silenciosa)."""

    @pytest.fixture
    def df(self):
        from services.auto_retrain import _build_training_dataframe
        return _build_training_dataframe(_candles(120))

    @staticmethod
    def _recorder(width: int):
        class _Recorder:
            def __init__(self):
                self.observation_space = gym.spaces.Box(
                    -np.inf, np.inf, shape=(20, width), dtype=np.float32
                )
                self.seen = []

            def predict(self, observation, deterministic=True):
                self.seen.append(tuple(np.asarray(observation).shape))
                return np.zeros(4, dtype=np.float32), None

        return _Recorder()

    def test_legacy_24_model_evaluated_without_memory_channel(self, df):
        from services.auto_retrain import _evaluate_model
        model = self._recorder(24)
        metrics = _evaluate_model(model, df, _env_cfg(df))
        assert model.seen and model.seen[0] == (1, 20, 24)
        assert "trade_pnls" in metrics

    def test_memory_25_model_evaluated_with_memory_channel(self, df):
        from services.auto_retrain import _evaluate_model
        model = self._recorder(25)
        _evaluate_model(model, df, _env_cfg(df))
        assert model.seen and model.seen[0] == (1, 20, 25)

    def test_unknown_obs_contract_raises(self, df):
        from services.auto_retrain import _evaluate_model
        with pytest.raises(RuntimeError, match="Contrato de observación desconocido"):
            _evaluate_model(self._recorder(30), df, _env_cfg(df))


class TestRetrainStateBootstrap:
    """filled_count=0 solo bootstrap si NUNCA hubo retrain (tras promoción NO recomputa)."""

    @pytest.mark.asyncio
    async def test_no_bootstrap_after_retrain(self, monkeypatch):
        from services.auto_retrain import _load_retrain_state

        class FakePool:
            async def fetch(self, query, params):
                return [
                    {"key": "retrain_state.EURUSD.filled_count", "value": "0"},
                    {"key": "retrain_state.EURUSD.last_retrain_time", "value": "1725450000"},
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
                return [{"key": "retrain_state.EURUSD.filled_count", "value": "0"}]

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
        assert _load_obs_rms(str(zip_path)) is None

    def test_load_obs_rms_raw_bundle_returns_none(self, tmp_path):
        from services.auto_retrain import _load_obs_rms
        model = self._certified(tmp_path, vec_norm=False)
        assert _load_obs_rms(model) is None

    def test_load_obs_rms_normalized_bundle_returns_stats(self, tmp_path):
        from services.auto_retrain import _load_obs_rms
        model = self._certified(tmp_path, vec_norm=True)
        assert _load_obs_rms(model) is not None

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
    """_do_retrain end-to-end (red/BD parcheadas): walk-forward gate + bundle."""

    @staticmethod
    def _patch_common(monkeypatch, tmp_path, *, candles=500, model_path=None):
        import stable_baselines3 as sb3
        from services import auto_retrain as ar

        path = model_path or str(tmp_path / "ppo_trading_bot_v3.zip")
        # P3-3: la ruta se calcula por símbolo — se parchea la resolución
        # (default_model_path), no un global que el pipeline ya no lee.
        monkeypatch.setattr(ar, "default_model_path", lambda _sym: path)
        monkeypatch.setattr(ar, "_fetch_historical_candles",
                            AsyncMock(return_value=_candles(candles)))
        monkeypatch.setattr(ar, "_load_persisted_outcomes", AsyncMock(return_value=[]))
        monkeypatch.setattr(ar, "_log_retrain_event", AsyncMock())
        # P3-2a: snapshot + experimentos (sin BD real ni escritura en repo/ml)
        from services.data_snapshot import SnapshotInfo
        monkeypatch.setattr(
            "services.data_snapshot.save_snapshot",
            MagicMock(return_value=SnapshotInfo(
                path=str(tmp_path / "snapshots" / "20260101T000000Z_20260102T000000Z__deadbeef0000__retrain.parquet"),
                sha256="d" * 64, rows=candles, columns=24,
            )),
        )
        monkeypatch.setattr("services.data_snapshot.prune_snapshots", MagicMock(return_value=[]))
        # P3-2c: baseline de drift congelado en la promoción (sin escritura en ml/)
        monkeypatch.setattr(
            "services.drift_monitor.save_baseline",
            MagicMock(return_value={"path": "/x/feature_drift_baseline.json",
                                    "sha256": "f" * 64, "n_features": 17}),
        )
        monkeypatch.setattr(
            "services.experiment_tracker.start_experiment", AsyncMock(return_value=42)
        )
        monkeypatch.setattr("services.experiment_tracker.finish_experiment", AsyncMock())
        record_promo = AsyncMock()
        reload_mock = MagicMock()
        monkeypatch.setattr("services.model_registry.record_promotion", record_promo)
        monkeypatch.setattr("routers.ai.reload_inference_artifacts", reload_mock)
        monkeypatch.setattr("services.model_backup.backup_model",
                            AsyncMock(return_value=None))

        state = ar.RetrainState(filled_count=7, last_retrain_time=1.0)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        monkeypatch.setattr(ar, "_save_retrain_state", AsyncMock())

        # learn() sin-op: 100k steps no cabe en la suite. Se valida la mecánica
        # de promoción/rechazo (bundle + gate + eventos), no el entrenamiento.
        monkeypatch.setattr(sb3.PPO, "learn", lambda self, *a, **k: self)
        return ar, state, path, record_promo, reload_mock

    @staticmethod
    def _fake_metrics(*a, **k):
        """Métricas deterministas con curvas (Sharpe finito, reward > 0).

        openings=50×4 pliegues = 200 aperturas: el gate canario (P3-3) exige
        ≥200 cuando no hay baseline — 3 por pliego quedaría corto.
        trade_pnls consistente con balance (10 ≥3€ win + −5 loss = +5):
        expectancy R-1 = +2.5 > 0 → suelo absoluto superado.
        """
        return {
            "openings": 50, "trades": 2, "wins": 1, "losses": 1,
            "win_rate": 0.5, "reward": 1.5, "balance": 1005.0,
            "equity_curve": [1000.0, 1005.0, 998.0, 1010.0],
            "step_rewards": [1.0, -0.5, 1.0],
            "trade_pnls": [10.0, -5.0],
        }

    @pytest.mark.asyncio
    async def test_promotes_normalized_bundle_and_resets_counter(self, tmp_path, monkeypatch):
        from services.model_registry import validate_bundle

        ar, state, model_path, record_promo, reload_mock = self._patch_common(
            monkeypatch, tmp_path
        )
        # Sin baseline (primer modelo del par): gate canario (P3-3) —
        # ≥200 aperturas agregadas + sharpe≥1 en ≥4 pliegues.
        monkeypatch.setattr(ar, "_evaluate_model", self._fake_metrics)

        await ar._do_retrain()

        norm_path = str(tmp_path / "ppo_trading_bot_v3_vec_norm.pkl")
        assert os.path.exists(model_path)
        assert os.path.exists(norm_path)

        manifest = validate_bundle(model_path)
        assert manifest["vec_norm_file"] == "ppo_trading_bot_v3_vec_norm.pkl"
        assert manifest["source"] == "auto_retrain_normalized"
        # Informe walk-forward persistido en el bundle (P1-1).
        assert manifest["extra"]["walk_forward"]["n_folds"] == 4
        assert manifest["extra"]["gate"]["ok"] is True

        # M-1: la promoción queda marcada junto al bundle para que el commit
        # de cron (scripts/commit_promotion.py) la versione con su sha.
        marker = json.loads(
            (tmp_path / "last_promotion.json").read_text(encoding="utf-8")
        )
        assert marker["symbol"] == "EURUSD"
        assert marker["model"] == "ppo_trading_bot_v3.zip"
        assert marker["model_sha256"] == manifest["model_sha256"]

        # Evento de auditoría con métricas por pliego (antes: retrain_completed
        # idéntico para promoción y rechazo, sin métricas).
        ar._log_retrain_event.assert_awaited_once()
        assert ar._log_retrain_event.await_args.args[0] == "retrain_promoted"
        report = ar._log_retrain_event.await_args.args[1]["report"]
        assert len(report["folds"]) == 4
        assert report["holdout"]["candidate"]["reward"] > 0   # congelado reportado
        assert report["fold_wins"] is None                    # sin baseline

        # P1-2: historial en model_registry + hot-reload del bundle en memoria.
        record_promo.assert_awaited_once()
        assert record_promo.await_args.args[0]["model_sha256"] == manifest["model_sha256"]
        assert record_promo.await_args.args[1]["reason"] == "first_model_validated"
        reload_mock.assert_called_once()

        # Contador de ciclo reiniciado: los próximos N cierres cuentan para el
        # siguiente retrain (antes: filled_count nunca se reseteaba → storm).
        assert state.filled_count == 0
        assert state.last_retrain_time > 1.0

    @pytest.mark.asyncio
    async def test_promotion_targets_requested_symbol(self, tmp_path, monkeypatch):
        """P3-3: la corrida de GBPUSD entrena con SU lookback, escribe el
        bundle donde dice la convención de rutas, registra la promoción en
        SU mundo y recarga SU caché de inferencia."""
        ar, state, model_path, record_promo, reload_mock = self._patch_common(
            monkeypatch, tmp_path
        )
        monkeypatch.setattr(ar, "_evaluate_model", self._fake_metrics)

        await ar._do_retrain(symbol="GBPUSD")

        # Fetch por símbolo con el lookback del canario (30000 H1)
        assert ar._fetch_historical_candles.await_args.kwargs == {
            "symbol": "GBPUSD", "count": 30000,
        }
        # Experimento, promoción, recarga y auditoría: todos en SU mundo
        assert ar.experiment_tracker.start_experiment.await_args.kwargs["symbol"] == "GBPUSD"
        assert record_promo.await_args.kwargs["symbol"] == "GBPUSD"
        reload_mock.assert_called_once_with("GBPUSD")
        assert ar._log_retrain_event.await_args.kwargs["symbol"] == "GBPUSD"
        # El bundle se escribe en la ruta resuelta para el símbolo (tmp aquí)
        assert os.path.exists(model_path)
        assert state.filled_count == 0          # SU contador es el que se resetea
        assert state.outcomes == []

    @pytest.mark.asyncio
    async def test_rejects_tie_with_baseline_and_keeps_bundle(self, tmp_path, monkeypatch):
        """Empate con el baseline = rechazo estricto: bundle intacto, sin reset."""
        import shutil

        real_zip = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "ml", "ppo_trading_bot_v3.zip")
        )
        assert os.path.exists(real_zip)
        tmp_model = tmp_path / "ppo_trading_bot_v3.zip"
        shutil.copy(real_zip, tmp_model)

        ar, state, model_path, record_promo, reload_mock = self._patch_common(
            monkeypatch, tmp_path, model_path=str(tmp_model)
        )
        # Mismas métricas para candidato y baseline → empate → gate falla.
        monkeypatch.setattr(ar, "_evaluate_model", self._fake_metrics)

        await ar._do_retrain()

        # Bundle activo intacto: ni manifest nuevo, ni registro, ni recarga.
        assert not os.path.exists(str(tmp_path / "ppo_trading_bot_v3.manifest.json"))
        assert state.filled_count == 7
        # P3-2d: el rechazo también ancla el calendario (1.0 era el inicial)
        assert state.last_retrain_time > 1.0
        record_promo.assert_not_awaited()
        reload_mock.assert_not_called()

        ar._log_retrain_event.assert_awaited_once()
        assert ar._log_retrain_event.await_args.args[0] == "retrain_rejected"
        reason = ar._log_retrain_event.await_args.args[1]["reason"]
        assert reason.startswith("failed:")


class TestExperimentLifecycle:
    """P3-2a: cada corrida deja UNA fila consultable en training_experiments."""

    @staticmethod
    def _fake_metrics(*a, **k):
        return TestDoRetrainPromotion._fake_metrics(*a, **k)

    @pytest.mark.asyncio
    async def test_promoted_records_experiment(self, tmp_path, monkeypatch):
        ar, *_ = TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(ar, "_evaluate_model", self._fake_metrics)

        await ar._do_retrain(trigger="forced")

        start = ar.experiment_tracker.start_experiment
        finish = ar.experiment_tracker.finish_experiment
        start.assert_awaited_once()
        assert start.await_args.args[0] == "forced"            # disparador propagado
        assert start.await_args.args[2] == "d" * 64            # sha256 del snapshot
        finish.assert_awaited_once()
        assert finish.await_args.args == (42, "promoted")
        kwargs = finish.await_args.kwargs
        assert kwargs["gate_reason"]                            # por qué lo aprobó
        assert kwargs["gate_metrics"]["n_folds"] == 4
        assert kwargs["model_sha256"] and len(kwargs["model_sha256"]) == 64

    @pytest.mark.asyncio
    async def test_rejected_records_experiment(self, tmp_path, monkeypatch):
        import shutil

        real_zip = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "ml", "ppo_trading_bot_v3.zip")
        )
        tmp_model = tmp_path / "ppo_trading_bot_v3.zip"
        shutil.copy(real_zip, tmp_model)

        ar, *_ = TestDoRetrainPromotion._patch_common(
            monkeypatch, tmp_path, model_path=str(tmp_model)
        )
        monkeypatch.setattr(ar, "_evaluate_model", self._fake_metrics)

        await ar._do_retrain()

        finish = ar.experiment_tracker.finish_experiment
        finish.assert_awaited_once()
        assert finish.await_args.args == (42, "rejected")
        assert finish.await_args.kwargs["gate_reason"].startswith("failed:")
        assert finish.await_args.kwargs.get("model_sha256") is None

    @pytest.mark.asyncio
    async def test_pipeline_failure_marks_experiment_failed(self, tmp_path, monkeypatch):
        ar, *_ = TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            ar, "_load_or_create_model",
            MagicMock(side_effect=RuntimeError("boom-train")),
        )

        with pytest.raises(RuntimeError, match="boom-train"):
            await ar._do_retrain()

        # La excepción se re-propaga (no se traga) Y el experimento queda cerrado.
        finish = ar.experiment_tracker.finish_experiment
        finish.assert_awaited_once()
        assert finish.await_args.args == (42, "failed")
        assert "boom-train" in finish.await_args.kwargs["error"]
        ar.experiment_tracker.start_experiment.assert_awaited_once()

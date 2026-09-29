"""
P3-2c: services/drift_monitor.py — PSI de features + decay de métricas + alertas.
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from services import drift_monitor as dm


def _candles(n: int = 300, price: float = 1.10, scale: float = 0.0005,
             seed: int = 42) -> list:
    """Velas sintéticas OHLC (mismo formato que MT5 MCP / _candles de auto_retrain)."""
    rng = np.random.default_rng(seed)
    close = price + np.cumsum(rng.normal(0, scale, n))
    out = []
    for i in range(n):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        out.append({
            "time": (pd.Timestamp("2026-09-01") + pd.Timedelta(hours=i)).isoformat(),
            "open": o,
            "high": max(o, c) + scale * 2,
            "low": min(o, c) - scale * 2,
            "close": c,
        })
    return out


def _engineered(n=300, price=1.10, scale=0.0005, seed=42) -> pd.DataFrame:
    from services.auto_retrain import _build_training_dataframe
    return _build_training_dataframe(_candles(n, price=price, scale=scale, seed=seed))


class TestBaseline:
    def test_build_covers_market_features_with_deciles(self):
        df = _engineered()
        baseline = dm.build_feature_baseline(df)

        from ml.trading_env_v2 import MARKET_FEATURES
        feats = baseline["features"]
        assert len(feats) >= 10                       # spread/volume pueden excluirse (std=0)
        for col, stats in feats.items():
            assert col in MARKET_FEATURES
            assert len(stats["edges"]) == 9           # deciles → 10 bins
            assert all(b > a for a, b in zip(stats["edges"], stats["edges"][1:]))
            assert len(stats["expected"]) == 10
            assert sum(stats["expected"]) == pytest.approx(1.0)

    def test_degenerate_feature_excluded(self):
        df = _engineered()
        df["atr"] = 0.0                                # std=0 → sin distribución
        baseline = dm.build_feature_baseline(df)
        assert "atr" not in baseline["features"]

    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        info = dm.save_baseline(_engineered())

        assert info["n_features"] > 0
        assert len(info["sha256"]) == 64
        loaded = dm.load_baseline()
        assert loaded is not None
        assert set(loaded["features"]) == set(dm.build_feature_baseline(_engineered())["features"])

    def test_save_raises_when_no_usable_features(self, tmp_path, monkeypatch):
        from ml.trading_env_v2 import MARKET_FEATURES

        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        df = pd.DataFrame({c: np.zeros(200) for c in MARKET_FEATURES})
        with pytest.raises(RuntimeError, match="ninguna feature"):
            dm.save_baseline(df)
        assert dm.load_baseline() is None


class TestPSI:
    @pytest.fixture
    def stats(self):
        df = _engineered()
        return dm.build_feature_baseline(df)["features"]["sma20"]

    def test_identical_distribution_is_zero(self, stats):
        live = _engineered()["sma20"].to_numpy()
        assert dm.compute_psi(live, stats) == pytest.approx(0.0, abs=1e-9)
        assert dm.classify_psi(dm.compute_psi(live, stats)) == "ok"

    def test_shifted_distribution_is_critical(self, stats):
        live = _engineered(price=1000.0, scale=0.5)["sma20"].to_numpy()
        psi = dm.compute_psi(live, stats)
        assert psi > dm.PSI_CRIT
        assert dm.classify_psi(psi) == "critical"

    def test_insufficient_mass_returns_nan(self, stats):
        assert np.isnan(dm.compute_psi(np.arange(10.0), stats))
        assert dm.classify_psi(float("nan")) == "insufficient"

    def test_classify_thresholds(self):
        assert dm.classify_psi(0.05) == "ok"
        assert dm.classify_psi(0.10) == "warning"
        assert dm.classify_psi(0.15) == "warning"
        assert dm.classify_psi(0.20) == "critical"
        assert dm.classify_psi(0.90) == "critical"


class TestDecay:
    def test_insufficient_samples_no_alert(self):
        out = dm.evaluate_decay([-1.0] * 10)
        assert out["status"] == "insufficient"

    def test_positive_expectancy_ok(self):
        out = dm.evaluate_decay([5.0, 7.0, -3.0, 6.0] * 7 + [4.0])
        assert out["status"] == "ok"
        assert out["n"] == 29
        assert out["winrate"] > 0.5

    def test_mildly_negative_warning(self):
        pnls = [-51.0, 49.0] * 15                    # media=-1, std=50 → z≈-0.11
        out = dm.evaluate_decay(pnls)
        assert out["status"] == "warning"
        assert out["z"] > dm.DECAY_Z_CRIT            # negativa pero no significativa

    def test_significantly_negative_critical(self):
        pnls = [-30.0, 10.0] * 15                    # media=-10, std=20 → z≈-2.74
        out = dm.evaluate_decay(pnls)
        assert out["status"] == "critical"
        assert out["z"] <= dm.DECAY_Z_CRIT

    def test_zero_variance_all_losses_critical(self):
        out = dm.evaluate_decay([-5.0] * 25)
        assert out["status"] == "critical"

    def test_zero_variance_all_wins_ok(self):
        out = dm.evaluate_decay([5.0] * 25)
        assert out["status"] == "ok"


def _patch_env(monkeypatch, tmp_path, **kw):
    monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(dm, "send_alert", MagicMock())
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    monkeypatch.setattr("db.connection.get_pool", lambda: pool)
    return pool


class TestRunDriftCheck:
    @pytest.mark.asyncio
    async def test_no_baseline_warns_once(self, tmp_path, monkeypatch):
        pool = _patch_env(monkeypatch, tmp_path)
        monkeypatch.setattr("services.drift_monitor._recent_pnl",
                            AsyncMock(return_value=[1.0] * 5))

        report = await dm.run_drift_check()

        assert report["status"] == "no_baseline"
        assert report["level"] == "warning"
        dm.send_alert.assert_called_once()              # única alerta: sin baseline
        pool.execute.assert_awaited_once()              # evidencia en audit_log

    @pytest.mark.asyncio
    async def test_healthy_window_no_alert(self, tmp_path, monkeypatch):
        pool = _patch_env(monkeypatch, tmp_path)
        dm.save_baseline(_engineered())
        monkeypatch.setattr(
            "services.auto_retrain._fetch_historical_candles",
            AsyncMock(return_value=_candles(300, price=1.10, scale=0.0005, seed=42)),
        )
        monkeypatch.setattr("services.drift_monitor._recent_pnl",
                            AsyncMock(return_value=[5.0, -2.0, 4.0] * 10))

        report = await dm.run_drift_check()

        assert report["level"] == "ok"
        assert report["max_psi"] == pytest.approx(0.0, abs=1e-9)
        assert report["decay"]["status"] == "ok"
        dm.send_alert.assert_not_called()
        # Evidencia: evento drift_report en audit_log (sql, cycle_id, event, data)
        args = pool.execute.await_args.args
        assert args[2] == "drift_report"
        assert json.loads(args[3])["level"] == "ok"

    @pytest.mark.asyncio
    async def test_shifted_market_alerts_critical(self, tmp_path, monkeypatch):
        pool = _patch_env(monkeypatch, tmp_path)
        dm.save_baseline(_engineered())               # mercado ~1.10
        monkeypatch.setattr(
            "services.auto_retrain._fetch_historical_candles",
            # mercado vivo ~1000 con volatilidad ×1000 → sma20/atr desplazados
            AsyncMock(return_value=_candles(200, price=1000.0, scale=0.5)),
        )
        monkeypatch.setattr("services.drift_monitor._recent_pnl",
                            AsyncMock(return_value=[5.0] * 25))

        report = await dm.run_drift_check()

        assert report["level"] == "critical"
        assert report["max_psi"] > dm.PSI_CRIT
        assert report["max_psi_feature"] in report["features"]
        dm.send_alert.assert_called_once()
        assert dm.send_alert.call_args.args[0].value == "ERROR"
        assert pool.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_decay_critical_drives_level(self, tmp_path, monkeypatch):
        _patch_env(monkeypatch, tmp_path)
        dm.save_baseline(_engineered())
        monkeypatch.setattr(
            "services.auto_retrain._fetch_historical_candles",
            AsyncMock(return_value=_candles(300, price=1.10, scale=0.0005, seed=42)),
        )
        monkeypatch.setattr(
            "services.drift_monitor._recent_pnl",
            # media=-10, std=20 → z≈-2.74 → critical determinista
            AsyncMock(return_value=[-30.0, 10.0] * 15),
        )

        report = await dm.run_drift_check()

        assert report["decay"]["status"] == "critical"
        assert report["level"] == "critical"
        dm.send_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_report_and_alerts(self, tmp_path, monkeypatch):
        _patch_env(monkeypatch, tmp_path)
        dm.save_baseline(_engineered())
        monkeypatch.setattr(
            "services.auto_retrain._fetch_historical_candles",
            AsyncMock(side_effect=RuntimeError("MT5 down")),
        )
        monkeypatch.setattr("services.drift_monitor._recent_pnl",
                            AsyncMock(return_value=[5.0] * 25))

        report = await dm.run_drift_check()            # no lanza: informa

        assert report["level"] == "warning"
        assert "MT5 down" in report["features_error"]
        # alertas: una por features_error; sin otra (decay ok, sin ofensores)
        dm.send_alert.assert_called_once()


class TestEndpoint:
    def test_route_exists(self):
        from main import app
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/v1/ai/model/drift" in paths

    def test_requires_token(self):
        from contextlib import ExitStack
        from unittest.mock import patch as _patch
        from fastapi.testclient import TestClient
        from main import app

        stack = ExitStack()
        stack.enter_context(_patch("main.init_pool", AsyncMock()))
        stack.enter_context(_patch("main.close_pool", AsyncMock()))
        with stack, TestClient(app) as client:
            missing = client.get("/api/v1/ai/model/drift")
            wrong = client.get(
                "/api/v1/ai/model/drift", headers={"x-internal-token": "bad"}
            )
        assert missing.status_code == 422
        assert wrong.status_code == 401

    def test_wires_run_drift_check(self):
        from contextlib import ExitStack
        from unittest.mock import patch as _patch
        from fastapi.testclient import TestClient
        from main import app

        payload = {"level": "ok", "max_psi": 0.0}
        stack = ExitStack()
        stack.enter_context(_patch("main.init_pool", AsyncMock()))
        stack.enter_context(_patch("main.close_pool", AsyncMock()))
        stack.enter_context(_patch(
            "services.drift_monitor.run_drift_check", AsyncMock(return_value=payload)
        ))
        with stack, TestClient(app) as client:
            res = client.get(
                "/api/v1/ai/model/drift", headers={"x-internal-token": "test-token"}
            )
        assert res.status_code == 200
        assert res.json() == payload


def test_loop_wired_in_lifespan():
    with open(os.path.join(os.path.dirname(dm.__file__), "..", "main.py")) as f:
        src = f.read()
    assert "_bg_tasks.add(asyncio.create_task(_drift_monitor_loop()))" in src


def test_baseline_is_per_symbol(tmp_path, monkeypatch):
    """P3-3: baseline por par — la promoción de GBPUSD jamás pisa el de EURUSD."""
    monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
    df = _engineered()

    eu = dm.save_baseline(df, symbol="EURUSD")
    gb = dm.save_baseline(df, symbol="GBPUSD")

    assert eu["path"] != gb["path"]
    assert os.path.dirname(eu["path"]) == str(tmp_path)             # legado en raíz
    assert os.path.dirname(gb["path"]) == str(tmp_path / "GBPUSD")  # namespace propio
    assert dm.load_baseline("EURUSD") is not None
    assert dm.load_baseline("GBPUSD") is not None

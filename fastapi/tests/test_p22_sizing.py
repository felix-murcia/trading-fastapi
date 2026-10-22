"""
P2-2: sizing del PPO delegado — paridad train/serve del sizing (auditoría).

- UNA función: `core.rules.size_ppo_position` la usan `/api/v1/ai/predict`
  y el env de entrenamiento (identidad de objeto, igual que `production_stops`
  en P2-1). Si alguien la copia a otro módulo, estos tests fallan.
- La heurística "mín. 3€" (0.1 €/pip/lot, error ×100) y el tope degenerado
  `min(0.5, 3%·equity/dist)` — siempre activo, cabeza `volume` muerta —
  desaparecen: el tamaño es riesgo fraccional modulado por la política.
- Comportamiento del env derivado del sizing: volumen 0 → sin entrada
  (paridad con el HOLD por sizing inviable de /predict) y el tamaño escala
  con el balance (riesgo fijo por equity, no lotes fijos).
"""
import numpy as np
import pandas as pd
import pytest

from config import settings
from core import rules
from ml import trading_env_v2
from ml.trading_env_v2 import ForexTradingEnvV2, engineer_market_features
from services import position_sizing as ps

INITIAL = 1000.0
OPEN = np.array([0.5, 0.4], dtype=np.float32)   # long, vol_norm=0.4
FLAT = np.array([0.0, 0.0], dtype=np.float32)


@pytest.fixture(autouse=True)
def pinned_sizing_settings(monkeypatch):
    """Fija los topes de sizing a los defaults canónicos (host = contenedor)."""
    for attr, value in {
        "ppo_risk_pct": 0.03,
        "min_volume": 0.01,
        "max_volume": 0.50,
    }.items():
        monkeypatch.setattr(settings, attr, value)


def _raw_df(n=40):
    """OHLC determinista: close 1.1000, rango 0.0011 → ATR 10 pips → SL 15/TP 30."""
    closes = np.full(n, 1.1000)
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="h"),
        "open": closes,
        "high": closes + 0.00055,
        "low": closes - 0.00055,
        "close": closes,
        "tick_volume": np.full(n, 1000),
    })


def _env(**overrides):
    kwargs = dict(
        df=engineer_market_features(_raw_df()),
        window_size=5,
        initial_balance=INITIAL,
        lot_size=100000.0,
        max_lot=0.5,
        pip_size=0.0001,
        spread_pips=1.5,
        commission_per_lot=7.0,
        max_episode_steps=50,
        random_reset=False,
        trailing_enabled=True,
    )
    kwargs.update(overrides)
    return ForexTradingEnvV2(**kwargs)


class TestSharedSizingFunction:
    """Paridad: mismo objeto de función y misma cadena de parámetros en serve y env."""

    def test_predict_and_env_share_the_same_function_object(self):
        from services import trade_setup
        assert trade_setup.size_ppo_position is rules.size_ppo_position
        assert trading_env_v2.size_ppo_position is rules.size_ppo_position

    def test_env_defaults_match_settings_serve_contract(self):
        # serve usa settings.ppo_risk_pct / max_volume; el env debe construir
        # con los mismos topes o el MDP entrenado no es el ejecutado.
        env = _env()
        assert env.risk_pct == settings.ppo_risk_pct
        assert env.max_lot == settings.max_volume

    def test_env_open_volume_equals_serve_sizing_formula(self):
        # Cadena completa: lo que /predict calcularía para este mismo estado
        # (equity 1000, SL 15 pips de production_stops, ppv EURUSD 10) es
        # exactamente lo que abre el env.
        env = _env()
        env.reset()
        env.step(OPEN)

        serve_lots = rules.size_ppo_position(
            volume_norm=0.4,
            sl_pips=17.0,
            equity=INITIAL,
            pip_value=ps.pip_value_per_lot("EURUSD", 1.1000),
            risk_pct=settings.ppo_risk_pct,
            max_lot=settings.max_volume,
            min_lot=settings.min_volume,
        )
        assert env.volume == pytest.approx(serve_lots, abs=1e-9)
        assert serve_lots == pytest.approx(0.07)   # 1000 × 3% × 0.4 / (17 × 10)


class TestEnvVolumeBehavior:
    """El env obedece el sizing delegado (comportamiento nuevo del MDP)."""

    def test_zero_volume_norm_skips_entry(self):
        # Paridad con /predict: sizing 0 → sin posición (no un lote mínimo
        # forzado como antes — max(0.01, …) — ni orden de volumen 0).
        env = _env()
        env.reset()
        balance_before = env.balance

        _, _, terminated, truncated, info = env.step(np.array([0.5, 0.0], dtype=np.float32))

        assert env.position == 0
        assert env.volume == 0.0
        assert env.balance == pytest.approx(balance_before)   # ni comisión
        assert info["trades"] == 0
        assert terminated is False and truncated is False

    def test_volume_scales_with_balance(self):
        # Riesgo fijo por equity: 500 de balance → la mitad de lotes.
        env_small = _env(initial_balance=500.0)
        env_small.reset()
        env_small.step(OPEN)
        # 500 × 3% × 0.4 / (17 × 10) = 0.03
        assert env_small.volume == pytest.approx(0.03, abs=1e-9)

        env_big = _env(initial_balance=2000.0)
        env_big.reset()
        env_big.step(OPEN)
        # 2000 × 3% × 0.4 / (17 × 10) = 0.14
        assert env_big.volume == pytest.approx(0.14, abs=1e-9)

    def test_tiny_equity_never_opens_beyond_risk(self):
        # 5 de balance → 0.001 lots < lote mínimo → sin entrada.
        env = _env(initial_balance=5.0)
        env.reset()
        env.step(OPEN)
        assert env.position == 0
        assert env.volume == 0.0

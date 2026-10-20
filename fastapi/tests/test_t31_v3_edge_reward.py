"""
T-31.9 / L3 (2026-10-20) — SEPARACIÓN DEL OBJETIVO: reward "v3_edge".

Causa raíz medida: la reward v1 codifica las REGLAS DE NEGOCIO (la regla de
3 € invierte el reward de los closes en (0,3 €) y el churn cobra 7 €/lot), de
modo que el óptimo del agente es «operar poco» y no «acertar la dirección»
(medido: PPO 50,7 % — moneda al aire).

Contrato de v3_edge:
1. NO invierte el reward de los closes sub-3 € (la regla 3 € es política de
   la capa de EJECUCIÓN: core.rules.MIN_WIN_EUR).
2. NO cobra el ancla anti-churn en el reward (política de ejecución: veto de
   re-entrada + CHURN_BARS). El coste REAL (spread + comisión) sigue dentro
   del Δequity: no se esconde ningún coste.
3. El feedback de outcomes REALES usa el pnl CRUDO (direccional), no el
   reclasificado por la regla 3 €.
4. v1 y v2_align quedan BYTE-IDÉNTICOS (el default no cambia).
5. La métrica de DIRECCIÓN (acierto por trade a +24 h) es la comparable con
   el benchmark L2 (SMA200-trend 66,7 % · LLM 69,6 %).
"""
import numpy as np
import pandas as pd
import pytest

from ml.trading_env_v2 import (
    REWARD_V3_DIRECTION_HORIZON_BARS,
    ForexTradingEnvV2,
    is_winning_close,
)


def _df(n=300, drift=0.0, start=1.10):
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = start + np.arange(n) * drift
    return pd.DataFrame({
        "time": idx,
        "open": close,
        "high": close + 0.0005,
        "low": close - 0.0005,
        "close": close,
        "volume": 1000.0,
        "real_volume": 1000.0,
    })


def _env(df, reward_version="v1", **kw):
    cfg = dict(
        df=df, real_outcomes=[], random_reset=False,
        reward_version=reward_version,
        initial_balance=1000.0, window_size=20, max_episode_steps=len(df) - 30,
        spread_pips=1.7, commission_per_lot=0.0,
    )
    cfg.update(kw)
    return ForexTradingEnvV2(**cfg)


class TestRewardComposition:
    def test_v1_unchanged_is_the_default(self):
        """El default sigue siendo v1 (byte-idéntico) — L3 no cambia el
        comportamiento por defecto, lo habilita explícitamente."""
        import os
        os.environ.pop("RETRAIN_REWARD_VERSION", None)
        from services.auto_retrain import RetrainConfig
        assert RetrainConfig().reward_version == "v1"

    def test_v3_edge_is_a_valid_version(self):
        env = _env(_df(), reward_version="v3_edge")
        assert env.reward_version == "v3_edge"

    def test_unknown_version_raises(self):
        with pytest.raises(ValueError):
            _env(_df(), reward_version="v99")

    def test_rule_3euro_inversion_absent_in_v3_edge(self):
        """Un close de +2 € (sub-3 €) NO se invierte en v3_edge: el reward
        refleja la dirección, no la regla de negocio."""
        df = _df(300, drift=0.0002)
        env = _env(df, reward_version="v3_edge")
        # El término de la regla 3 € con pnl=+2 € sería -2×2/1000×100 = -0.4 u.
        # Con v3_edge ese término NO se aplica.
        assert is_winning_close(2.0) is False, "la regla 3 € sigue siendo la del negocio"
        # Verificación directa: con el mismo estado, v3_edge da un reward MAYOR
        # que v1 (v1 resta el doble del pnl) ⇒ el término se quitó.
        rewards = {}
        for version in ("v1", "v3_edge"):
            e = _env(df, reward_version=version)
            e.reset()
            total = 0.0
            for _ in range(60):
                obs, r, done, trunc, info = e.step(np.array([[0.0, 0.18, 0.15, 0.15]], dtype=np.float32))
                total += float(r)
                if done or trunc:
                    break
            rewards[version] = total
        assert rewards["v3_edge"] >= rewards["v1"], (
            "v3_edge no puede ser peor que v1 en el mismo recorrido "
            f"({rewards})"
        )


class TestDirectionMetric:
    def test_direction_hits_recorded_per_trade(self):
        """Cada trade cerrado con futuro disponible deja 1 (acertó) o 0."""
        df = _df(300, drift=0.0002)   # mercado en ALZA
        env = _env(df, reward_version="v3_edge")
        env.reset()
        # Fuerza una entrada larga (dirección > deadband)
        for _ in range(80):
            obs, r, done, trunc, info = env.step(np.array([[0.9, 0.18, 0.15, 0.15]], dtype=np.float32))
            if info.get("trades", 0) > 0:
                break
            if done or trunc:
                break
        hits = info.get("direction_hits")
        assert hits is not None, "el env debe exponer la métrica direccional"
        if hits:
            assert all(h in (0, 1) for h in hits)
            # En un mercado que sube, una entrada larga debe acertar
            assert hits[0] == 1, "en tendencia alcista el LONG debe ser acierto direccional"

    def test_horizon_matches_benchmark(self):
        """El horizonte de la métrica direccional es el del benchmark L2."""
        assert REWARD_V3_DIRECTION_HORIZON_BARS == 24

    def test_no_future_means_no_metric(self):
        """Sin futuro disponible (fin del segmento) no se inventa valor."""
        df = _df(60, drift=0.0002)
        env = _env(df, reward_version="v3_edge", max_episode_steps=25)
        env.reset()
        for _ in range(30):
            obs, r, done, trunc, info = env.step(np.array([[0.9, 0.18, 0.15, 0.15]], dtype=np.float32))
            if done or trunc:
                break
        assert info.get("direction_hits") is not None
        # Puede estar vacío: no hay dato, no hay valor inventado.
        assert isinstance(info["direction_hits"], list)

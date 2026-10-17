"""T-24 (2026-10-17, orden «implementación de T-24»): recompensa «Sat-Align».

Contrato pre-registrado (audits/t24-reward-redesign-2026-10-17.md §8):

- ``reward_version="v1"`` (default) BYTE-IDÉNTICO al reward actual. Lo
  garantizan: (a) el bloque v2 sólo se evalúa bajo ``"v2_align"``; (b) igualdad
  EXACTA default vs ``"v1"`` explícito; (c) la suite completa — los tests de
  reward existentes corren sobre el default = v1, si el camino v1 cambiara,
  fallan.
- ``reward_version="v2_align"`` = v1 + exactamente DOS términos puntuales:
  ``r_v2 − r_v1 = −λ_sat·S·sat(dir) + λ_align·S·A_t`` (paridad punto a punto
  sobre trayectorias idénticas — se registra el estado observado por paso y se
  contrasta con la fórmula pre-registrada, §4 del diseño).
- Constantes CONGELADAS por las reglas de anclaje a-priori de §4 — NO retunear
  (si el gate T-27 falla se reporta y se para; protocolo anti-multiple-testing).

Nota de aislamiento: ``sma200_slope``/``adx`` se inyectan controladamente tras
``engineer`` (el env por defecto es ``trend_features=False`` → esas columnas NO
entran al obs, sólo al reward — ninguna trayectoria depende de ellas, sólo el
término v2). La honestidad de las propias columnas la cubre
``test_t22_trend_features``.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.trading_env_v2 import (
    REWARD_V2_ADX_TREND,
    REWARD_V2_D0_CEIL,
    REWARD_V2_LAMBDA_ALIGN,
    REWARD_V2_LAMBDA_SAT,
    REWARD_V2_S0_SLOPE,
    ForexTradingEnvV2,
    engineer_market_features,
)

INITIAL = 1000.0
SCALE = 100.0
LAMBDA_ALIGN_S = REWARD_V2_LAMBDA_ALIGN * SCALE   # 0,0298 u/paso a A=+1
LAMBDA_SAT_S = REWARD_V2_LAMBDA_SAT * SCALE       # 0,10 u a pleno techo


# ---------------------------------------------------------------------------
# Fixtures (patrón de test_trading_env_step: OHLC determinista)
# ---------------------------------------------------------------------------
def _raw_df(n=40):
    """OHLC determinista: close fijo y rango 0.0011 → ATR 10 pips exacto."""
    closes = np.full(n, 1.1000)
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="h"),
        "open": closes,
        "high": closes + 0.00055,
        "low": closes - 0.00055,
        "close": closes,
        "tick_volume": np.full(n, 1000),
    })


def _df(slope=0.0, adx=0.5):
    """df engineering + columnas de tendencia inyectadas (ver docstring)."""
    df = engineer_market_features(_raw_df())
    df["sma200_slope"] = float(slope)
    df["adx"] = float(adx)
    return df


def _env(df=None, **overrides):
    kwargs = dict(
        df=df if df is not None else _df(),
        window_size=5,
        initial_balance=INITIAL,
        lot_size=100000.0,
        max_lot=0.5,
        pip_size=0.0001,
        spread_pips=1.5,
        commission_per_lot=7.0,
        max_episode_steps=1000,
        max_holding_steps=120,
        reward_scale=SCALE,
        random_reset=False,
    )
    kwargs.update(overrides)
    return ForexTradingEnvV2(**kwargs)


def _run(actions, df=None, **overrides):
    """Trajetoria guionizada → (rewards, estados observados por paso).

    El estado registra la posición viva AL COMPUTAR el reward (la terminación
    sólo marca flag, no cierra → ``env.position`` tras ``step`` es esa misma
    posición) y la fila de la vela del paso (``current_step`` ya incrementado).
    """
    env = _env(df, **overrides)
    env.reset()
    rewards, states = [], []
    for act in actions:
        a = np.asarray(act, dtype=np.float32).reshape(-1)
        _, r, terminated, truncated, _info = env.step(a)
        rewards.append(float(r))
        states.append({
            "dir": float(a[0]),
            "pos": int(env.position),
            "candle_row": int(env.current_step) - 1,
        })
        if terminated or truncated:
            break
    return rewards, states


def _terms(df, state):
    """(parte_sat, parte_align) esperadas para un paso — fórmula pre-registrada."""
    excess = max(0.0, abs(state["dir"]) - REWARD_V2_D0_CEIL)
    sat = (excess * excess) / ((1.0 - REWARD_V2_D0_CEIL) ** 2)
    sat_part = -LAMBDA_SAT_S * sat
    align_part = 0.0
    if state["pos"] != 0:
        row = df.iloc[state["candle_row"]]
        slope = float(row["sma200_slope"])
        if np.isnan(slope):
            slope = 0.0          # neutral: paridad con el relleno de warm-up
        adx = float(row["adx"])
        if np.isnan(adx):
            adx = 0.5            # fallback del bloque de régimen (current_adx)
        a_t = float(np.sign(state["pos"])) * float(
            np.clip(slope / REWARD_V2_S0_SLOPE, -1.0, 1.0)
        ) * min(1.0, adx / REWARD_V2_ADX_TREND)
        align_part = LAMBDA_ALIGN_S * a_t
    return sat_part, align_part


# Trayectoria que cubre: abrir, invertir con churn (mismo paso), cerrar,
# saturar (0,95 → sat 0,25 y 1,0 → sat 1,0), paso plano sin término.
ACTIONS = [
    [0.5, 0.4],    # abre long   → pos=1 al reward (align +)
    [0.5, 0.4],    # mantiene    → align +
    [-0.5, 0.4],   # cierra + abre short el MISMO paso (churn) → align −
    [0.0, 0.0],    # cierra short → pos=0 (sin término)
    [0.95, 0.4],   # abre long con dir 0,95 → sat 0,25 + align +
    [1.0, 0.4],    # mantiene con dir 1,0 → sat 1,0 + align +
    [0.0, 0.0],    # cierra → pos=0 y sat=0 → Δ exacto 0
    [-0.5, 0.4],   # abre short → align −
]


class TestV1Parity:
    """El default debe ser byte-idéntico a v1 (§8): ninguna rama v1 cambia."""

    def test_default_is_v1(self):
        assert _env().reward_version == "v1"

    def test_invalid_version_raises_explicitly(self):
        # Sin fallbacks: versión desconocida = error ruidoso, nunca caer a v1.
        with pytest.raises(ValueError, match="reward_version"):
            _env(reward_version="v3")

    def test_default_exactly_equals_explicit_v1(self):
        r_def, _ = _run(ACTIONS)
        r_v1, _ = _run(ACTIONS, reward_version="v1")
        assert r_def == r_v1  # igualdad exacta (no aprox.): mismo camino de código


class TestV2PointwiseParity:
    """§8: v2 = v1 + EXACTAMENTE dos términos punto a punto."""

    def test_v2_minus_v1_equals_sat_plus_align_on_every_step(self):
        df = _df(slope=REWARD_V2_S0_SLOPE, adx=0.5)
        r_v1, states = _run(ACTIONS, df, reward_version="v1")
        r_v2, _ = _run(ACTIONS, df, reward_version="v2_align")
        assert len(r_v1) == len(r_v2) == len(ACTIONS)

        sat_any = align_any = zero_step = False
        for s, a, b in zip(states, r_v1, r_v2):
            sat_part, align_part = _terms(df, s)
            assert b - a == pytest.approx(sat_part + align_part, abs=1e-12)
            sat_any = sat_any or sat_part != 0.0
            align_any = align_any or align_part != 0.0
            zero_step = zero_step or (sat_part == 0.0 and align_part == 0.0)
        # la secuencia no puede ser vacía: ambos términos ejercitados + paso neutro
        assert sat_any and align_any and zero_step


class TestSaturationTerm:
    """(a) higiene: sólo |dir| > d0; magnitud-libre en el resto."""

    def test_below_ceiling_no_effect(self):
        # dir 0,5 vs 0,9 (ambos «mantener long»; decode magnitud-libre → misma
        # trayectoria) → idénticos bajo v2 (sat=0 en ambos)
        r_lo, _ = _run([[0.5, 0.4], [0.5, 0.4]], reward_version="v2_align")
        r_hi, _ = _run([[0.9, 0.4], [0.9, 0.4]], reward_version="v2_align")
        assert r_lo == pytest.approx(r_hi, abs=1e-12)

    def test_full_ceiling_costs_minus_010(self):
        # dir 1,0 → sat=1 en AMBOS pasos → −0,10 u por paso (decode magnitud-
        # libre: ambas mantienen long → el resto de la trayectoria es idéntica)
        r090, _ = _run([[0.9, 0.4], [0.9, 0.4]], reward_version="v2_align")
        r100, _ = _run([[1.0, 0.4], [1.0, 0.4]], reward_version="v2_align")
        assert r100[0] - r090[0] == pytest.approx(-0.10, abs=1e-12)
        assert r100[1] - r090[1] == pytest.approx(-0.10, abs=1e-12)

    def test_quarter_ceiling_costs_minus_0025(self):
        # sat(0,95) = (0,05)²/0,01 = 0,25 → −0,025 u; Δ vs pleno techo = −0,075.
        # Tol 1e-7: la acción viaja en float32 (0,95 no es exacto → cuantización
        # ~1e-8); la paridad punto a punto (mismo origen en ambos lados) usa 1e-12.
        r095, _ = _run([[0.95, 0.4], [0.95, 0.4]], reward_version="v2_align")
        r100, _ = _run([[1.0, 0.4], [1.0, 0.4]], reward_version="v2_align")
        assert r100[1] - r095[1] == pytest.approx(-0.075, abs=1e-7)


class TestAlignmentTerm:
    """(b) A_t = sign(pos)·clip(slope/s0,±1)·min(1, adx/0,25); neutro en flat/warm-up."""

    def _delta_vs_neutral(self, df_test, actions, r_neutral):
        r_test, _ = _run(actions, df_test, reward_version="v2_align")
        return [t - n for t, n in zip(r_test, r_neutral)]

    def test_long_aligned_pays_and_short_flips_sign(self):
        neutral = _df(slope=0.0, adx=0.5)
        r0, _ = _run([[0.5, 0.4], [0.5, 0.4]], neutral, reward_version="v2_align")
        # long con slope=+s0 → A=+1 en ambos pasos (pos=1 en open y hold)
        d_long = self._delta_vs_neutral(_df(slope=REWARD_V2_S0_SLOPE, adx=0.5),
                                        [[0.5, 0.4], [0.5, 0.4]], r0)
        assert d_long == pytest.approx([LAMBDA_ALIGN_S, LAMBDA_ALIGN_S], abs=1e-12)
        # short con slope=+s0 → signo invertido
        r0s, _ = _run([[-0.5, 0.4], [-0.5, 0.4]], neutral, reward_version="v2_align")
        d_short = self._delta_vs_neutral(_df(slope=REWARD_V2_S0_SLOPE, adx=0.5),
                                         [[-0.5, 0.4], [-0.5, 0.4]], r0s)
        assert d_short == pytest.approx([-LAMBDA_ALIGN_S, -LAMBDA_ALIGN_S], abs=1e-12)

    def test_anti_aligned_pays_negative(self):
        neutral = _df(slope=0.0, adx=0.5)
        r0, _ = _run([[0.5, 0.4], [0.5, 0.4]], neutral, reward_version="v2_align")
        d = self._delta_vs_neutral(_df(slope=-REWARD_V2_S0_SLOPE, adx=0.5),
                                   [[0.5, 0.4], [0.5, 0.4]], r0)
        assert d == pytest.approx([-LAMBDA_ALIGN_S, -LAMBDA_ALIGN_S], abs=1e-12)

    def test_slope_clips_at_plusminus_one(self):
        neutral = _df(slope=0.0, adx=0.5)
        r0, _ = _run([[0.5, 0.4], [0.5, 0.4]], neutral, reward_version="v2_align")
        d = self._delta_vs_neutral(_df(slope=5.0 * REWARD_V2_S0_SLOPE, adx=0.5),
                                   [[0.5, 0.4], [0.5, 0.4]], r0)
        assert d == pytest.approx([LAMBDA_ALIGN_S, LAMBDA_ALIGN_S], abs=1e-12)

    def test_adx_gate_scales_alignment(self):
        neutral = _df(slope=0.0, adx=0.5)
        r0, _ = _run([[0.5, 0.4], [0.5, 0.4]], neutral, reward_version="v2_align")
        # adx=0,10 → gate min(1, 0,10/0,25)=0,4 → A=0,4
        d = self._delta_vs_neutral(_df(slope=REWARD_V2_S0_SLOPE, adx=0.10),
                                   [[0.5, 0.4], [0.5, 0.4]], r0)
        assert d == pytest.approx([0.4 * LAMBDA_ALIGN_S] * 2, abs=1e-12)

    def test_flat_pays_nothing_regardless_of_slope(self):
        neutral = _df(slope=0.0, adx=0.5)
        r0, _ = _run([[0.0, 0.0], [0.0, 0.0]], neutral, reward_version="v2_align")
        d = self._delta_vs_neutral(_df(slope=REWARD_V2_S0_SLOPE, adx=0.5),
                                   [[0.0, 0.0], [0.0, 0.0]], r0)
        assert d == pytest.approx([0.0, 0.0], abs=1e-12)

    def test_nan_slope_is_neutral_and_reward_finite(self):
        # warm-up / datos insuficientes: slope NaN → término 0, reward sin NaN
        neutral = _df(slope=0.0, adx=0.5)
        r0, _ = _run([[0.5, 0.4], [0.5, 0.4]], neutral, reward_version="v2_align")
        df_nan = _df(slope=float("nan"), adx=0.5)
        r_nan, _ = _run([[0.5, 0.4], [0.5, 0.4]], df_nan, reward_version="v2_align")
        assert r_nan == pytest.approx(r0, abs=1e-12)
        assert all(np.isfinite(r) for r in r_nan)


class TestAutoRetrainWiring:
    """env_cfg (dict literal en _do_retrain) sólo se ejercita en un retrain real
    (los fallos son ruidosos: TypeError/ValueError) — se fija la línea de
    cableado con precedente de lectura de fuente (test_e2e_ai_predict)."""

    def test_config_default_is_v1(self):
        from services.auto_retrain import RetrainConfig
        assert RetrainConfig().reward_version == "v1"

    def test_env_cfg_carries_reward_version(self):
        src = (Path(__file__).resolve().parent.parent
               / "services" / "auto_retrain.py").read_text(encoding="utf-8")
        assert "reward_version=cfg.reward_version" in src

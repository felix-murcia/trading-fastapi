"""
P1-4 / P2-1: tests donde hay dinero — ForexTradingEnvV2.step (SL/TP, comisiones, reward).

El env simula la ejecución que después cobra el broker: cada aserción verifica
dinero concreto sobre balances exactos (initial=1000, lot=100000, comm=7/lot,
spread=1.5 pips, pip=1e-4).

Fixture P2-1: close fijo 1.1000 y rango 0.0011 → ATR(14) exacto 0.001 = 10 pips
→ production_stops() da SL 15 pips / TP 30 pips (misma fórmula que /predict).
La acción es [direction, volume] (Box(2,)); el trailing del EA queda inerte en
este fixture (nunca se recupera el riesgo de 15 pips con close fijo) — su
comportamiento se cubre en test_p21_unification.py.

Cubertura:
- Apertura: spread aplicado al entry + comisión cobrada al abrir.
- SL intra-vela: tiene PRIORIDAD sobre TP si ambos se alcanzan en la vela
  (gap favorable+adverso) → se sale al precio del SL.
- TP intra-vela: beneficio = distancia TP × volumen − comisión de cierre.
- Cierre discrecional (FLAT): paga el round-trip de spread (2× half-spread).
- Reward diferencial (Moody & Saffell): reward = Δequity/inicial × scale −
  penalties; primer paso sin movimiento ⇒ reward 0.
- Penalty por estancamiento (steps_in_trade > max_holding_steps).
- Terminación por quiebra (equity ≤ 50% del inicial) y truncado por fin de datos.
- info expone las claves de dinero (trades, win_rate, balance, equity, drawdown)
  de las que depende el gate walk-forward.
"""
import numpy as np
import pandas as pd
import pytest

from ml.trading_env_v2 import ForexTradingEnvV2, engineer_market_features

INITIAL = 1000.0
LOT = 100000.0
COMM = 7.0
VOL_OPEN = 0.2          # vol_norm 0.4 × max_lot 0.5
SL_PIPS = 15.0          # production_stops: ATR 10 pips × 1.5 (P2-1, no acción)
TP_PIPS = 30.0          # TP = 2 × SL
HALF_SPREAD = 1.5 * 0.0001 / 2   # 7.5e-5

OPEN = np.array([0.5, 0.4], dtype=np.float32)    # long, vol_norm=0.4 → 0.2 lots
OPEN_BIG = np.array([0.5, 1.0], dtype=np.float32)  # vol_norm=1.0 → 0.5 lots
FLAT = np.array([0.0, 0.0], dtype=np.float32)


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


def _env(df, **overrides):
    kwargs = dict(
        df=df,
        window_size=5,
        initial_balance=INITIAL,
        lot_size=LOT,
        max_lot=0.5,
        pip_size=0.0001,
        spread_pips=1.5,
        commission_per_lot=COMM,
        max_episode_steps=1000,
        max_holding_steps=120,
        reward_scale=100.0,
        random_reset=False,
    )
    kwargs.update(overrides)
    return ForexTradingEnvV2(**kwargs)


def _make_opened_env(action=OPEN, **overrides):
    """Env con reset + una posición abierta en la fila `window` con `action`."""
    env = _env(engineer_market_features(_raw_df()), **overrides)
    env.reset()
    entry_row_close = float(env.df.iloc[env.current_step]["close"])
    env.step(action)
    assert env.position == 1
    entry = entry_row_close + HALF_SPREAD
    return env, entry


class TestOpenAppliesSpreadAndCommission:
    def test_entry_includes_half_spread_and_commission_debits_balance(self):
        env = _env(engineer_market_features(_raw_df()))
        env.reset()
        close5 = float(env.df.iloc[5]["close"])

        _, reward, terminated, truncated, info = env.step(OPEN)

        assert env.position == 1
        assert env.entry_price == pytest.approx(close5 + HALF_SPREAD)
        assert env.volume == pytest.approx(VOL_OPEN, abs=1e-6)   # float32: 0.4×0.5
        expected_balance = INITIAL - VOL_OPEN * COMM          # 1000 − 1.4 = 998.6
        assert info["balance"] == pytest.approx(expected_balance)
        assert info["equity"] == pytest.approx(expected_balance)
        assert terminated is False and truncated is False
        # Stops de producción en la apertura (P2-1): no salen de la acción
        assert env.sl_pips == pytest.approx(SL_PIPS)
        assert env.tp_pips == pytest.approx(TP_PIPS)
        # Reward de apertura: Δequity = −comisión; −risk_penalty por capital en riesgo
        capital_at_risk = VOL_OPEN * LOT * (SL_PIPS * 0.0001)
        risk_penalty = (capital_at_risk / expected_balance) * 0.02
        assert reward == pytest.approx(
            (expected_balance - INITIAL) / INITIAL * 100.0 - risk_penalty
        )

    def test_flat_first_step_reward_is_zero(self):
        env = _env(engineer_market_features(_raw_df()))
        env.reset()

        _, reward, *_ = env.step(FLAT)

        assert reward == 0.0
        assert env.position == 0


class TestIntraCandleExits:
    def test_sl_has_priority_over_tp_when_both_hit(self):
        env, entry = _make_opened_env()
        sl = entry - SL_PIPS * 0.0001          # entry − 0.0015
        tp = entry + TP_PIPS * 0.0001          # entry + 0.0030
        # Vela que toca AMBOS: gap adverso (low ≤ sl) y favorable (high ≥ tp)
        env.df.loc[6, ["high", "low", "close"]] = [entry + 0.021, entry - 0.0016, entry]
        balance_before = env.balance                                # 998.6

        _, reward, terminated, _, info = env.step(FLAT)

        assert env.position == 0
        realized = VOL_OPEN * LOT * (sl - entry) - VOL_OPEN * COMM  # −30 −1.4
        expected_balance = balance_before + realized                # 967.2
        assert info["balance"] == pytest.approx(expected_balance)
        assert info["trades"] == 1
        assert info["win_rate"] == 0.0                              # pérdida
        assert terminated is False
        # El cierre NO reabre: la acción era FLAT
        assert env.position == 0
        # Reward diferencial del paso de cierre (sin risk_penalty: no abre)
        assert reward == pytest.approx(
            (expected_balance - balance_before) / INITIAL * 100.0
        )

    def test_tp_close_realizes_distance_minus_commission(self):
        env, entry = _make_opened_env()
        tp = entry + TP_PIPS * 0.0001
        # high toca TP; low NO toca SL (entry−0.0005 > entry−0.0015)
        env.df.loc[6, ["high", "low", "close"]] = [entry + 0.021, entry - 0.0005, entry]
        balance_before = env.balance

        _, reward, terminated, _, info = env.step(FLAT)

        realized = VOL_OPEN * LOT * (tp - entry) - VOL_OPEN * COMM  # 60 −1.4 = 58.6
        expected_balance = balance_before + realized                # 1057.2
        assert env.position == 0
        assert info["balance"] == pytest.approx(expected_balance)
        assert info["trades"] == 1
        assert info["win_rate"] == 1.0
        assert reward == pytest.approx(
            (expected_balance - balance_before) / INITIAL * 100.0
        )

    def test_agent_close_pays_round_trip_spread(self):
        env, entry = _make_opened_env()
        # Vela neutra en el MISMO close de la apertura → pierde 2× half-spread + comisión
        c = entry - HALF_SPREAD
        env.df.loc[6, ["high", "low", "close"]] = [c + 0.0002, c - 0.0002, c]
        balance_before = env.balance

        env.step(FLAT)

        # exit = close − half = c5 − 7.5e-5; entry = c5 + 7.5e-5 → diff = −0.00015
        realized = VOL_OPEN * LOT * (-2 * HALF_SPREAD) - VOL_OPEN * COMM  # −3 −1.4
        assert env.balance == pytest.approx(balance_before + realized)    # 994.2
        assert env.position == 0
        assert env.trade_count == 1

    def test_big_sl_terminates_on_50_percent_drawdown(self):
        # initial=100: el SL de 15 pips con 0.5 lots (−75) supera el 50% → quiebra
        env, entry = _make_opened_env(action=OPEN_BIG, initial_balance=100.0)
        sl = entry - SL_PIPS * 0.0001                            # entry − 0.0015
        env.df.loc[6, ["high", "low", "close"]] = [entry, entry - 0.011, entry - 0.005]

        _, _, terminated, _, info = env.step(FLAT)

        # 100 − 3.5 (apertura) − 75 (SL) − 3.5 (cierre) = 18 ≤ 50 → quiebra
        expected = 100.0 - 0.5 * COMM - 0.5 * LOT * (SL_PIPS * 0.0001) - 0.5 * COMM
        assert info["balance"] == pytest.approx(expected)       # 18.0
        assert terminated is True
        assert info["drawdown"] > 0.5
        assert info["win_rate"] == 0.0


class TestStagnationPenalty:
    def test_penalty_applies_after_max_holding_steps(self):
        env, _ = _make_opened_env(max_holding_steps=0)
        eq_open = env.equity                                   # tras apertura

        # HOLD: mantiene posición → steps_in_trade=1 > 0 → −0.001
        _, reward, _, _, info = env.step(OPEN)

        expected = (info["equity"] - eq_open) / INITIAL * 100.0 - 0.001
        assert reward == pytest.approx(expected)
        assert env.steps_in_trade == 1


class TestEpisodeBoundaries:
    def test_truncates_at_end_of_data(self):
        env = _env(engineer_market_features(_raw_df()))
        env.reset()

        steps = 0
        truncated = False
        while not truncated:
            _, _, terminated, truncated, _ = env.step(FLAT)
            steps += 1
            assert not terminated, "episodio no puede terminar sin operar"
            assert steps <= len(env.df), "sin truncado → bucle infinito"

        assert steps == len(env.df) - 1 - 5   # de `window` hasta len−1

    def test_info_exposes_money_keys_for_gate(self):
        env = _env(engineer_market_features(_raw_df()))
        env.reset()

        _, _, _, _, info = env.step(FLAT)

        for key in ("position", "volume", "sl_pips", "tp_pips",
                    "balance", "equity", "trades", "win_rate", "drawdown"):
            assert key in info, f"info sin clave de dinero: {key}"

    def test_observation_shape_matches_contract(self):
        env = _env(engineer_market_features(_raw_df()))
        obs, _ = env.reset()
        # 17 MARKET_FEATURES + 7 EXTRA_STATE_FEATURES, window 5 → (5, 24)
        assert obs.shape == (5, 24)
        assert env.observation_space.shape == (5, 24)

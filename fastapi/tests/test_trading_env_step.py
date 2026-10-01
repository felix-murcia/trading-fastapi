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
- P2-2 (sizing por riesgo): el volumen sale de size_ppo_position —
  lots = equity × 3% × vol_norm / (SL × ppv) — por lo que la pérdida de un SL
  queda ACOTADA al tope de riesgo (la quiebra en una operación es imposible);
  la rama de terminación (equity ≤ 50%) se cubre aparte forzando el balance.
- info expone las claves de dinero (trades, win_rate, balance, equity, drawdown)
  de las que depende el gate walk-forward.
"""
import numpy as np
import pandas as pd
import pytest

from ml.trading_env_v2 import CHURN_BARS, ForexTradingEnvV2, engineer_market_features

INITIAL = 1000.0
LOT = 100000.0
COMM = 7.0
# P2-2: sizing por riesgo — 1000 equity × 3% × vol_norm 0.4 / (SL 15 × ppv 10)
VOL_OPEN = 0.08
SL_PIPS = 15.0          # production_stops: ATR 10 pips × 1.5 (P2-1, no acción)
TP_PIPS = 30.0          # TP = 2 × SL
HALF_SPREAD = 1.5 * 0.0001 / 2   # 7.5e-5

OPEN = np.array([0.5, 0.4], dtype=np.float32)    # long, vol_norm=0.4 → 0.08 lots
OPEN_BIG = np.array([0.5, 1.0], dtype=np.float32)  # vol_norm=1.0 → 0.20 lots
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
        assert env.volume == pytest.approx(VOL_OPEN, abs=1e-6)   # P2-2: 0.08 por riesgo
        expected_balance = INITIAL - VOL_OPEN * COMM          # 1000 − 0.56 = 999.44
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
        balance_before = env.balance                                # 999.44

        _, reward, terminated, _, info = env.step(FLAT)

        assert env.position == 0
        realized = VOL_OPEN * LOT * (sl - entry) - VOL_OPEN * COMM  # −12 −0.56
        expected_balance = balance_before + realized                # 986.88
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

        realized = VOL_OPEN * LOT * (tp - entry) - VOL_OPEN * COMM  # 24 −0.56 = 23.44
        expected_balance = balance_before + realized                # 1022.88
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
        realized = VOL_OPEN * LOT * (-2 * HALF_SPREAD) - VOL_OPEN * COMM  # −1.2 −0.56
        assert env.balance == pytest.approx(balance_before + realized)    # 997.68
        assert env.position == 0
        assert env.trade_count == 1

    def test_sl_loss_is_capped_by_risk_pct_no_ruin_in_one_trade(self):
        # P2-2: sizing por riesgo → el SL de una operación NO puede llevar a
        # la quiebra. initial=100, vol_norm=1.0:
        #   lots = 100 × 3% × 1.0 / (15 pips × 10) = 0.02 (no 0.5)
        #   pérdida = 0.02 × 100000 × 0.0015 = 3.0 € (no 75 €)
        env, entry = _make_opened_env(action=OPEN_BIG, initial_balance=100.0)
        assert env.volume == pytest.approx(0.02, abs=1e-9)
        sl = entry - SL_PIPS * 0.0001                            # entry − 0.0015
        env.df.loc[6, ["high", "low", "close"]] = [entry, entry - 0.011, entry - 0.005]

        _, _, terminated, _, info = env.step(FLAT)

        expected = 100.0 - 0.02 * COMM - 0.02 * LOT * (SL_PIPS * 0.0001) - 0.02 * COMM
        assert info["balance"] == pytest.approx(expected)       # 96.72
        assert terminated is False
        assert info["drawdown"] <= 0.5
        # Pérdida total de la operación ≤ tope de riesgo (3%) + comisiones
        loss = 100.0 - info["balance"]
        assert loss <= 100.0 * 0.03 + 2 * 0.02 * COMM + 1e-9
        assert info["win_rate"] == 0.0

    def test_terminates_on_50_percent_drawdown(self):
        # Rama de terminación (equity ≤ 50% del inicial): con sizing por
        # riesgo la quiebra por una sola operación es imposible (test anterior),
        # así que se fuerza el balance (white-box) para cubrir el gate.
        env = _env(engineer_market_features(_raw_df()))
        env.reset()
        env.balance = INITIAL * 0.49
        env.equity = env.balance

        _, _, terminated, _, info = env.step(FLAT)

        assert terminated is True
        assert info["drawdown"] > 0.5


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
                    "balance", "equity", "trades", "win_rate", "drawdown",
                    "openings"):
            assert key in info, f"info sin clave de dinero: {key}"

    def test_observation_shape_matches_contract(self):
        env = _env(engineer_market_features(_raw_df()))
        obs, _ = env.reset()
        # 17 MARKET_FEATURES + 8 estado (7 + bars_since_close Fase 3), window 5
        assert obs.shape == (5, 25)
        assert env.observation_space.shape == (5, 25)
        # Contrato legado pre-memoria (bundle (20,24)): sin el canal 8º
        legacy = _env(engineer_market_features(_raw_df()), memory_channel=False)
        obs_l, _ = legacy.reset()
        assert obs_l.shape == (5, 24)
        assert legacy.observation_space.shape == (5, 24)


class TestOpeningsCounter:
    """Integridad de la métrica `openings` (fix 2026-10-01, O-4 backlog).

    `info["openings"]` cuenta aperturas REALES del env (0 → posición con
    volumen > 0), no la intención pre-step de la política. Antes el gate
    contaba la intención con umbral 0.33 (la env decodifica con zona muerta
    0.05) y sumaba intentos que el sizing denegaba → masa inflada
    (exp #14: 826 «aperturas» contadas vs 413 trades reales).
    """

    def test_real_open_counts_exactly_one_and_hold_adds_none(self):
        env = _env(engineer_market_features(_raw_df()))
        env.reset()

        _, _, _, _, info = env.step(OPEN)               # abre
        assert env.position == 1
        assert info["openings"] == 1

        _, _, _, _, info = env.step(OPEN)               # mantiene (HOLD)
        assert env.position == 1
        assert info["openings"] == 1                    # mantener no reabre

    def test_volume_denied_intent_counts_zero(self):
        env = _env(engineer_market_features(_raw_df()))
        env.reset()

        # dirección clara (1.0) pero vol_norm=0 → size_ppo_position() = 0.0
        # → el env no abre: la intención no es una apertura.
        _, _, _, _, info = env.step(np.array([1.0, 0.0], dtype=np.float32))

        assert env.position == 0
        assert info["openings"] == 0

    def test_counter_resets_with_episode(self):
        env = _env(engineer_market_features(_raw_df()))
        env.reset()
        env.step(OPEN)
        assert env.opening_count == 1

        env.reset()

        assert env.opening_count == 0
        _, _, _, _, info = env.step(FLAT)
        assert info["openings"] == 0


def _df_step_move(move: float, n: int = 40, jump: int = 26):
    """Cierre 1.1000 hasta `jump-1` y salto `move` desde `jump`.

    `engineer_market_features` descarta las 19 filas de warmup (SMA20) → el
    env ve las originales 19..39; el salto debe caer después de la fila 19
    (por eso `jump=26`: pasos 1-2 del env = originales 24-25 en 1.1000 y el
    cierre en el original 26). El rango intra-vela sigue en 0.0011 → TR
    idéntico al fixture plano → ATR y stops 15/30 pips intactos
    (production_stops se evalúa en el paso de apertura, anterior al salto)."""
    closes = np.full(n, 1.1000)
    closes[jump:] = 1.1000 + move
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="h"),
        "open": closes,
        "high": closes + 0.00055,
        "low": closes - 0.00055,
        "close": closes,
        "tick_volume": np.full(n, 1000),
    })


class TestFase3MemoryChurnAndThreeEuro:
    """Fase 3 (plan 2026-09-30): canal bars_since_close, recargo anti-churn
    (fricción completa, decisión de Félix 2026-10-01) y regla 3 € en el
    reward (R-1): un close ∈ (0, 3 €) se recompensa como pérdida."""

    def test_memory_channel_starts_fully_risen_and_counts_after_close(self):
        env = _env(engineer_market_features(_raw_df()))
        obs, _ = env.reset()
        # sin cierre previo: contador ≥ max_holding_steps → normalizado 1.0
        assert obs[:, -1] == pytest.approx(1.0)

        obs, *_ = env.step(FLAT)      # idx5: sin cierre → sigue "frío"
        assert obs[:, -1] == pytest.approx(1.0)

        obs, *_ = env.step(OPEN)      # idx6: abre; aún no ha cerrado nada
        assert env.position == 1
        assert obs[:, -1] == pytest.approx(1.0)

        obs, _, _, _, info = env.step(FLAT)   # idx7: cierra → 0 velas desde el cierre
        assert env.trade_count == 1
        assert obs[:, -1] == pytest.approx(0.0)
        # R-1: el gate necesita el pnl por trade (close discrecional pierde
        # round-trip de spread −1.2 € + comisión de cierre −0.56 €)
        assert info["trade_pnls"] == pytest.approx([-1.76])
        assert info["balance"] == pytest.approx(997.68)

        obs, *_ = env.step(FLAT)      # idx8: +1 vela
        assert obs[:, -1] == pytest.approx(1.0 / env.max_holding_steps)
        obs, *_ = env.step(FLAT)      # idx9: +2 velas
        assert obs[:, -1] == pytest.approx(2.0 / env.max_holding_steps)

    def test_sub_3euro_win_is_rewarded_as_a_loss(self):
        env = _env(engineer_market_features(_df_step_move(0.0003)))
        env.reset()
        env.step(FLAT)                              # paso 1
        _, r_open, *_ = env.step(OPEN)              # paso 2: abre (sin cierre previo)
        _, r_close, _, _, info = env.step(FLAT)     # paso 3: cierra en +0.64 €

        pnl = 0.08 * LOT * (0.0003 - 2 * HALF_SPREAD) - 0.08 * COMM
        assert pnl == pytest.approx(0.64)
        assert info["trade_pnls"] == pytest.approx([pnl])
        # la regla 3 € cambia la SEÑAL de aprendizaje, nunca el dinero
        assert info["balance"] == pytest.approx(INITIAL - 0.56 + pnl)
        assert env.wins == 0 and env.losses == 1     # win de dinero, pérdida AGENTS.md

        # el paso de cierre sólo suma el pnl realizado (+0.64 €): base 0.064
        # y la inversión R-1 −2×0.64×0.1 = −0.128 → reward −0.064 (PERDEDORA
        # para el aprendizaje aunque el dinero subió)
        assert r_close == pytest.approx(
            (pnl / INITIAL) * 100.0 - 2.0 * (pnl / INITIAL) * 100.0
        )
        assert r_close < 0.0
        # el ciclo completo (open+close) queda NEGATIVO: no compensa
        assert r_open + r_close < 0.0

    def test_win_at_or_above_3euros_keeps_positive_reward(self):
        env = _env(engineer_market_features(_df_step_move(0.0007)))
        env.reset()
        env.step(FLAT)                              # paso 1
        env.step(OPEN)                              # paso 2
        _, r_close, _, _, info = env.step(FLAT)     # paso 3: cierra en +3.84 €

        pnl = 0.08 * LOT * (0.0007 - 2 * HALF_SPREAD) - 0.08 * COMM
        assert pnl == pytest.approx(3.84)
        assert info["trade_pnls"] == pytest.approx([pnl])
        assert env.wins == 1 and env.losses == 0
        # delta = pnl realizado (+3.84 €) → SIN inversión, reward positivo
        assert r_close == pytest.approx((pnl / INITIAL) * 100.0)
        assert r_close > 0.0

    @staticmethod
    def _churn_scenario(open_delay: int, **over):
        """Cierra y reabre `open_delay` velas después (1..CHURN_BARS =
        recargo; CHURN_BARS+1 = sin recargo). Devuelve (env, reward_apertura).
        `over` se pasa al env (para el desacoplamiento ancla/comisión O-2)."""
        env = _env(engineer_market_features(_raw_df()), **over)
        env.reset()
        env.step(FLAT)                # paso 1
        env.step(OPEN)                # paso 2: abre
        env.step(FLAT)                # paso 3: cierra (bars=0)
        for _ in range(open_delay - 1):
            env.step(FLAT)            # velas de espera → bars = 1, 2, ...
        _, r_open, *_ = env.step(OPEN)   # reabre (la memoria ya avanzó en este paso)
        return env, r_open

    def test_reopen_within_churn_window_pays_full_friction(self):
        assert CHURN_BARS == 2   # las fronteras 2/3 de este test dependen de él

        env_free, r_free = self._churn_scenario(open_delay=CHURN_BARS + 1)
        assert env_free.ever_closed
        vol = env_free.volume
        # O-2: el recargo sale del ancla declarada (churn_friction_per_lot),
        # no de la comisión del bróker
        churn_expected = (2.0 * env_free.churn_friction_per_lot * vol / INITIAL) * 100.0
        assert churn_expected > 0.0

        for delay in (1, 2):   # ≤ CHURN_BARS → fricción completa
            env_c, r_c = self._churn_scenario(open_delay=delay)
            assert env_c.volume == pytest.approx(vol)   # misma historia y balance
            # misma delta y risk_penalty que la apertura "fría": la ÚNICA
            # diferencia del reward es el recargo anti-churn
            assert r_c == pytest.approx(r_free - churn_expected)

    def test_first_open_never_pays_churn(self):
        """Sin cierre previo (ever_closed False) la primera apertura es
        limpia — mismo reward exacto que asercionan los tests de apertura."""
        env = _env(engineer_market_features(_raw_df()))
        env.reset()
        _, r_open, *_ = env.step(OPEN)
        assert env.ever_closed is False
        expected_balance = INITIAL - VOL_OPEN * COMM
        capital_at_risk = VOL_OPEN * LOT * (SL_PIPS * 0.0001)
        risk_penalty = (capital_at_risk / expected_balance) * 0.02
        assert r_open == pytest.approx(
            (expected_balance - INITIAL) / INITIAL * 100.0 - risk_penalty
        )

    def test_churn_anchor_is_decoupled_from_broker_commission(self):
        """O-2 (2026-10-01): el recargo anti-churn usa el ancla declarada
        (churn_friction_per_lot), NO la comisión del bróker — que en el
        EURUSD real es 0 (271/271 deals sin commission/fee)."""
        # comisión bróker 0 (valor real) → el recargo sigue siendo el ancla
        env_free, r_free = self._churn_scenario(
            open_delay=CHURN_BARS + 1, commission_per_lot=0.0)
        env_churn, r_c = self._churn_scenario(
            open_delay=1, commission_per_lot=0.0)
        assert env_churn.churn_friction_per_lot == 7.0   # ancla por defecto
        vol = env_churn.volume
        anchor_expected = (2.0 * env_churn.churn_friction_per_lot * vol / INITIAL) * 100.0
        assert anchor_expected > 0.0
        assert env_churn.volume == pytest.approx(env_free.volume)
        assert r_c == pytest.approx(r_free - anchor_expected)

        # ancla 0 → sin recargo aunque la comisión del bróker sea 7.0
        env_free2, r_free2 = self._churn_scenario(
            open_delay=CHURN_BARS + 1, churn_friction_per_lot=0.0)
        env_na, r_na = self._churn_scenario(
            open_delay=1, churn_friction_per_lot=0.0)
        assert r_na == pytest.approx(r_free2)

    def test_zero_broker_commission_debits_nothing(self):
        """O-2 (2026-10-01): con la comisión real del bróker (0) apertura y
        cierre no pagan comisión — el round-trip sólo paga el spread (−1.2 €),
        no los −1.76 € del fixture con comisión 7 €/lot."""
        env = _env(engineer_market_features(_raw_df()), commission_per_lot=0.0)
        env.reset()
        env.step(OPEN)
        assert env.balance == pytest.approx(INITIAL)      # sin comisión de apertura
        _, _, _, _, info = env.step(FLAT)
        assert info["trade_pnls"] == pytest.approx([-1.2])
        assert info["balance"] == pytest.approx(INITIAL - 1.2)


class TestPositionVetoParity:
    """O-1 (diagnóstico 2026-10-01): el flip con posición abierta NO cierra
    ni revierte en el env — paridad con serve (`position_rejected` → HOLD)
    y con el EA (`rejected_position_open`, L407/L440/L475). Sólo SL/TP y la
    acción FLAT terminan un trade."""

    SHORT = np.array([-0.5, 0.4], dtype=np.float32)      # short, vol_norm=0.4
    FLIP_SHORT = np.array([-0.5, 0.4], dtype=np.float32)  # flip con LONG
    FLIP_LONG = np.array([0.5, 0.4], dtype=np.float32)    # flip con SHORT

    @staticmethod
    def _opened_short():
        """Env con posición SHORT abierta (espejo de _make_opened_env)."""
        env = _env(engineer_market_features(_raw_df()))
        env.reset()
        entry_row_close = float(env.df.iloc[env.current_step]["close"])
        env.step(TestPositionVetoParity.SHORT)
        assert env.position == -1
        return env, entry_row_close - HALF_SPREAD

    def test_flip_with_long_position_holds_without_closing(self):
        env, entry = _make_opened_env()
        _, _, terminated, truncated, info = env.step(self.FLIP_SHORT)

        assert env.position == 1                      # ni cierra ni revierte
        assert env.trade_count == 0
        assert info["trade_pnls"] == []
        assert env.volume == pytest.approx(VOL_OPEN, abs=1e-6)
        assert env.entry_price == pytest.approx(entry)
        assert terminated is False and truncated is False
        # Mark-to-market del paso: equity = balance + no realizado (misma
        # fórmula que un HOLD con dirección coincidente)
        mark = 1.1000 - HALF_SPREAD
        unrealized = VOL_OPEN * LOT * (mark - entry)
        assert info["equity"] == pytest.approx(info["balance"] + unrealized)

    def test_flip_with_short_position_holds_without_closing(self):
        env, entry = self._opened_short()
        _, _, terminated, truncated, info = env.step(self.FLIP_LONG)

        assert env.position == -1                     # espejo: no revierte
        assert env.trade_count == 0
        assert info["trade_pnls"] == []
        assert env.volume == pytest.approx(VOL_OPEN, abs=1e-6)
        assert env.entry_price == pytest.approx(entry)
        assert terminated is False and truncated is False

    def test_flip_state_is_identical_to_same_direction_hold(self):
        """El flip con posición abierta produce EXACTAMENTE el mismo estado
        que mantener la misma dirección — en serve ambos casos terminan en
        HOLD (`position_rejected` sólo convierte el flip a HOLD)."""
        e_hold = _env(engineer_market_features(_raw_df()))
        e_hold.reset()
        e_hold.step(OPEN)                    # abre LONG
        _, r_hold, term_h, trunc_h, info_h = e_hold.step(OPEN)  # misma dir → HOLD

        e_flip = _env(engineer_market_features(_raw_df()))
        e_flip.reset()
        e_flip.step(OPEN)                    # abre LONG
        _, r_flip, term_f, trunc_f, info_f = e_flip.step(self.FLIP_SHORT)

        assert e_flip.position == e_hold.position == 1
        assert e_flip.balance == e_hold.balance
        assert e_flip.equity == e_hold.equity
        assert e_flip.volume == e_hold.volume
        assert e_flip.trade_count == e_hold.trade_count == 0
        assert r_flip == pytest.approx(r_hold)          # misma delta → mismo reward
        assert (term_f, trunc_f) == (term_h, trunc_h)
        assert info_f["drawdown"] == pytest.approx(info_h["drawdown"])

    def test_flat_action_still_closes_the_position(self):
        """La otra mitad de la paridad: FLAT → CLOSE sí existe en producción
        (EA L358-377) — el veto es sólo del flip."""
        env, _entry = _make_opened_env()
        _, _, _, _, info = env.step(FLAT)

        assert env.position == 0
        assert env.trade_count == 1
        # cierre en flat con spread completo: −1.20 € de mercado − 0.56 € comm
        assert info["trade_pnls"] == pytest.approx(
            [VOL_OPEN * LOT * (-2 * HALF_SPREAD) - VOL_OPEN * COMM]
        )

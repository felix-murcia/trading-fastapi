"""
P2-1: unificación train/serve (D3 de la auditoría integral).

El MDP entrenado = MDP ejecutado:
- `production_stops()` es la ÚNICA fuente de verdad de SL/TP: la comparten el
  env (entrenamiento/gate) y `/api/v1/ai/predict` (inferencia) — mismo objeto,
  no una copia.
- La acción es [direction, volume] (Box(2,)); las cabezas sl/tp desaparecen.
  El bundle legado de 4 cabezas sigue siendo evaluable (gate walk-forward) y
  servible (predict) durante la transición.
- El trailing del EA está modelado en el env, espejo de `ManageTrailingStop`
  (mql5 §V): activación al recuperar el riesgo, destino entry+max(25% riesgo,
  minTrail), solo aprieta, respeta stops-level y se congela tras el primer
  movimiento (igual que el `return` del EA con riskDist ≤ 0).
- El retrain detecta el cambio de contrato y reentrena desde cero; el pickle
  de VecNormalize no arrastra el action_space viejo al env de train.
"""
import numpy as np
import pandas as pd
import pytest

from ml.trading_env_v2 import (
    ForexTradingEnvV2,
    engineer_market_features,
    effective_sl_pips,
    production_sl,
    production_stops,
    production_stops_with_paying_tp,
    target_level_candidates,
)

PIP = 0.0001


def _raw_df(n=40):
    """OHLC determinista: close fijo 1.1000, rango 0.0011 → ATR 10 pips
    (0.0011/1.1 = 0.001) tras el warmup → stops 15/30 pips."""
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
        initial_balance=1000.0,
        lot_size=100000.0,
        max_lot=0.5,
        pip_size=PIP,
        spread_pips=1.5,
        commission_per_lot=7.0,
        max_episode_steps=1000,
        max_holding_steps=120,
        reward_scale=100.0,
        random_reset=False,
    )
    kwargs.update(overrides)
    return ForexTradingEnvV2(**kwargs)


LONG = np.array([0.5, 0.4], dtype=np.float32)
SHORT = np.array([-0.5, 0.4], dtype=np.float32)


class TestProductionStopsSingleSource:
    def test_formula_and_clamps(self):
        # ATR 10 pips → ×1.5 = 15 (dentro de [10,50]) y TP relación 1:2
        sl, tp = production_stops(0.001, PIP)
        assert sl == pytest.approx(15.0)
        assert tp == pytest.approx(30.0)
        # clamp inferior: ATR 4 pips → ×1.5 = 6 → 10 (TP 20)
        assert production_stops(0.0004, PIP) == (10.0, 20.0)
        # clamp superior: ATR 100 pips → ×1.5 = 150 → 50 (TP 100)
        assert production_stops(0.01, PIP) == (50.0, 100.0)

    def test_predict_imports_the_same_function_object(self):
        # Si /predict dejara de importar esta función (p.ej. una copia local),
        # la paridad train/serve se rompería en silencio → este test la bloquea.
        # G3-2 (2026-10-22): la geometría se compone de DOS funciones (el SL y
        # el TP) ⇒ las DOS tienen que ser el MISMO objeto en el router y en el
        # env (una copia local en cualquiera de los dos lados rompe la paridad).
        import routers.ai as ai_router
        assert ai_router.production_sl is production_sl
        assert ai_router.production_stops_with_paying_tp is production_stops_with_paying_tp
        assert ai_router.target_level_candidates is target_level_candidates

    def test_env_open_uses_production_stops_not_the_action(self):
        env = _env()
        env.reset()
        env.step(LONG)
        entry_candle_atr = float(env.df.iloc[env.current_step - 1]["atr"])
        sl_expected, tp_expected = production_stops(entry_candle_atr, PIP)
        # G3-2: el env simula el SL REAL (geométrico + buffer de spread
        # del EA) ⇒ el sizing y el PnL usan la distancia que se ejecuta.
        assert env.sl_pips == pytest.approx(effective_sl_pips(sl_expected))
        assert env.tp_pips == pytest.approx(tp_expected)
        assert env.sl_pips == pytest.approx(17.0)   # ATR 10 × 1.5 + buffer 2
        assert env.tp_pips == pytest.approx(30.0)


class TestActionContract:
    def test_action_space_is_two_dimensional(self):
        env = _env()
        assert env.action_space.shape == (2,)
        assert list(env.action_space.low) == [-1.0, 0.0]
        assert list(env.action_space.high) == [1.0, 1.0]

    def test_legacy_four_dim_action_uses_first_two_and_ignores_sltp_heads(self):
        env = _env()
        env.reset()
        env.step(np.array([0.5, 0.4, 0.99, 0.99], dtype=np.float32))
        assert env.position == 1
        # P2-2: sizing por riesgo — 1000 × 3% × 0.4 / (17 pips × 10) = 0.07
        # (G3-2: el SL real incluye el buffer de spread del EA)
        assert env.volume == pytest.approx(0.07, abs=1e-6)
        # D3: las cabezas sl/tp NO deciden los stops
        assert env.sl_pips == pytest.approx(17.0)   # SL real (15 + buffer 2)
        assert env.tp_pips == pytest.approx(30.0)

    def test_step_rejects_unknown_action_sizes(self):
        env = _env()
        env.reset()
        for bad in ([0.5], [0.5, 0.4, 0.1], [0.5] * 5):
            with pytest.raises(ValueError, match="elementos"):
                env.step(np.array(bad, dtype=np.float32))


class TestTrailingMirrorsEA:
    """Espejo de ManageTrailingStop (mql5 §V)."""

    def _opened(self, **overrides):
        env = _env(**overrides)
        env.reset()
        env.step(LONG)
        assert env.position == 1
        entry = env.entry_price
        assert env.sl_price == pytest.approx(entry - 17 * PIP)   # SL real (15 geom + 2 buffer)
        return env, entry

    def test_no_activation_below_full_risk_recovery(self):
        env, entry = self._opened()
        sl_before = env.sl_price
        # precio recupera PARTE del riesgo (15 pips) pero no todo → sin cambios
        env.df.loc[6, ["high", "low", "close"]] = [
            entry + 0.0010, entry - 0.0010, entry + 0.0008,
        ]
        env.step(LONG)

        assert env.position == 1
        assert env.sl_price == pytest.approx(sl_before)
        assert env.sl_pips == pytest.approx(17.0)   # no recalculado

    @staticmethod
    def _ea_min_trail_pips(env, entry, close, position=1):
        """minTrail del EA: max(TrailingMinPips, ATR real/pip × 2) con
        ATR real ≈ atr_norm × close (el ATR del env es adimensional)."""
        atr_norm = float(env.df.iloc[env.current_step]["atr"])
        atr_real_pips = (atr_norm * close) / PIP
        return max(env.trailing_min_pips, atr_real_pips * env.trailing_atr_mult)

    def test_activation_locks_profit_at_max_percent_or_min_trail(self):
        env, entry = self._opened()
        # mark = close − half ≥ entry+15p activa; newSL ≤ mark − 1 pip exige
        # close ≥ entry+23.8p; high < TP (entry+30p) para no salir antes.
        close6 = entry + 0.0024
        env.df.loc[6, ["high", "low", "close"]] = [entry + 0.0026, entry - 0.0010, close6]
        env.step(LONG)

        # destino = entry + max(25% riesgo, minTrail) — minTrail domina aquí
        min_trail = self._ea_min_trail_pips(env, entry, close6)
        assert min_trail > env.trailing_percent * 15.0   # 22p > 3.75p
        assert env.position == 1
        assert env.sl_price == pytest.approx(entry + min_trail * PIP, abs=1e-9)
        assert env.sl_pips == pytest.approx(min_trail, abs=1e-6)
        assert env.tp_price == pytest.approx(entry + 30 * PIP, abs=1e-9)  # TP intacto

    def test_trailing_applies_from_next_candle_no_lookahead(self):
        env, entry = self._opened()
        # vela 6: cierra lo suficiente para activar; su low NO toca el SL viejo
        # (entry−15p) → la posición sigue abierta sin importar el nuevo SL.
        close6 = entry + 0.0024
        env.df.loc[6, ["high", "low", "close"]] = [entry + 0.0026, entry - 0.0014, close6]
        env.step(LONG)
        assert env.position == 1
        min_trail = self._ea_min_trail_pips(env, entry, close6)
        assert env.sl_price == pytest.approx(entry + min_trail * PIP, abs=1e-9)
        trailed_sl = env.sl_price

        # vela 7: el SL trailado ya rige → salida en beneficio a ese precio
        env.df.loc[7, ["high", "low", "close"]] = [
            entry + 0.0026, entry + 0.0021, entry + 0.0023,
        ]
        balance_before = env.balance
        v = env.volume  # P2-2: sizing por riesgo — capturar ANTES (close pone volume=0)
        env.step(np.array([0.0, 0.0], dtype=np.float32))

        assert env.position == 0
        realized = v * 100000 * (trailed_sl - entry) - v * 7.0
        assert env.balance == pytest.approx(balance_before + realized, rel=1e-9)
        assert env.trade_count == 1

    def test_freezes_after_first_move_like_ea_return(self):
        env, entry = self._opened()
        close6 = entry + 0.0024
        env.df.loc[6, ["high", "low", "close"]] = [entry + 0.0026, entry - 0.0010, close6]
        env.step(LONG)
        sl_after_first = env.sl_price
        assert sl_after_first > entry   # ya en beneficio

        # precio aún más arriba: riskDist = entry − SL_actual ≤ 0 → el EA hace
        # `return` → SL congelado (vela acotada: low > SL trailado, high < TP)
        env.df.loc[7, ["high", "low", "close"]] = [
            entry + 0.0026, entry + 0.0023, entry + 0.0025,
        ]
        env.step(LONG)

        assert env.position == 1
        assert env.sl_price == pytest.approx(sl_after_first)

    def test_sell_trailing_is_symmetric(self):
        env = _env()
        env.reset()
        env.step(SHORT)
        assert env.position == -1
        entry = env.entry_price
        assert env.sl_price == pytest.approx(entry + 17 * PIP)

        # precio CAE el riesgo completo: mark = close + half ≤ entry − 15p y
        # newSL ≥ mark + 1 pip; low > TP (entry−30p) para no salir antes.
        close6 = entry - 0.0024
        env.df.loc[6, ["high", "low", "close"]] = [entry + 0.0010, entry - 0.0026, close6]
        env.step(SHORT)

        min_trail = self._ea_min_trail_pips(env, entry, close6)
        assert env.position == -1
        assert env.sl_price == pytest.approx(entry - min_trail * PIP, abs=1e-9)
        assert env.sl_pips == pytest.approx(min_trail, abs=1e-6)
        assert env.sl_price < entry   # SL en beneficio (espejo BUY)

    def test_trailing_disabled_keeps_initial_sl(self):
        # P1: el env modela los DOS inputs independientes del EA
        # (EnableTrailing / EnableBreakeven) — para aislar el trailing aquí se
        # desactivan AMBOS: con breakeven activo, close = entry+23.8p (≥1R)
        # movería la SL a la entrada.
        env, entry = self._opened(trailing_enabled=False, breakeven_enabled=False)
        sl_before = env.sl_price
        env.df.loc[6, ["high", "low", "close"]] = [
            entry + 0.0025, entry - 0.0010, entry + 0.0023,
        ]
        env.step(LONG)

        assert env.sl_price == pytest.approx(sl_before)
        assert env.sl_pips == pytest.approx(17.0)


class TestRetrainContractChange:
    """P2-1: el pipeline de retrain afronta el cambio 4→2 de forma explícita."""

    @staticmethod
    def _train_env(df, window=5):
        from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
        cfg = dict(df=df, window_size=window, random_reset=False, max_episode_steps=30)
        return VecNormalize(
            DummyVecEnv([lambda: ForexTradingEnvV2(**cfg)]),
            norm_obs=True, norm_reward=True,
        )

    def test_make_train_env_refreshes_stale_action_space_from_pickle(self, tmp_path):
        """El pickle de VecNormalize arrastra el action_space (4,) del bundle
        origen (set_venv no lo refresca): sin la corrección, PPO construiría
        la política con la cabeza vieja."""
        import gymnasium as gym
        from gymnasium import spaces
        from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

        from services.auto_retrain import _make_train_env
        from services.model_registry import build_manifest, write_manifest

        window = 20   # obs (20,25) del env Fase 3 → check_shape_equal del load

        class _LegacyAct4Env(gym.Env):
            # Fase 3: el obs del pickle CASA con el contrato nuevo (20,25);
            # lo obsoleto es la CABEZA de acción (4). El caso obs obsoleto
            # (20,24 → stats nuevos desde cero) cubre
            # test_make_train_env_rebuilds_stats_on_obs_contract_change.
            observation_space = spaces.Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
            action_space = spaces.Box(
                low=np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32),
                high=np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32),
            )

            def reset(self, seed=None, options=None):
                super().reset(seed=seed)
                return self.observation_space.sample(), {}

            def step(self, action):
                return self.observation_space.sample(), 0.0, False, False, {}

        zip_path = tmp_path / "ppo_trading_bot_v3.zip"
        zip_path.write_bytes(b"fake-ppo-zip")
        pkl_path = tmp_path / "ppo_trading_bot_v3_vec_norm.pkl"
        legacy = VecNormalize(
            DummyVecEnv([_LegacyAct4Env]), norm_obs=True, norm_reward=True,
        )
        legacy.reset()
        legacy.save(str(pkl_path))
        assert tuple(legacy.action_space.shape) == (4,)   # pkl "viejo" real

        write_manifest(str(zip_path), build_manifest(
            str(zip_path), obs_shape=(20, 25),
            vec_norm_file=pkl_path.name, source="test",
        ))
        env_cfg = dict(df=engineer_market_features(_raw_df()), window_size=window,
                       random_reset=False, max_episode_steps=30)
        env = _make_train_env(env_cfg, str(zip_path))

        assert tuple(env.action_space.shape) == (2,)
        assert tuple(env.venv.action_space.shape) == (2,)

    def test_make_train_env_rebuilds_stats_on_obs_contract_change(self, tmp_path):
        """Fase 3: bundle pre-memoria (obs 24) sobre env con memoria (25) →
        VecNormalize NUEVO desde cero (SB3 rechazaría el set_venv con un
        AssertionError críptico; la normalización se reconstruye, igual que
        la cabeza de acción en P2-1)."""
        from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

        from services.auto_retrain import _make_train_env
        from services.model_registry import build_manifest, write_manifest

        window = 20
        zip_path = tmp_path / "ppo_trading_bot_v3.zip"
        zip_path.write_bytes(b"fake-ppo-zip")
        pkl_path = tmp_path / "ppo_trading_bot_v3_vec_norm.pkl"
        legacy = VecNormalize(
            DummyVecEnv([lambda: ForexTradingEnvV2(
                df=engineer_market_features(_raw_df()), window_size=window,
                random_reset=False, max_episode_steps=30, memory_channel=False,
            )]),
            norm_obs=True, norm_reward=True,
        )
        legacy.reset()
        legacy.save(str(pkl_path))
        assert tuple(legacy.observation_space.shape) == (20, 24)  # contrato viejo

        write_manifest(str(zip_path), build_manifest(
            str(zip_path), obs_shape=(20, 24),
            vec_norm_file=pkl_path.name, source="test",
        ))
        # env_cfg SIN memory_channel → contrato nuevo (20,25) por defecto
        env_cfg = dict(df=engineer_market_features(_raw_df()), window_size=window,
                       random_reset=False, max_episode_steps=30)
        env = _make_train_env(env_cfg, str(zip_path))

        # stats del pickle (20,24) NO se pueden usar: se reconstruyen a 25
        assert tuple(env.observation_space.shape) == (20, 25)
        assert np.shape(env.obs_rms.mean) == (20, 25)

    def test_load_or_create_model_fresh_on_legacy_obs_contract(self, tmp_path):
        """Fase 3: zip pre-memoria (20,24) + env con memoria (20,25) →
        reentreno desde cero con el contrato nuevo (mismo precedente P2-1)."""
        from stable_baselines3 import PPO
        from stable_baselines3.common.vec_env import DummyVecEnv

        from services.auto_retrain import RetrainConfig, _load_or_create_model

        legacy_df = engineer_market_features(_raw_df())
        legacy_zip = tmp_path / "legacy24.zip"
        PPO("MlpPolicy", DummyVecEnv([lambda: ForexTradingEnvV2(
            df=legacy_df, window_size=20, random_reset=False, max_episode_steps=30,
            memory_channel=False,
        )]), n_steps=8, verbose=0).save(str(legacy_zip))

        env = self._train_env(legacy_df, window=20)   # (20,25) con memoria
        model = _load_or_create_model(str(legacy_zip), env, RetrainConfig)

        # contrato incompatible → política NUEVA con el obs del env
        assert tuple(model.observation_space.shape) == (20, 25)
        assert tuple(model.action_space.shape) == (2,)
        assert model.seed is None   # recién creada (el zip legado no tenía seed)

    def test_load_or_create_model_fresh_when_no_model(self, tmp_path):
        from services.auto_retrain import RetrainConfig, _load_or_create_model

        env = self._train_env(engineer_market_features(_raw_df()))
        model = _load_or_create_model(str(tmp_path / "nope.zip"), env, RetrainConfig)

        assert tuple(model.action_space.shape) == (2,)

    def test_load_or_create_model_fresh_on_legacy_action_contract(self, tmp_path):
        import gymnasium as gym
        from gymnasium import spaces
        from stable_baselines3 import PPO
        from stable_baselines3.common.vec_env import DummyVecEnv

        from services.auto_retrain import RetrainConfig, _load_or_create_model

        class _Act4Env(gym.Env):
            observation_space = spaces.Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
            action_space = spaces.Box(
                low=np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32),
                high=np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32),
            )

            def reset(self, seed=None, options=None):
                super().reset(seed=seed)
                return self.observation_space.sample(), {}

            def step(self, action):
                return self.observation_space.sample(), 0.0, False, False, {}

        legacy_zip = tmp_path / "legacy4.zip"
        PPO("MlpPolicy", DummyVecEnv([_Act4Env]), n_steps=8, verbose=0).save(str(legacy_zip))

        env = self._train_env(engineer_market_features(_raw_df()))
        model = _load_or_create_model(str(legacy_zip), env, RetrainConfig)

        # contrato incompatible → política NUEVA de 2 (nunca arrastra la vieja)
        assert tuple(model.action_space.shape) == (2,)
        assert model.seed is None   # recién creada (el zip legado no tenía seed)

    def test_load_or_create_model_continues_when_contract_matches(self, tmp_path):
        from stable_baselines3 import PPO

        from services.auto_retrain import RetrainConfig, _load_or_create_model

        env = self._train_env(engineer_market_features(_raw_df()))
        zip_path = tmp_path / "compatible.zip"
        PPO("MlpPolicy", env, n_steps=8, seed=42, verbose=0).save(str(zip_path))

        model = _load_or_create_model(str(zip_path), env, RetrainConfig)

        # mismo contrato → continúa desde el zip (seed=42 solo sobrevive si se cargó)
        assert tuple(model.action_space.shape) == (2,)
        assert model.seed == 42

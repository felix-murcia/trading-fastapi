"""
ForexTradingEnv v3 — Arquitectura Óptima para RL (PPO)
=====================================================
- Recompensa diferencial telescópica: R_t = (Equity_t - Equity_{t-1}) / Balance_0.
- Ejecución realista OHLC: Detección de SL/TP intra-vela por High/Low con sesgo conservador.
- Espacio de acción continuo optimizado: Zona muerta reducida a [-0.05, 0.05] para preservar gradientes.
- Observación extendida: Distancias normalizadas a SL/TP por ATR y tiempo transcurrido en el trade.
- Manejo correcto de fin de episodio: Bootstrapping en 'truncated' sin liquidaciones forzadas artificiales.
"""

from datetime import datetime
from typing import Optional, Tuple
import gymnasium as gym
from gymnasium import spaces
import numpy as np
import pandas as pd


MARKET_FEATURES = (
    "spread", "real_volume",
    "returns", "range", "sma20", "dist_sma20",
    "macd", "macd_signal", "macd_hist", "tr", "atr",
    "rsi14", "bb_pos", "lag_return_1", "lag_return_2",
    "lag_return_3", "lag_return_5",
)

EXTRA_STATE_FEATURES = 7  # [entry_norm, pos, unrealized_pnl, balance_norm, dist_sl, dist_tp, holding_time]


def engineer_market_features(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula indicadores técnicos normalizados y acotados para redes neuronales."""
    result = df.copy()
    close = result["close"].astype(float)
    high = result["high"].astype(float)
    low = result["low"].astype(float)

    result["spread"] = result.get("spread", 0.0)
    if "real_volume" not in result:
        result["real_volume"] = result.get("tick_volume", result.get("volume", 0.0))

    result["returns"] = close.pct_change().fillna(0.0)
    result["range"] = (high - low) / (close + 1e-10)
    result["sma20"] = close.rolling(20).mean()
    result["dist_sma20"] = (close - result["sma20"]) / (close + 1e-10)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    result["macd"] = macd / (close + 1e-10)
    result["macd_signal"] = signal / (close + 1e-10)
    result["macd_hist"] = (macd - signal) / (close + 1e-10)

    # True Range con gaps de apertura
    prev_close = close.shift(1)
    tr_components = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1)
    result["tr"] = tr_components.max(axis=1) / (close + 1e-10)
    result["atr"] = result["tr"].rolling(14).mean()

    # RSI normalizado en rango [0, 1]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    result["rsi14"] = (100.0 - (100.0 / (1.0 + rs))) / 100.0

    # Bollinger Bands Position acotada para mitigar explosión de outliers
    std20 = close.rolling(20).std()
    bb_raw = (close - result["sma20"]) / (2.0 * std20 + 1e-10)
    result["bb_pos"] = np.clip(bb_raw, -3.0, 3.0)

    for lag in (1, 2, 3, 5):
        result[f"lag_return_{lag}"] = result["returns"].shift(lag).fillna(0.0)

    return result.dropna().reset_index(drop=True)


def get_market_features(df: pd.DataFrame) -> list[str]:
    """Valida la presencia ordenada del vector de features de mercado."""
    for feature in MARKET_FEATURES:
        if feature not in df.columns:
            df[feature] = 0.0
    return list(MARKET_FEATURES)


def build_v3_observation(
    market_values: np.ndarray,
    expected_market_features: int,
    position: int,
    entry_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    current_atr: float,
    steps_in_trade: int,
    max_holding_steps: int,
    balance: float,
    initial_balance: float,
    window_size: int = 20,
) -> np.ndarray:
    """Genera el tensor de estado (window_size, total_features) invariante al par de divisas."""
    last_market = np.asarray(market_values[-window_size:], dtype=np.float32)
    feats_present = last_market.shape[1]

    if feats_present < expected_market_features:
        pad = np.zeros((window_size, expected_market_features - feats_present), dtype=np.float32)
        last_market = np.hstack([last_market, pad])
    elif feats_present > expected_market_features:
        last_market = last_market[:, :expected_market_features]

    atr_safe = max(current_atr, 1e-5)
    
    if position != 0 and entry_price > 0:
        entry_norm_val = (current_price - entry_price) / entry_price
        unrealized_pct = (current_price - entry_price) / entry_price if position == 1 else (entry_price - current_price) / entry_price
        dist_sl = (current_price - sl_price) / (atr_safe * current_price) if position == 1 else (sl_price - current_price) / (atr_safe * current_price)
        dist_tp = (tp_price - current_price) / (atr_safe * current_price) if position == 1 else (current_price - tp_price) / (atr_safe * current_price)
        holding_norm = min(1.0, steps_in_trade / max(1, max_holding_steps))
    else:
        entry_norm_val = 0.0
        unrealized_pct = 0.0
        dist_sl = 0.0
        dist_tp = 0.0
        holding_norm = 0.0

    state_extensions = np.tile(
        np.array([
            entry_norm_val,
            float(position),
            unrealized_pct * 10.0,
            balance / initial_balance,
            np.clip(dist_sl, -5.0, 5.0),
            np.clip(dist_tp, -5.0, 5.0),
            holding_norm
        ], dtype=np.float32),
        (window_size, 1)
    )

    return np.hstack([last_market, state_extensions])


def decode_v3_direction(direction: float, current_position: int) -> Tuple[int, str]:
    """
    Decodifica la acción con zona muerta reducida (5%) para evitar mesetas de gradiente nulo.
    1: LONG, -1: SHORT, 0: FLAT/CLOSE.
    """
    if direction < -0.05:
        target_pos = -1
    elif direction > 0.05:
        target_pos = 1
    else:
        target_pos = 0

    if target_pos == 1:
        decision = "HOLD" if current_position == 1 else "BUY"
    elif target_pos == -1:
        decision = "HOLD" if current_position == -1 else "SELL"
    else:
        decision = "CLOSE" if current_position != 0 else "FLAT"

    return target_pos, decision


class ForexTradingEnvV2(gym.Env):
    """
    Entorno Forex con simulación estocástica por episodios, PnL estándar y recompensa incremental.
    """
    metadata = {'render_modes': ['human']}

    def __init__(
        self,
        df: pd.DataFrame,
        window_size: int = 20,
        initial_balance: float = 10000.0,
        lot_size: float = 100000.0,
        max_lot: float = 0.5,
        max_sl_pips: float = 100.0,
        max_tp_pips: float = 200.0,
        pip_size: float = 0.0001,
        spread_pips: float = 1.5,
        commission_per_lot: float = 7.0,
        max_episode_steps: int = 1000,
        max_holding_steps: int = 120,
        reward_scale: float = 100.0,
        random_reset: bool = True,
        real_outcomes: Optional[list] = None,
        real_outcome_weight: float = 0.1,
    ):
        super(ForexTradingEnvV2, self).__init__()
        self.df = df.reset_index(drop=True)
        self.window_size = window_size
        self.initial_balance = initial_balance
        self.lot_size = lot_size
        self.max_lot = max_lot
        self.max_sl_pips = max_sl_pips
        self.max_tp_pips = max_tp_pips
        self.pip_size = pip_size
        self.spread = spread_pips * pip_size
        self.commission_per_lot = commission_per_lot
        self.max_episode_steps = max_episode_steps
        self.max_holding_steps = max_holding_steps
        self.reward_scale = reward_scale
        self.random_reset = random_reset

        self.real_outcomes = real_outcomes or []
        self.real_outcome_weight = real_outcome_weight
        self._real_outcomes_by_step = self._index_real_outcomes(self.real_outcomes)
        self._used_outcomes = set()

        self.features = get_market_features(self.df)
        self.action_space = spaces.Box(
            low=np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            high=np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32),
            dtype=np.float32
        )
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(window_size, len(self.features) + EXTRA_STATE_FEATURES),
            dtype=np.float32
        )

        # Variables de estado
        self.current_step = 0
        self.start_step = 0
        self.position = 0
        self.entry_price = 0.0
        self.volume = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0
        self.sl_pips = 0.0
        self.tp_pips = 0.0
        self.steps_in_trade = 0

        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.prev_equity = self.initial_balance
        self.max_equity = self.initial_balance

        self.trade_count = 0
        self.wins = 0
        self.losses = 0

    def _index_real_outcomes(self, outcomes: list) -> dict:
        if not outcomes or 'time' not in self.df.columns:
            return {}
        try:
            time_col_ts = pd.to_datetime(self.df['time'], utc=True).apply(lambda x: x.timestamp())
        except Exception:
            return {}

        indexed = {}
        for outcome in outcomes:
            entry_t = self._coerce_timestamp(outcome.entry_time)
            if entry_t is None:
                continue
            matches = self.df.index[time_col_ts >= entry_t].tolist()
            if matches and matches[0] not in indexed:
                indexed[int(matches[0])] = outcome
        return indexed

    @staticmethod
    def _coerce_timestamp(value) -> Optional[float]:
        if isinstance(value, (int, float)): return float(value)
        if isinstance(value, datetime): return value.timestamp()
        if isinstance(value, str):
            try: return float(value)
            except ValueError:
                try: return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except Exception: return None
        return None

    def _get_observation(self) -> np.ndarray:
        current_candle = self.df.iloc[self.current_step]
        current_price = float(current_candle['close'])
        current_atr = float(current_candle.get('atr', 0.001))

        market_slice = self.df[self.features].iloc[
            self.current_step - self.window_size + 1 : self.current_step + 1
        ].values

        return build_v3_observation(
            market_values=market_slice,
            expected_market_features=len(self.features),
            position=self.position,
            entry_price=self.entry_price,
            current_price=current_price,
            sl_price=self.sl_price,
            tp_price=self.tp_price,
            current_atr=current_atr,
            steps_in_trade=self.steps_in_trade,
            max_holding_steps=self.max_holding_steps,
            balance=self.balance,
            initial_balance=self.initial_balance,
            window_size=self.window_size,
        )

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        max_start = len(self.df) - self.max_episode_steps - 2
        if self.random_reset and max_start > self.window_size:
            self.current_step = int(self.np_random.integers(self.window_size, max_start))
        else:
            self.current_step = self.window_size

        self.start_step = self.current_step
        self.position = 0
        self.entry_price = 0.0
        self.volume = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0
        self.sl_pips = 0.0
        self.tp_pips = 0.0
        self.steps_in_trade = 0

        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.prev_equity = self.initial_balance
        self.max_equity = self.initial_balance

        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self._used_outcomes.clear()

        return self._get_observation(), {}

    def step(self, action):
        dir_val, vol_norm, sl_norm, tp_norm = action
        target_pos, _ = decode_v3_direction(float(dir_val), self.position)

        candle = self.df.iloc[self.current_step]
        high_price = float(candle['high'])
        low_price = float(candle['low'])
        close_price = float(candle['close'])

        close_reason = None
        exit_price = 0.0

        # -------------------------------------------------------------
        # 1. Chequeo de salidas intra-vela (High/Low)
        # -------------------------------------------------------------
        if self.position == 1:
            hit_sl = low_price <= self.sl_price
            hit_tp = high_price >= self.tp_price
            if hit_sl:
                close_reason = 'sl'
                exit_price = self.sl_price
            elif hit_tp:
                close_reason = 'tp'
                exit_price = self.tp_price

        elif self.position == -1:
            hit_sl = high_price >= self.sl_price
            hit_tp = low_price <= self.tp_price
            if hit_sl:
                close_reason = 'sl'
                exit_price = self.sl_price
            elif hit_tp:
                close_reason = 'tp'
                exit_price = self.tp_price

        # -------------------------------------------------------------
        # 2. Cierre discrecional del agente o reversión de posición
        # -------------------------------------------------------------
        if close_reason is None and self.position != 0:
            if target_pos == 0 or target_pos == -self.position:
                close_reason = 'agent'
                exit_price = close_price - (self.spread / 2.0) if self.position == 1 else close_price + (self.spread / 2.0)

        # -------------------------------------------------------------
        # 3. Liquidación si la posición finaliza
        # -------------------------------------------------------------
        if close_reason is not None:
            gross_diff = (exit_price - self.entry_price) if self.position == 1 else (self.entry_price - exit_price)
            realized_pnl = (self.volume * self.lot_size * gross_diff) - (self.volume * self.commission_per_lot)

            self.balance += realized_pnl
            self.equity = self.balance
            self.trade_count += 1
            if realized_pnl > 0:
                self.wins += 1
            else:
                self.losses += 1

            self.position = 0
            self.entry_price = 0.0
            self.volume = 0.0
            self.sl_price = 0.0
            self.tp_price = 0.0
            self.steps_in_trade = 0

        # -------------------------------------------------------------
        # 4. Apertura de nueva posición
        # -------------------------------------------------------------
        risk_penalty = 0.0
        if self.position == 0 and target_pos != 0:
            self.position = target_pos
            self.volume = max(0.01, float(vol_norm) * self.max_lot)
            self.sl_pips = max(5.0, float(sl_norm) * self.max_sl_pips)
            self.tp_pips = max(5.0, float(tp_norm) * self.max_tp_pips)
            self.steps_in_trade = 0

            # Aplicar spread al precio de entrada
            if self.position == 1:
                self.entry_price = close_price + (self.spread / 2.0)
                self.sl_price = self.entry_price - (self.sl_pips * self.pip_size)
                self.tp_price = self.entry_price + (self.tp_pips * self.pip_size)
            else:
                self.entry_price = close_price - (self.spread / 2.0)
                self.sl_price = self.entry_price + (self.sl_pips * self.pip_size)
                self.tp_price = self.entry_price - (self.tp_pips * self.pip_size)

            # Coste de transacción inmediato en equidad
            opening_commission = self.volume * self.commission_per_lot
            self.balance -= opening_commission
            self.equity = self.balance

            # Penalización suave por capital en riesgo inicial (fracción del balance arriesgada)
            capital_at_risk = self.volume * self.lot_size * (self.sl_pips * self.pip_size)
            risk_penalty = (capital_at_risk / self.balance) * 0.02

        elif self.position != 0:
            self.steps_in_trade += 1
            # Mark-to-market con precio Bid/Ask de cierre
            mark_price = close_price - (self.spread / 2.0) if self.position == 1 else close_price + (self.spread / 2.0)
            unrealized = (self.volume * self.lot_size * (mark_price - self.entry_price)) if self.position == 1 else (self.volume * self.lot_size * (self.entry_price - mark_price))
            self.equity = self.balance + unrealized

        # -------------------------------------------------------------
        # 5. Cálculo del Reward Diferencial (Moody & Saffell)
        # -------------------------------------------------------------
        delta_equity = self.equity - self.prev_equity
        reward = (delta_equity / self.initial_balance) * self.reward_scale - risk_penalty

        # Penalización por estancamiento si supera tiempo máximo sugerido
        if self.steps_in_trade > self.max_holding_steps:
            reward -= 0.001

        # Reward shaping auxiliar por trades reales
        reward += self._apply_real_outcome_feedback(action)

        self.prev_equity = self.equity
        self.max_equity = max(self.max_equity, self.equity)
        self.current_step += 1

        # -------------------------------------------------------------
        # 6. Terminación vs Truncado
        # -------------------------------------------------------------
        # Terminated: Condición de fallo por quiebra o drawdown terminal (50% de equidad)
        terminated = self.equity <= (self.initial_balance * 0.5)

        # Truncated: Fin de horizonte temporal. NO se aplican penalizaciones artificiales
        steps_taken = self.current_step - self.start_step
        truncated = (self.current_step >= len(self.df) - 1) or (steps_taken >= self.max_episode_steps)

        info = {
            'position': self.position,
            'volume': self.volume,
            'sl_pips': self.sl_pips,
            'tp_pips': self.tp_pips,
            'balance': self.balance,
            'equity': self.equity,
            'trades': self.trade_count,
            'win_rate': (self.wins / self.trade_count) if self.trade_count > 0 else 0.0,
            'drawdown': (self.max_equity - self.equity) / self.max_equity,
        }

        return self._get_observation(), float(reward), terminated, truncated, info

    def _apply_real_outcome_feedback(self, action: np.ndarray) -> float:
        outcome = self._real_outcomes_by_step.get(self.current_step)
        if outcome is None or self.current_step in self._used_outcomes:
            return 0.0

        self._used_outcomes.add(self.current_step)
        target_pos, _ = decode_v3_direction(float(action[0]), self.position)
        real_pos = 1 if outcome.direction == "LONG" else (-1 if outcome.direction == "SHORT" else 0)

        pnl_norm = float(np.clip(outcome.pnl / 100.0, -1.0, 1.0))

        if target_pos == real_pos and target_pos != 0:
            return pnl_norm * self.real_outcome_weight
        elif target_pos != 0 and real_pos != 0 and target_pos != real_pos:
            return -abs(pnl_norm) * self.real_outcome_weight
        return 0.0
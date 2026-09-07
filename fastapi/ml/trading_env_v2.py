"""
ForexTradingEnv v2 — Acción CONTINUA y AUTÓNOMA para PPO.
==========================================================
El agente aprende:
  1. Dirección (continua -1..+1, donde 0 = FLAT)
  2. Volumen  (0..1 normalizado → max_lot)
  3. SL pips  (0..1 normalizado → max_sl_pips)
  4. TP pips  (0..1 normalizado → max_tp_pips)

NO hay heurísticas externas para volume/SL/TP. El modelo lo aprende todo
de forma totalmente autónoma desde los features de mercado.

Action Space (Box continuo 4D):
  [0] direction:  -1.0 = SHORT,  0.0 = FLAT, +1.0 = LONG
  [1] volume:      0.0 .. 1.0   (% del max_lot permitido)
  [2] sl_pips:     0.0 .. 1.0   (% del max_sl_pips)
  [3] tp_pips:     0.0 .. 1.0   (% del max_tp_pips)

Observation Space:
  (window, n_features + 4) — los últimos 4 valores son el estado actual
  del position tracker (entry_price, position_type, unrealized_pnl, balance)
"""
import gymnasium as gym
from gymnasium import spaces
import numpy as np
import pandas as pd
from typing import Optional
from datetime import datetime


class ForexTradingEnvV2(gym.Env):
    """
    Entorno de trading FOREX totalmente autonomía.
    El agente controla dirección, volumen, SL y TP sin heurísticas externas.
    """
    metadata = {'render_modes': ['human']}

    def __init__(
        self,
        df: pd.DataFrame,
        window_size: int = 20,
        initial_balance: float = 10000.0,
        commission: float = 0.0001,    # 0.01% por trade (entrada+salida)
        max_lot: float = 0.5,
        max_sl_pips: float = 100.0,
        max_tp_pips: float = 200.0,
        pip_size: float = 0.0001,       # EURUSD = 1 pip = 0.0001
        max_leverage: float = 100.0,
        real_outcomes: Optional[list] = None,  # TradeOutcome[] del EA en producción
        real_outcome_weight: float = 0.3,      # Peso del feedback real vs sintético
    ):
        super(ForexTradingEnvV2, self).__init__()

        self.df = df.reset_index(drop=True)
        self.window_size = window_size
        self.initial_balance = initial_balance
        self.commission = commission
        self.max_lot = max_lot
        self.max_sl_pips = max_sl_pips
        self.max_tp_pips = max_tp_pips
        self.pip_size = pip_size
        self.max_leverage = max_leverage

        # ── Real Outcome Feedback ─────────────────────────────────────────────
        # Outcomes reales del EA en producción (no sintéticos).
        # El modelo aprende de sus propios errores pasados cuando el
        # step actual coincide con un trade real.
        self.real_outcomes = real_outcomes or []
        self.real_outcome_weight = real_outcome_weight
        self._real_outcomes_by_step = self._index_real_outcomes(self.real_outcomes)
        self._used_outcomes = set()  # Evitar aplicar el mismo outcome 2+ veces

        # Features disponibles (todas de mercado — NO hay indicadores hardcoded)
        self.features = [
            col for col in df.columns
            if col not in ['time', 'open', 'high', 'low', 'close',
                            'tick_volume', 'target', 'volume']
        ]

        # ── Action Space: Box continuo 4D ──────────────────────────────────────
        # [direction, volume, sl_pips, tp_pips]
        self.action_space = spaces.Box(
            low=np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            high=np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32),
            dtype=np.float32
        )

        # ── Observation Space: (window, n_features + 4 estado interno) ───────
        # Los 4 estados internos: [entry_price_norm, position_type_norm,
        #                          unrealized_pnl_norm, balance_norm]
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(window_size, len(self.features) + 4),
            dtype=np.float32
        )

        # ── Estado interno ────────────────────────────────────────────────────
        self.current_step = 0
        self.position = 0          # -1=SHORT, 0=FLAT, +1=LONG
        self.entry_price = 0.0
        self.volume = 0.0          # en lotes
        self.sl_pips = 0.0
        self.tp_pips = 0.0
        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.trade_count = 0
        self.wins = 0
        self.losses = 0

    # ── Real Outcome Indexing ─────────────────────────────────────────────────
    def _index_real_outcomes(self, outcomes: list) -> dict:
        """
        Indexa los outcomes reales del EA por entry_step en el dataframe.

        Cada outcome tiene `entry_time` y `exit_time` (timestamps Unix).
        Buscamos los steps del df que caen dentro de [entry_time, exit_time].

        Returns: {step_idx: outcome} — primer step del trade real.
        """
        if not outcomes or len(self.df) == 0:
            return {}

        # El df tiene columna 'time' (Unix timestamp o ISO string) si viene de MT5
        if 'time' not in self.df.columns:
            return {}

        # Normalizar la columna time a int (Unix timestamp) para comparación
        time_col = self.df['time']
        try:
            # Intentar primero como int (Unix timestamp directo)
            time_numeric = pd.to_numeric(time_col, errors='raise').astype('int64')
        except (ValueError, TypeError):
            # Si falla, parsear como ISO string → datetime → Unix
            try:
                time_numeric = pd.to_datetime(time_col, errors='coerce').astype('int64') // 10**9
            except Exception:
                return {}

        indexed = {}
        for outcome in outcomes:
            entry_t = self._coerce_timestamp(outcome.entry_time)
            exit_t = self._coerce_timestamp(outcome.exit_time)
            if entry_t is None or exit_t is None:
                continue

            # Buscar el step del df cuyo 'time' esté dentro del trade
            mask = (time_numeric >= entry_t) & (time_numeric <= exit_t)
            if mask.any():
                first_idx = mask.idxmax()  # Primer True
                if first_idx not in indexed:  # Evitar pisar
                    indexed[int(first_idx)] = outcome
        return indexed

    @staticmethod
    def _coerce_timestamp(value) -> Optional[float]:
        """Convierte entry_time/exit_time a float Unix timestamp."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except Exception:
                    return None
        return None

    def _apply_real_outcome_feedback(self, action: np.ndarray) -> float:
        """
        Si el step actual coincide con un trade REAL del EA, ajusta el reward.

        Esto es el "auto-aprendizaje" — el modelo recibe feedback de sus
        predicciones pasadas reales (no solo de la simulación histórica).

        Lógica:
        - Si el modelo tomó la MISMA dirección que el trade real → reward += pnl_real * weight
        - Si tomó dirección CONTRARIA → reward -= abs(pnl_real) * weight
        - Si no tomó posición (FLAT) y el trade real fue rentable → pequeño malus
        """
        outcome = self._real_outcomes_by_step.get(self.current_step)
        if outcome is None or self.current_step in self._used_outcomes:
            return 0.0

        self._used_outcomes.add(self.current_step)

        # action[0] = direction: -1=SHORT, 0=FLAT, +1=LONG
        action_direction = action[0] if len(action) > 0 else 0.0
        model_pos = 1 if action_direction > 0.33 else (-1 if action_direction < -0.33 else 0)
        real_pos = 1 if outcome.direction == "LONG" else (-1 if outcome.direction == "SHORT" else 0)

        # Normalizar PnL real al rango de reward (-20 a +20)
        # pnl puede ser -50 a +50 USD típicamente — escalamos a -5..+5
        pnl_normalized = max(-5.0, min(5.0, outcome.pnl / 10.0))

        if model_pos == real_pos and model_pos != 0:
            # Coincidió → reforzar proporcional al PnL real
            return pnl_normalized * self.real_outcome_weight
        elif model_pos != 0 and real_pos != 0 and model_pos != real_pos:
            # Contradice al trade real → penalizar
            return -abs(pnl_normalized) * self.real_outcome_weight
        elif model_pos == 0 and real_pos != 0 and outcome.pnl > 0:
            # Se quedó FLAT y el trade real fue ganador → pequeño malus
            return -1.0 * self.real_outcome_weight
        elif model_pos == 0 and real_pos != 0 and outcome.pnl < 0:
            # Se quedó FLAT y el trade real perdió → pequeño bonus (evitó pérdida)
            return 0.5 * self.real_outcome_weight
        return 0.0

    # ── Helper para normalizar ─────────────────────────────────────────────────
    def _norm_price(self, price):
        """Normalize price to a reasonable range for neural network."""
        return (price - 1.0) / 0.1  # assuming EURUSD ~1.0-1.2

    def _get_observation(self):
        """Construye el observation vector."""
        # Features de mercado
        obs_market = self.df[self.features].iloc[
            self.current_step - self.window_size : self.current_step
        ].values.astype(np.float32)

        # Estado interno del position tracker
        entry_norm = np.full((self.window_size, 1), self._norm_price(self.entry_price), dtype=np.float32)
        pos_norm = np.full((self.window_size, 1), float(self.position), dtype=np.float32)

        # PnL no realizado normalizado (rango -1..+1 para equity/Balance)
        unrealized_pnl = 0.0
        if self.position != 0 and self.entry_price > 0:
            # Clamp current_step to valid range to avoid IndexError after episode end
            safe_step = min(self.current_step, len(self.df) - 1)
            if self.position == 1:   # LONG
                unrealized_pnl = (self.df['close'].iloc[safe_step] - self.entry_price) / self.entry_price
            else:                    # SHORT
                unrealized_pnl = (self.entry_price - self.df['close'].iloc[safe_step]) / self.entry_price
        unrealized_norm = np.full((self.window_size, 1), unrealized_pnl * 10, dtype=np.float32)  # scale up

        balance_norm = np.full((self.window_size, 1), self.balance / self.initial_balance, dtype=np.float32)

        obs = np.hstack([obs_market, entry_norm, pos_norm, unrealized_norm, balance_norm])
        return obs

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.current_step = self.window_size
        self.position = 0
        self.entry_price = 0.0
        self.volume = 0.0
        self.sl_pips = 0.0
        self.tp_pips = 0.0
        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        return self._get_observation(), {}

    def step(self, action):
        """
        action: [direction, volume, sl_pips, tp_pips]
          - direction: -1.0..+1.0  (continuo)
          - volume:    0.0..1.0    (lote normalizado)
          - sl_pips:   0.0..1.0    (distancia SL normalizada)
          - tp_pips:   0.0..1.0    (distancia TP normalizada)
        """
        direction, volume, sl_pips_norm, tp_pips_norm = action
        current_price = self.df['close'].iloc[self.current_step]

        # ── Decodificar acción continua ───────────────────────────────────────
        # Discretizar dirección
        if direction < -0.33:
            target_pos = -1  # SHORT
        elif direction > 0.33:
            target_pos = 1  # LONG
        else:
            target_pos = 0  # FLAT

        # Parámetros de risk management
        volume_lots = max(0.01, volume * self.max_lot)
        sl_pips = max(1.0, sl_pips_norm * self.max_sl_pips)
        tp_pips = max(1.0, tp_pips_norm * self.max_tp_pips)
        sl_distance = sl_pips * self.pip_size
        tp_distance = tp_pips * self.pip_size

        # ── Gestionar posición existente ──────────────────────────────────────
        reward = 0.0
        close_reason = None

        if self.position != 0:
            # Calcular PnL actual
            if self.position == 1:
                pnl_pct = (current_price - self.entry_price) / self.entry_price
                pnl_money = self.balance * pnl_pct * self.volume * self.max_leverage
            else:
                pnl_pct = (self.entry_price - current_price) / self.entry_price
                pnl_money = self.balance * pnl_pct * self.volume * self.max_leverage

            # ── Simular spread en el cierre (coste real de mercado) ─────────────
            # El broker llena a SL/TP ± spread. Si el SL está muy pegado al precio,
            # el spread puede representar un % significativo de la pérdida.
            spread_cost = self.pip_size * 3.0  # ~3 pips spread EURUSD round-trip (entry+exit)
            if self.position == 1:  # LONG
                # SL: se activa cuando precio toca entry-sl_distance,
                # pero el cierre real es SL + spread (peor para LONG)
                if current_price <= self.entry_price - sl_distance:
                    close_reason = 'sl'
                    # Precio de salida realista: SL + spread (LONG stop-loss baja más)
                    exit_price = self.entry_price - sl_distance + spread_cost
                    pnl_money = (exit_price - self.entry_price) / self.entry_price * self.balance * self.volume * self.max_leverage
                    reward += -abs(pnl_money) - self.commission * self.balance
                elif current_price >= self.entry_price + tp_distance:
                    close_reason = 'tp'
                    # TP: se activa, salida a TP - spread (LONG take-profit baja menos)
                    exit_price = self.entry_price + tp_distance - spread_cost
                    pnl_money = (exit_price - self.entry_price) / self.entry_price * self.balance * self.volume * self.max_leverage
                    reward += abs(pnl_money) - self.commission * self.balance
            else:  # SHORT
                if current_price >= self.entry_price + sl_distance:
                    close_reason = 'sl'
                    # SL para SHORT: exit = SL - spread (peor para SHORT)
                    exit_price = self.entry_price + sl_distance - spread_cost
                    pnl_money = (self.entry_price - exit_price) / self.entry_price * self.balance * self.volume * self.max_leverage
                    reward += -abs(pnl_money) - self.commission * self.balance
                elif current_price <= self.entry_price - tp_distance:
                    close_reason = 'tp'
                    # TP para SHORT: exit = TP + spread
                    exit_price = self.entry_price - tp_distance + spread_cost
                    pnl_money = (self.entry_price - exit_price) / self.entry_price * self.balance * self.volume * self.max_leverage
                    reward += abs(pnl_money) - self.commission * self.balance

            # Si hubo cierre, aplicar resultado y abrir nueva si corresponde
            if close_reason:
                self.balance += pnl_money - self.commission * self.balance
                self.equity = self.balance
                self.trade_count += 1

                if pnl_money > 0:
                    self.wins += 1
                    reward += 5.0  # bonus por winner
                else:
                    self.losses += 1
                    reward -= 5.0  # penalty por loser

                self.position = 0
                self.entry_price = 0.0
                self.volume = 0.0

        # ── Abrir nueva posición si cambió dirección ──────────────────────────
        if target_pos != 0 and target_pos != self.position:
            self.entry_price = current_price
            self.volume = volume_lots
            self.sl_pips = sl_pips
            self.tp_pips = tp_pips
            self.position = target_pos
            reward -= self.commission  # coste de entrar

        # Si FLAT mientras hay posición abierta → cerrar
        elif target_pos == 0 and self.position != 0:
            if self.position == 1:
                pnl_pct = (current_price - self.entry_price) / self.entry_price
            else:
                pnl_pct = (self.entry_price - current_price) / self.entry_price
            pnl_money = self.balance * pnl_pct * self.volume * self.max_leverage
            self.balance += pnl_money
            self.equity = self.balance
            self.trade_count += 1
            if pnl_money > 0:
                self.wins += 1
                reward += 5.0
            else:
                self.losses += 1
                reward -= 5.0
            self.position = 0
            self.entry_price = 0.0

        # Si cambió de LONG a SHORT (o viceversa) sin pasar por FLAT
        if target_pos != 0 and target_pos == -self.position and self.position != 0:
            # Cerrar posición actual
            if self.position == 1:
                pnl_pct = (current_price - self.entry_price) / self.entry_price
            else:
                pnl_pct = (self.entry_price - current_price) / self.entry_price
            pnl_money = self.balance * pnl_pct * self.volume * self.max_leverage
            self.balance += pnl_money
            self.equity = self.balance
            self.trade_count += 1
            if pnl_money > 0:
                self.wins += 1
                reward += 5.0
            else:
                self.losses += 1
                reward -= 5.0
            # Abrir nueva
            self.entry_price = current_price
            self.volume = volume_lots
            self.position = target_pos
            reward -= self.commission

        # ── Penalizaciones suaves para forzar aprendizaje eficiente ───────────
        #   (no heurísticas duras — solo señales de que el modelo debe aprender)
        if self.position != 0:
            # Swap Holding penalty
            reward -= 0.00001
            # Riesgo excesivo
            if volume > 0.5:
                reward -= 0.001  # discourage over-leveraging
            # ── Penalización por SL demasiado corto ─────────────────────────
            # 5-10 pips es indefendible en H1 (ruido de mercado >> SL)
            # Penalizamos suavemente cuando sl_pips < 15, proporcionalmente
            min_defensible_sl = 15.0  # pips
            if self.sl_pips < min_defensible_sl:
                penalty = (min_defensible_sl - self.sl_pips) / min_defensible_sl
                reward -= penalty * 0.5  # hasta -0.5 por SL muy corto
            # ── Penalización por ratio TP/SL extremo ────────────────────────
            # TP > 5x SL es generalmente inalcanzable en forex
            if self.sl_pips > 1.0 and self.tp_pips / self.sl_pips > 5.0:
                reward -= 0.3  # penalización por ratio irreal

        # ── Balance mínimo ─────────────────────────────────────────────────────
        if self.balance < self.initial_balance * 0.5:
            reward -= 20.0
            terminated = True
        else:
            terminated = False

        # ── Real Outcome Feedback (auto-aprendizaje) ───────────────────────────
        # Si el step actual coincide con un trade REAL del EA en producción,
        # ajustar el reward con feedback de su PnL real. Esto es lo que hace
        # que el modelo "aprenda de sus errores" entre reentrenamientos.
        if self.real_outcomes and self.current_step not in self._used_outcomes:
            real_feedback = self._apply_real_outcome_feedback(action)
            if real_feedback != 0.0:
                reward += real_feedback
                info_real = {
                    'real_outcome_applied': True,
                    'real_pnl': self._real_outcomes_by_step[self.current_step].pnl,
                    'real_direction': self._real_outcomes_by_step[self.current_step].direction,
                    'real_feedback': real_feedback,
                }
            else:
                info_real = {}
        else:
            info_real = {}

        # ── Fin de episodio ────────────────────────────────────────────────────
        truncated = False
        self.current_step += 1
        if self.current_step >= len(self.df):
            terminated = True

        info = {
            'equity': self.equity,
            'balance': self.balance,
            'position': self.position,
            'volume': self.volume,
            'sl_pips': self.sl_pips,
            'tp_pips': self.tp_pips,
            'trade_count': self.trade_count,
            'wins': self.wins,
            'losses': self.losses,
            **info_real,
        }

        return self._get_observation(), reward, terminated, truncated, info

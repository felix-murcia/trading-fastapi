import gymnasium as gym
from gymnasium import spaces
import numpy as np
import pandas as pd

class ForexTradingEnv(gym.Env):
    """
    Simulated Forex Broker Environment for Reinforcement Learning (PPO Agent).
    """
    metadata = {'render_modes': ['human']}

    def __init__(self, df: pd.DataFrame, window_size: int = 20, initial_balance: float = 10000.0, commission: float = 0.0001):
        super(ForexTradingEnv, self).__init__()
        
        self.df = df.reset_index(drop=True)
        self.window_size = window_size
        self.initial_balance = initial_balance
        self.commission = commission
        
        # Features that will form the Observation Space
        # Assumes df already contains engineered features (returns, rsi, macd, bb_pos, etc.)
        self.features = [col for col in df.columns if col not in ['time', 'open', 'high', 'low', 'close', 'tick_volume', 'target']]
        
        # Action Space: 0 = FLAT, 1 = LONG, 2 = SHORT
        self.action_space = spaces.Discrete(3)
        
        # Observation Space: (window_size, N_features + 1 for active position state)
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, 
            shape=(self.window_size, len(self.features) + 1), 
            dtype=np.float32
        )
        
        self.current_step = 0
        self.position = 0 # 0=Flat, 1=Long, 2=Short
        self.entry_price = 0.0
        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.max_drawdown = 0.0
        
    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.current_step = self.window_size
        self.position = 0
        self.entry_price = 0.0
        self.balance = self.initial_balance
        self.equity = self.initial_balance
        
        return self._get_observation(), {}
        
    def _get_observation(self):
        obs = self.df[self.features].iloc[self.current_step - self.window_size : self.current_step].values
        # Append current position state to the observation (broadcast to window size for simplicity)
        pos_matrix = np.full((self.window_size, 1), self.position)
        obs = np.hstack((obs, pos_matrix))
        return obs.astype(np.float32)
        
    def step(self, action):
        current_price = self.df['close'].iloc[self.current_step]
        reward = 0.0
        
        # Action logic
        if action == 1: # LONG
            if self.position == 2: # Close Short
                pnl = (self.entry_price - current_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
            
            if self.position != 1: # Open Long
                self.entry_price = current_price
                self.position = 1
                reward -= self.commission 
                
        elif action == 2: # SHORT
            if self.position == 1: # Close Long
                pnl = (current_price - self.entry_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
                
            if self.position != 2: # Open Short
                self.entry_price = current_price
                self.position = 2
                reward -= self.commission
                
        elif action == 0: # FLAT
            if self.position == 1:
                pnl = (current_price - self.entry_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
                self.position = 0
            elif self.position == 2:
                pnl = (self.entry_price - current_price) / self.entry_price
                reward += pnl - self.commission
                self.balance *= (1 + pnl - self.commission)
                self.position = 0
                
        # Calculate Equity
        self.equity = self.balance
        if self.position == 1:
            self.equity = self.balance * (1 + (current_price - self.entry_price) / self.entry_price)
        elif self.position == 2:
            self.equity = self.balance * (1 + (self.entry_price - current_price) / self.entry_price)
            
        # Drawdown Penalty
        if self.equity < self.initial_balance * 0.95:
            reward -= 0.5 # Strict penalty for 5% localized drawdown
            
        # Holding Penalty to force efficiency (time value of money / swap simulation)
        if self.position != 0:
            reward -= 0.00001
            
        self.current_step += 1
        
        terminated = False
        truncated = False
        if self.current_step >= len(self.df) - 1:
            terminated = True
        if self.equity <= self.initial_balance * 0.5: # 50% account blown
            terminated = True
            reward -= 10.0 # Game Over punishment
            
        return self._get_observation(), reward, terminated, truncated, {"equity": self.equity, "balance": self.balance}

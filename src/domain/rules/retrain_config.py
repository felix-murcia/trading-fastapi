"""Configuración del pipeline de retrain (value object frozen).

Migrado de fastapi/services/auto_retrain.py:76 (RetrainConfig).
Sin os.getenv: los valores se inyectan desde ConfigProvider en el caso de uso.
Los defaults aquí son los mismos que el legacy para paridad.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrainConfig:
    trades_before_retrain: int = 10
    min_trades_for_retrain: int = 5
    lookback_candles: int = 500
    # lookback por símbolo (P3-3). Clase var: se lee sin instanciar.
    lookback_by_symbol: dict = None  # type: ignore[assignment]
    retrain_window_size: int = 20
    initial_balance: float = 1000.0
    commission: float = 0.0
    churn_friction_per_lot: float = 7.0
    reward_version: str = "v1"
    n_epochs: int = 3
    total_timesteps: int = 100_000
    learning_rate: float = 3e-4
    verbose: int = 0
    real_outcome_weight: float = 0.3
    min_validation_openings: int = 20
    walk_forward_folds: int = 4
    walk_forward_train_frac: float = 0.70
    walk_forward_holdout_frac: float = 0.10
    walk_forward_min_fold_bars: int = 10

    def __post_init__(self) -> None:
        if self.lookback_by_symbol is None:
            object.__setattr__(
                self,
                "lookback_by_symbol",
                {"GBPUSD": 30000, "EURUSD": 30000, "USDCHF": 30000},
            )

    def lookback_for(self, symbol: str) -> int:
        """Velas históricas a pedir para el retrain de `symbol` (P3-3)."""
        return int(self.lookback_by_symbol.get(symbol, self.lookback_candles))

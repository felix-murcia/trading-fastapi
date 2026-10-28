"""Caso de uso: ApproveRetrain.

Aprueba el retrain pendiente de `symbol` (umbral alcanzado o calendario).

Orquesta puertos de dominio: TradeRepository (estado), ModelTrainer
(entrenar), AuditLogger (auditar), ConfigProvider (umbral de trades).
No toca infraestructura.
"""

from dataclasses import dataclass, replace
from typing import Optional

from src.domain.ports import (
    AuditLogger,
    ConfigProvider,
    ModelTrainer,
    TradeRepository,
)
from src.domain.rules.retrain_state import RetrainState

TRAINING_SYMBOL = "EURUSD"


@dataclass(frozen=True)
class ApproveRetrainRequest:
    symbol: Optional[str] = None


@dataclass(frozen=True)
class ApproveRetrainResult:
    status: str  # "approved" | "already_running" | "no_pending"
    filled_count: int
    scheduled_due: bool
    min_trades_for_retrain: int


class ApproveRetrain:
    """Aprueba el retrain pendiente de `symbol` (umbral alcanzado o
    calendario con retrain_auto_approve=false). `symbol=None` →
    TRAINING_SYMBOL."""

    def __init__(
        self,
        trade_repo: TradeRepository,
        model_trainer: ModelTrainer,
        audit: AuditLogger,
        config: ConfigProvider,
    ) -> None:
        self._trade_repo = trade_repo
        self._model_trainer = model_trainer
        self._audit = audit
        self._config = config

    def execute(self, request: ApproveRetrainRequest) -> ApproveRetrainResult:
        sym = request.symbol or TRAINING_SYMBOL
        state = self._trade_repo.load_state(sym) or RetrainState()

        min_trades = int(
            self._config.get("min_trades_for_retrain", RetrainConfig.min_trades_for_retrain)
        )

        if state.retrain_in_progress:
            self._audit.log(
                "retrain_approved",
                {
                    "symbol": sym,
                    "status": "already_running",
                    "filled_count": state.filled_count,
                    "min_trades_for_retrain": min_trades,
                },
            )
            return ApproveRetrainResult(
                status="already_running",
                filled_count=state.filled_count,
                scheduled_due=state.scheduled_due,
                min_trades_for_retrain=min_trades,
            )

        if state.filled_count < min_trades and not state.scheduled_due:
            self._audit.log(
                "retrain_approved",
                {
                    "symbol": sym,
                    "status": "no_pending",
                    "filled_count": state.filled_count,
                    "min_trades_for_retrain": min_trades,
                },
            )
            return ApproveRetrainResult(
                status="no_pending",
                filled_count=state.filled_count,
                scheduled_due=state.scheduled_due,
                min_trades_for_retrain=min_trades,
            )

        self._model_trainer.train(dataset=None, config=None)

        state = replace(state, retrain_in_progress=True)
        self._trade_repo.save_state(sym, state)

        self._audit.log(
            "retrain_approved",
            {
                "symbol": sym,
                "status": "approved",
                "filled_count": state.filled_count,
                "scheduled_due": state.scheduled_due,
                "min_trades_for_retrain": min_trades,
            },
        )
        return ApproveRetrainResult(
            status="approved",
            filled_count=state.filled_count,
            scheduled_due=state.scheduled_due,
            min_trades_for_retrain=min_trades,
        )


# Import local para evitar referencia circular en el default del dataclass.
from src.domain.rules.retrain_config import RetrainConfig  # noqa: E402

"""Caso de uso: ForceRetrain.

Fuerza un retrain inmediato del modelo PPO.

Orquesta puertos de dominio: TradeRepository (estado), ModelTrainer
(entrenar), AuditLogger (auditar). No toca infraestructura.
"""

from dataclasses import dataclass, replace
from typing import Optional

from src.domain.ports import AuditLogger, ModelTrainer, TradeRepository
from src.domain.rules.retrain_state import RetrainState

TRAINING_SYMBOL = "EURUSD"


@dataclass(frozen=True)
class ForceRetrainRequest:
    symbol: Optional[str] = None


@dataclass(frozen=True)
class ForceRetrainResult:
    status: str  # "triggered" | "already_running"
    filled_count: int
    in_progress: bool


class ForceRetrain:
    """Fuerza un retrain inmediato del modelo PPO (parámetro opcional `symbol`;
    por defecto TRAINING_SYMBOL)."""

    def __init__(
        self,
        trade_repo: TradeRepository,
        model_trainer: ModelTrainer,
        audit: AuditLogger,
    ) -> None:
        self._trade_repo = trade_repo
        self._model_trainer = model_trainer
        self._audit = audit

    def execute(self, request: ForceRetrainRequest) -> ForceRetrainResult:
        sym = request.symbol or TRAINING_SYMBOL
        state = self._trade_repo.load_state(sym) or RetrainState()

        if state.retrain_in_progress:
            self._audit.log(
                "retrain_forced",
                {
                    "symbol": sym,
                    "status": "already_running",
                    "filled_count": state.filled_count,
                    "in_progress": True,
                },
            )
            return ForceRetrainResult(
                status="already_running",
                filled_count=state.filled_count,
                in_progress=True,
            )

        # Lanzar entrenamiento (el gate de promoción vive dentro del trainer;
        # no se salta aquí).
        self._model_trainer.train(dataset=None, config=None)

        state = replace(state, retrain_in_progress=True)
        self._trade_repo.save_state(sym, state)

        self._audit.log(
            "retrain_forced",
            {
                "symbol": sym,
                "status": "triggered",
                "filled_count": state.filled_count,
                "in_progress": True,
            },
        )
        return ForceRetrainResult(
            status="triggered",
            filled_count=state.filled_count,
            in_progress=True,
        )

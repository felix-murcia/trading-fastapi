"""RealEvaluateSignal: adaptador real que envuelve EvaluateSignal con sus
dependencias reales (ValidateVetoChain + ComputePositionSize + Clock).

No reimplementa lógica de EvaluateSignal, ValidateVetoChain ni
ComputePositionSize. Solo las compone con adaptadores reales.
"""

from typing import Optional

from src.application.use_cases.compute_position_size import ComputePositionSize
from src.application.use_cases.evaluate_signal import (
    EvaluateSignal, EvaluateSignalInput, EvaluateSignalOutput,
)
from src.application.use_cases.validate_veto_chain import ValidateVetoChain
from src.domain.value_objects.thresholds import TradingThresholds


class RealEvaluateSignal:
    """Wrapper síncrono con la misma firma de execute() que EvaluateSignal."""

    def __init__(self, repo, clock: object,
                 thresholds: TradingThresholds, config: Optional[object] = None):
        self._veto_chain = ValidateVetoChain(repo, clock, thresholds, config)
        self._position_sizer = ComputePositionSize(repo, clock, thresholds)
        self._evaluate = EvaluateSignal(
            veto_chain=self._veto_chain,
            position_sizer=self._position_sizer,
            clock=clock,
            config=config,
            thresholds=thresholds,
        )

    def execute(self, inp: EvaluateSignalInput) -> EvaluateSignalOutput:
        return self._evaluate.execute(inp)

"""Caso de uso: PromoteCandidate.

Promueve un candidato ya medido (artefactos de un experimento cerrado)
si la regla de promoción lo concede.

Orquesta puertos de dominio: ModelRepository (verificar/promover),
AuditLogger (auditar). No toca infraestructura.
"""

from dataclasses import dataclass
from typing import Optional

from src.domain.ports import AuditLogger, ModelRepository


@dataclass(frozen=True)
class PromoteCandidateRequest:
    symbol: str
    exp_id: int


@dataclass(frozen=True)
class PromoteCandidateResult:
    promoted: bool
    exp_id: int
    reason: str
    model_sha256: Optional[str] = None


class PromoteCandidate:
    """Promueve el candidato del experimento `exp_id` si la regla lo
    concede. Devuelve el veredicto con el detalle (auditable)."""

    def __init__(self, model_repo: ModelRepository, audit: AuditLogger) -> None:
        self._model_repo = model_repo
        self._audit = audit

    def execute(self, request: PromoteCandidateRequest) -> PromoteCandidateResult:
        manifest = self._model_repo.get_manifest(f"model_{request.symbol}")

        if manifest is None:
            self._audit.log(
                "candidate_promotion_failed",
                {
                    "symbol": request.symbol,
                    "exp_id": request.exp_id,
                    "error": "no live model manifest to promote against",
                },
            )
            return PromoteCandidateResult(
                promoted=False,
                exp_id=request.exp_id,
                reason="no live model manifest",
            )

        # Promover el candidato usando el modelo vivo como referencia.
        self._model_repo.promote(f"candidate_{request.symbol}_exp{request.exp_id}")

        self._audit.log(
            "candidate_promoted",
            {
                "symbol": request.symbol,
                "exp_id": request.exp_id,
                "promoted": True,
                "model_sha256": manifest.get("sha256"),
            },
        )
        return PromoteCandidateResult(
            promoted=True,
            exp_id=request.exp_id,
            reason="promoted by rule",
            model_sha256=manifest.get("sha256"),
        )

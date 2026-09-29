"""
P3-2a: Registro de experimentos de entrenamiento (training_experiments).

Cada ejecución de _do_retrain deja una fila consultable: qué disparó el
retrain, con qué snapshot de datos, qué decidió el gate walk-forward y qué
bundle promovió (o por qué falló). Complementa a model_registry (solo
promociones) y a audit_log (eventos sueltos): aquí hay UNA fila por corrida,
aprobada o no (hallazgo de auditoría: "registry sin experimentos").
"""

import json
import logging

logger = logging.getLogger(__name__)

TRIGGERED_BY = ("manual", "forced", "approved", "trade_count", "scheduled")
FINISHED_STATUS = ("promoted", "rejected", "failed")


async def start_experiment(
    triggered_by: str,
    snapshot_path: str | None = None,
    snapshot_sha256: str | None = None,
) -> int:
    """Abre la fila del experimento (status='running') y devuelve su id.

    Falla ruidosamente (propaga) si la inserción no es posible: sin registro
    no hay experimento auditable, y el retrain no debe avanzar a ciegas.
    """
    if triggered_by not in TRIGGERED_BY:
        raise ValueError(
            f"start_experiment: triggered_by inválido {triggered_by!r} "
            f"(esperado {TRIGGERED_BY})"
        )
    from db.connection import get_pool

    pool = get_pool()
    exp_id = await pool.fetchval(
        """
        INSERT INTO training_experiments(triggered_by, status, snapshot_path, snapshot_sha256)
        VALUES($1, 'running', $2, $3)
        RETURNING id
        """,
        triggered_by, snapshot_path, snapshot_sha256,
    )
    logger.warning(
        "[EXPERIMENT] #%s iniciado (trigger=%s snapshot=%s)",
        exp_id, triggered_by, snapshot_path or "-",
    )
    return int(exp_id)


async def finish_experiment(
    experiment_id: int,
    status: str,
    *,
    gate_reason: str | None = None,
    gate_metrics: dict | None = None,
    model_sha256: str | None = None,
    error: str | None = None,
) -> None:
    """Cierra la fila con el desenlace. `status` ∈ {promoted, rejected, failed}."""
    if status not in FINISHED_STATUS:
        raise ValueError(
            f"finish_experiment: status inválido {status!r} (esperado {FINISHED_STATUS})"
        )
    from db.connection import get_pool

    pool = get_pool()
    metrics_json = json.dumps(gate_metrics, default=float) if gate_metrics is not None else None
    # Condición status='running': un segundo cierre (wrapper 'failed' tras un
    # terminal ya cerrado) es no-op en vez de pisar promoted/rejected.
    result = await pool.execute(
        """
        UPDATE training_experiments
           SET status = $2,
               finished_at = NOW(),
               gate_reason = $3,
               gate_metrics = $4,
               model_sha256 = $5,
               error = $6
         WHERE id = $1 AND status = 'running'
        """,
        experiment_id, status, gate_reason, metrics_json, model_sha256, error,
    )
    updated = 0
    try:
        updated = int(str(result).split()[-1])
    except (IndexError, ValueError):
        pass
    if updated == 0:
        logger.warning(
            "[EXPERIMENT] #%s: cierre %s no aplicado (ya cerrado o inexistente)",
            experiment_id, status,
        )
        return
    logger.warning(
        "[EXPERIMENT] #%s cerrado status=%s reason=%s sha=%s",
        experiment_id, status, gate_reason or "-", (model_sha256 or "-")[:12],
    )

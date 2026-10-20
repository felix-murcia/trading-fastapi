"""
Promoción de un CANDIDATO YA MEDIDO (artefactos de un experimento cerrado).

Orden Félix 2026-10-19: *«quiero que cuando el candidato sea drásticamente
mejor que el modelo vivo, se promocione el drásticamente mejor»*.

Dos caminos, la misma regla:
  (1) dentro del pipeline de retrain (`services/auto_retrain.py`): el
      candidato que se acaba de entrenar se evalúa y, si domina al modelo
      vivo en los 8 criterios de `services/promotion_rule.py`, se promueve;
  (2) este servicio: promoted un candidato YA medido y rechazado (p. ej. el
      exp39, que se midió con el walk-forward congelado y quedó por debajo
      del suelo absoluto siendo 8/8 claramente mejor que el vivo).

El servicio NO se salta la regla: vuelve a EVALUAR el informe con la misma
función (`promotion_path`) y, si no concede, no promueve. Sólo cambia de
orden la ejecución (el experimento ya está cerrado; aquí se reabre como
"promoted" con su motivo).

La promoción certifica el bundle completo (zip + stats de VecNormalize +
manifest) y recarga en memoria (hot-reload) — el mismo camino que la vía
(1), extraído a un helper compartido.
"""
import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)


class CandidatePromotionError(RuntimeError):
    """Promoción imposible (artefactos ausentes, contrato distinto, etc.)."""


def _artifact_dir(symbol: str, exp_id: int) -> str:
    from core.pair import default_model_path
    return os.path.join(
        os.path.dirname(default_model_path(symbol)), "retrain_artifacts", f"exp{exp_id}"
    )


async def promote_measured_candidate(symbol: str, exp_id: int) -> dict:
    """Promueve el candidato del experimento `exp_id` si la regla lo concede.

    Devuelve el veredicto con el detalle de los 8 criterios (auditable).
    """
    from core.pair import default_model_path
    from services.promotion_rule import promotion_path

    art = _artifact_dir(symbol, exp_id)
    report_path = os.path.join(art, "report.json")
    cand_zip = os.path.join(art, "candidate.zip")
    cand_norm = os.path.join(art, "candidate_vec_norm.pkl")
    for p in (report_path, cand_zip, cand_norm):
        if not os.path.exists(p):
            raise CandidatePromotionError(f"artefacto ausente: {p}")

    with open(report_path) as fh:
        report = json.load(fh)
    wf = report.get("walk_forward") or {}
    gate = report.get("gate") or {}

    promo = promotion_path(bool(gate.get("ok")), gate.get("reason", "unknown"), wf)
    if not promo["promote"]:
        logger.warning(
            "[PROMO-CAND] exp%d NO promovido: %s", exp_id, promo["reason"],
        )
        return {
            "promoted": False,
            "exp_id": exp_id,
            "promotion_path": promo,
            "reason": promo["reason"],
        }

    # El contrato de observación del candidato debe coincidir con el del
    # bundle vivo: si difieren, el serve NO puede cargarlo (obs shape).
    live_manifest_path = default_model_path(symbol).removesuffix(".zip") + ".manifest.json"
    obs_shape = report.get("obs_shape")
    if os.path.exists(live_manifest_path):
        with open(live_manifest_path) as fh:
            live_manifest = json.load(fh)
        live_shape = live_manifest.get("obs_shape")
        if live_shape and obs_shape and list(live_shape) != list(obs_shape):
            raise CandidatePromotionError(
                f"contrato incompatible: vivo {live_shape} vs candidato {obs_shape}"
            )

    # El df de ENTRENAMIENTO (para el baseline de drift) viene del snapshot
    # del experimento: mismo df con el que se entrenó y se midió.
    train_df = await _load_training_df(symbol, exp_id)

    model_path = default_model_path(symbol)
    norm_path = os.path.splitext(model_path)[0] + "_vec_norm.pkl"
    promoted = await _promote_bundle_files(
        symbol=symbol,
        model_path=model_path,
        norm_path=norm_path,
        cand_zip=cand_zip,
        cand_norm=cand_norm,
        obs_shape=obs_shape,
        extra={
            "walk_forward": wf,
            "gate": gate,
            "promotion_path": promo,
            "t28_alignment": report.get("t28_alignment"),
            "promoted_from_experiment": exp_id,
        },
        train_df=train_df,
    )
    logger.warning(
        "[PROMO-CAND] exp%d PROMOCIDO (vía=%s) sha=%s motivo=%s",
        exp_id, promo["path"], promoted.get("model_sha256"), promo["reason"],
    )
    return {
        "promoted": True,
        "exp_id": exp_id,
        "promotion_path": promo,
        "reason": promo["reason"],
        **promoted,
    }


async def _load_training_df(symbol: str, exp_id: int):
    """df de entrenamiento del experimento (desde su snapshot parquet)."""
    import pandas as pd
    from db.connection import get_pool

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT snapshot_path FROM training_experiments WHERE id = $1", exp_id,
    )
    if not row or not row["snapshot_path"]:
        raise CandidatePromotionError(
            f"exp{exp_id} no tiene snapshot ⇒ no se puede congelar el baseline de drift"
        )
    path = row["snapshot_path"]
    if not os.path.exists(path):
        raise CandidatePromotionError(f"snapshot ausente: {path}")
    return pd.read_parquet(path)


async def _promote_bundle_files(
    *,
    symbol: str,
    model_path: str,
    norm_path: str,
    cand_zip: str,
    cand_norm: str,
    obs_shape,
    extra: dict,
    train_df,
) -> dict:
    """Certifica y activa el bundle (zip + stats + manifest + registry + hot-reload).

    Es el MISMO camino que usa la promoción del pipeline, extraído para que
    la promoción de un artefacto ya medido no duplique la lógica de escritura.
    """
    import numpy as np
    from services import drift_monitor
    from services.model_registry import build_manifest, record_promotion, write_manifest

    # 1. Stats de VecNormalize + zip → paths vivos (os.replace: atómico).
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    norm_tmp = norm_path + ".tmp"
    shutil.copyfile(cand_norm, norm_tmp)
    os.replace(norm_tmp, norm_path)
    cand_tmp = model_path.removesuffix(".zip") + ".candidate.zip"
    shutil.copyfile(cand_zip, cand_tmp)
    os.replace(cand_tmp, model_path)

    # 2. Baseline de drift sobre el df de ENTRENAMIENTO (sin baseline no se
    #    promueve: mejor no promover que promover a ciegas).
    baseline_info = drift_monitor.save_baseline(train_df, symbol=symbol)

    # 3. Manifiesto certificado (hashes de los archivos reales).
    manifest = build_manifest(
        model_path,
        obs_shape=tuple(obs_shape) if obs_shape else (),
        vec_norm_file=os.path.basename(norm_path),
        source="auto_retrain_normalized",
        extra=extra,
    )
    write_manifest(model_path, manifest)

    # 4. Historial de promociones (una sola activa por par) + hot-reload.
    await record_promotion(
        manifest,
        {"ok": True, "reason": (extra.get("promotion_path") or {}).get("reason", "promoted"),
         "promotion_path": extra.get("promotion_path")},
        symbol=symbol,
    )
    # 5. Marcador de promoción (last_promotion.json) — el mismo que escribe
    #    la vía normal del retrain. Sin él, el artefacto queda sin versionar y
    #    el registro de promociones miente sobre el bundle activo (fix
    #    2026-10-20: el marcador seguía con el sha del bundle anterior).
    from services.auto_retrain import _write_promotion_marker
    _write_promotion_marker(
        symbol=symbol,
        model_path=model_path,
        reason=(extra.get("promotion_path") or {}).get("reason", "promoted"),
    )
    from core.adapters import AiModelStoreAdapter
    AiModelStoreAdapter().reload(symbol)

    # Coherencia del experimento: una promoción de un experimento YA cerrado
    # deja su fila como 'rejected' (el tracker sólo cierra un 'running' y su
    # UPDATE condicional protege contra el doble cierre). Aquí se corrige de
    # forma explícita y trazada: el desenlace real de exp39 es 'promoted'.
    await _mark_experiment_promoted(exp_id, promoted.get("model_sha256"), promo["reason"])
    return manifest


async def _mark_experiment_promoted(exp_id: int, model_sha256: str | None, reason: str) -> None:
    """Cierra el experimento como 'promoted' (corrección explícita y trazada)."""
    from db.connection import get_pool

    pool = get_pool()
    await pool.execute(
        """
        UPDATE training_experiments
           SET status = 'promoted',
               gate_reason = $2,
               model_sha256 = COALESCE($3, model_sha256)
         WHERE id = $1 AND status <> 'promoted'
        """,
        exp_id, f"drastic_improvement:{reason}"[:200], model_sha256,
    )
    logger.warning(
        "[PROMO-CAND] exp%d: estado del experimento corregido a 'promoted' "
        "(la promoción de un experimento cerrado no la puede hacer el tracker).",
        exp_id,
    )

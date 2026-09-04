"""
Model Backup Service - Versiona y respalda el modelo PPO en Google Cloud Storage.

Ejecuta `pip install google-cloud-storage` para habilitar.
Se puede llamar manualmente o integrar como scheduled task.
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

MODEL_LOCAL_PATH = "/app/ml/ppo_trading_bot.zip"
MODEL_BUCKET = os.getenv("GCS_BUCKET", "trading-models-backup")
GCS_PREFIX = "ppo_models"


def _get_gcs_client():
    """Lazy import para no romper si no está instalado."""
    from google.cloud import storage
    return storage.Client()


def _local_backup_exists() -> bool:
    return os.path.exists(MODEL_LOCAL_PATH)


def _get_model_hash() -> str:
    """Calcula hash MD5 del modelo para detectar cambios."""
    import hashlib
    if not os.path.exists(MODEL_LOCAL_PATH):
        return "no_model"
    with open(MODEL_LOCAL_PATH, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:12]


async def backup_model() -> dict:
    """
    Hace backup del modelo actual a GCS si cambió desde el último backup.
    Returns dict con info del backup.
    """
    if not _local_backup_exists():
        return {"status": "skipped", "reason": "No model file found locally"}

    model_hash = _get_model_hash()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    gcs_path = f"{GCS_PREFIX}/{timestamp}__{model_hash}__ppo_trading_bot.zip"

    try:
        client = _get_gcs_client()
        bucket = client.bucket(MODEL_BUCKET)

        # Check if this hash already exists
        blobs = list(bucket.list_blobs(prefix=f"{GCS_PREFIX}/"))
        existing = [b.name for b in blobs if model_hash in b.name]
        if existing:
            logger.info("[MODEL-BACKUP] Hash %s ya existe en GCS: %s", model_hash, existing[0])
            return {"status": "skipped", "reason": "Hash unchanged", "hash": model_hash, "gcs_path": existing[0]}

        blob = bucket.blob(gcs_path)
        blob.upload_from_filename(MODEL_LOCAL_PATH)
        logger.warning("[MODEL-BACKUP] Backup creado: gs://%s/%s (hash=%s)", MODEL_BUCKET, gcs_path, model_hash)
        return {"status": "success", "hash": model_hash, "gcs_path": f"gs://{MODEL_BUCKET}/{gcs_path}"}

    except Exception as exc:
        logger.error("[MODEL-BACKUP] Falló: %s", exc)
        return {"status": "error", "error": str(exc)}


async def restore_latest_model() -> dict:
    """
    Descarga el modelo más reciente desde GCS si no existe localmente.
    Returns dict con info de la restauración.
    """
    if _local_backup_exists():
        return {"status": "skipped", "reason": "Local model already exists"}

    try:
        client = _get_gcs_client()
        bucket = client.bucket(MODEL_BUCKET)

        blobs = sorted(
            bucket.list_blobs(prefix=f"{GCS_PREFIX}/"),
            key=lambda b: b.updated,
            reverse=True,
        )
        if not blobs:
            return {"status": "error", "reason": "No backups found in GCS"}

        latest = blobs[0]
        logger.warning("[MODEL-BACKUP] Restaurando desde: gs://%s/%s", MODEL_BUCKET, latest.name)

        tmp_path = f"{MODEL_LOCAL_PATH}.tmp"
        latest.download_to_filename(tmp_path)
        shutil.move(tmp_path, MODEL_LOCAL_PATH)

        return {"status": "restored", "gcs_path": f"gs://{MODEL_BUCKET}/{latest.name}", "hash": _get_model_hash()}

    except Exception as exc:
        logger.error("[MODEL-BACKUP] Restauración falló: %s", exc)
        return {"status": "error", "error": str(exc)}

"""
P3-2b: Backup verificado del BUNDLE de modelo — local siempre + GCS mejor esfuerzo.

Corrige tres hallazgos de la auditoría (MLOps):
1. La ruta apuntaba al modelo v2 (`ppo_trading_bot.zip`) — el activo es v3.
2. Solo existía backup a GCS y sin credenciales ADC moría sin más (ahora el
   backup local siempre se ejecuta y se verifica; GCS es mejor esfuerzo con
   alerta explícita de su fallo).
3. Sin verificación de integridad, sin backup programado, y `restore`
   devolvía únicamente el zip (bundle incompleto → validate_bundle falla).

Unidad de backup = el BUNDLE completo (zip + vec_norm + manifest): es lo
mínimo que `validate_bundle` necesita para arrancar un rollback.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone

from config import settings
from services.alerting import AlertLevel, send_alert
from services.model_registry import load_manifest, manifest_path_for, sha256_of

logger = logging.getLogger(__name__)

MODEL_BUCKET = os.getenv("GCS_BUCKET", "trading-models-backup")
GCS_PREFIX = "ppo_models"
LOCAL_KEEP = 10        # retención local: últimos N bundles distintos (por mtime)
GCS_BACKUP_DELAY_S = 60  # primera pasada programada: arranque tranquilo


def default_model_path() -> str:
    """Ruta del bundle activo: PPO_OUTPUT_DIR/ppo_trading_bot_v3.zip (v3, no v2)."""
    return os.path.join(os.getenv("PPO_OUTPUT_DIR", "/app/ml"), "ppo_trading_bot_v3.zip")


def backups_dir() -> str:
    return os.path.join(os.getenv("PPO_OUTPUT_DIR", "/app/ml"), "backups")


def _get_gcs_client():
    """Lazy import: sin google-cloud-storage/ADC el fallo es visible arriba."""
    from google.cloud import storage
    return storage.Client()


def _md5_b64(path: str) -> str:
    """MD5 en base64 — el formato de `blob.md5_hash` de GCS."""
    with open(path, "rb") as f:
        return base64.b64encode(hashlib.md5(f.read()).digest()).decode()


def _bundle_files(model_path: str) -> dict[str, str]:
    """{nombre: ruta} de los ficheros del bundle.

    Lanza si el bundle está incompleto (manifest ausente = sin certificar,
    vec_norm declarado y ausente): no se respaldan bundles incoherentes.
    """
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"modelo ausente: {model_path}")
    manifest = load_manifest(model_path)          # lanza si no existe
    mpath = manifest_path_for(model_path)
    files = {
        os.path.basename(model_path): model_path,
        os.path.basename(mpath): mpath,
    }
    vec_name = manifest.get("vec_norm_file")
    if vec_name:
        vec_path = os.path.join(os.path.dirname(model_path), vec_name)
        if not os.path.exists(vec_path):
            raise FileNotFoundError(f"vec_norm declarado ausente: {vec_path}")
        files[vec_name] = vec_path
    return files


def _prune_local(keep: int = LOCAL_KEEP) -> list[str]:
    """Conserva los `keep` backups locales más recientes (mtime). Devuelve borrados."""
    base = backups_dir()
    if not os.path.isdir(base):
        return []
    dirs = [
        os.path.join(base, n) for n in os.listdir(base)
        if os.path.isdir(os.path.join(base, n))
    ]
    dirs.sort(key=os.path.getmtime, reverse=True)
    removed = []
    for stale in dirs[keep:]:
        try:
            shutil.rmtree(stale)
            removed.append(stale)
        except OSError as exc:
            logger.error("[MODEL-BACKUP] No se pudo purgar %s: %s", stale, exc)
    return removed


def _local_backup(files: dict[str, str], model_sha256: str) -> dict:
    """Copia el bundle a backups/<sha12>/ y VERIFICA cada copia por sha256.

    sha-keyed: respaldar el mismo modelo dos veces es idempotente.
    """
    dest = os.path.join(backups_dir(), model_sha256[:12])
    fresh = not os.path.isdir(dest)
    os.makedirs(dest, exist_ok=True)
    for name, src in files.items():
        tmp = os.path.join(dest, name + ".tmp")
        shutil.copyfile(src, tmp)
        if sha256_of(tmp) != sha256_of(src):
            os.remove(tmp)
            raise RuntimeError(f"verificación local falló para {name}: sha256 distinto")
        os.replace(tmp, os.path.join(dest, name))
    meta = {
        "model_sha256": model_sha256,
        "files": {n: {"sha256": sha256_of(p), "size": os.path.getsize(p)}
                  for n, p in files.items()},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(dest, "backup.json"), "w") as f:
        json.dump(meta, f, indent=2)
    _prune_local()
    return {"path": dest, "fresh": fresh, "verified": True}


async def _gcs_backup(files: dict[str, str], model_sha256: str) -> dict:
    """Sube el bundle a gs://<bucket>/ppo_models/<sha12>/ y verifica size+md5.

    Cualquier fallo (sin ADC, sin paquete, red, mismatch) vuelve como
    `{"error": ...}`: el caller lo alerta — nunca en silencio.
    """
    prefix = f"{GCS_PREFIX}/{model_sha256[:12]}"
    try:
        client = _get_gcs_client()
        bucket = client.bucket(MODEL_BUCKET)
        existing = list(bucket.list_blobs(prefix=f"{prefix}/"))
        if existing:
            return {"gcs_path": f"gs://{MODEL_BUCKET}/{prefix}", "reused": True}
        for name, src in files.items():
            blob = bucket.blob(f"{prefix}/{name}")
            blob.upload_from_filename(src)
            blob.reload()
            if blob.size != os.path.getsize(src):
                raise RuntimeError(
                    f"verificación GCS falló para {name}: size {blob.size} != {os.path.getsize(src)}"
                )
            if blob.md5_hash and blob.md5_hash != _md5_b64(src):
                raise RuntimeError(f"verificación GCS falló para {name}: md5 distinto")
        return {"gcs_path": f"gs://{MODEL_BUCKET}/{prefix}", "verified": True}
    except Exception as exc:  # cualquier fallo GCS no debe matar el backup local
        logger.error("[MODEL-BACKUP] GCS falló: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def backup_model(model_path: str | None = None) -> dict:
    """Backup del bundle: local verificado (siempre) + GCS (si habilitado).

    Returns dict con `status`:
    - `success`               local + GCS verificados
    - `local_success_gcs_disabled` / `local_ok_gcs_failed`
    - `skipped`               no hay modelo local
    - `error`                 incoherente o fallo local (alertado)
    """
    model_path = model_path or default_model_path()
    if not os.path.exists(model_path):
        return {"status": "skipped", "reason": "no local model", "model_path": model_path}

    try:
        files = _bundle_files(model_path)
    except Exception as exc:
        msg = f"Bundle incoherente, no respaldable: {exc}"
        logger.error("[MODEL-BACKUP] %s", msg)
        send_alert(AlertLevel.ERROR, "model_backup", msg, exc=exc)
        return {"status": "error", "error": str(exc), "model_path": model_path}

    model_sha = sha256_of(model_path)
    result: dict = {"model_path": model_path, "model_sha256": model_sha}

    try:
        result["local"] = _local_backup(files, model_sha)
    except Exception as exc:
        msg = f"Backup local falló: {type(exc).__name__}: {exc}"
        logger.error("[MODEL-BACKUP] %s", msg)
        send_alert(AlertLevel.ERROR, "model_backup", msg, exc=exc)
        result.update(status="error", error=str(exc))
        return result
    result["local_verified"] = True

    if not settings.model_backup_enabled:
        result["status"] = "local_success_gcs_disabled"
    else:
        gcs = await _gcs_backup(files, model_sha)
        result["gcs"] = gcs
        if gcs.get("error"):
            send_alert(
                AlertLevel.ERROR, "model_backup",
                f"Backup GCS falló ({gcs['error']}) — local verificado OK",
                context={"bucket": MODEL_BUCKET, "model_sha256": model_sha[:12]},
            )
            result["status"] = "local_ok_gcs_failed"
        else:
            result["status"] = "success"

    logger.warning(
        "[MODEL-BACKUP] %s sha=%s local=%s",
        result["status"], model_sha[:12], result["local"]["path"],
    )
    return result


async def restore_latest_model() -> dict:
    """Restaura el BUNDLE más reciente (local verificado primero; si no, GCS).

    Verificación por sha256/md5 ANTES de tocar el destino: nunca restaura
    bytes corruptos. Si ya existe modelo activo, no hace nada.
    """
    dest_model = default_model_path()
    if os.path.exists(dest_model):
        return {"status": "skipped", "reason": "local model already exists"}

    # ── Local: backups/<sha12>/ más reciente por mtime ─────────────────────
    base = backups_dir()
    candidates = [
        os.path.join(base, n) for n in (os.listdir(base) if os.path.isdir(base) else [])
        if os.path.isdir(os.path.join(base, n))
    ]
    candidates.sort(key=os.path.getmtime, reverse=True)
    for d in candidates:
        meta_path = os.path.join(d, "backup.json")
        if not os.path.exists(meta_path):
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        dest_dir = os.path.dirname(dest_model)
        restored: list[str] = []
        try:
            for name, expect in meta["files"].items():
                src = os.path.join(d, name)
                if not os.path.exists(src):
                    raise RuntimeError(f"fichero ausente en backup: {name}")
                if sha256_of(src) != expect["sha256"]:
                    raise RuntimeError(f"backup corrupto (sha256): {name}")
                tmp = os.path.join(dest_dir, name + ".restore.tmp")
                shutil.copyfile(src, tmp)
                if sha256_of(tmp) != expect["sha256"]:
                    os.remove(tmp)
                    raise RuntimeError(f"restauración corrupta (sha256): {name}")
                os.replace(tmp, os.path.join(dest_dir, name))
                restored.append(name)
        except RuntimeError as exc:
            logger.error("[MODEL-BACKUP] Candidato local inválido %s: %s", d, exc)
            continue
        logger.warning(
            "[MODEL-BACKUP] BUNDLE restaurado desde %s: %s (sha=%s)",
            d, restored, meta.get("model_sha256", "?")[:12],
        )
        return {"status": "restored", "source": d, "files": restored,
                "model_sha256": meta.get("model_sha256")}

    # ── GCS: mejor esfuerzo (bucket con sha12 más reciente por updated) ────
    try:
        client = _get_gcs_client()
        bucket = client.bucket(MODEL_BUCKET)
        blobs = list(bucket.list_blobs(prefix=f"{GCS_PREFIX}/"))
        if not blobs:
            return {"status": "error", "reason": "no backups found (ni local ni GCS)"}
        blobs.sort(key=lambda b: b.updated, reverse=True)
        prefix = blobs[0].name.rsplit("/", 1)[0]
        group = [b for b in blobs if b.name.rsplit("/", 1)[0] == prefix]
        dest_dir = os.path.dirname(dest_model)
        restored = []
        for blob in group:
            name = blob.name.rsplit("/", 1)[1]
            tmp = os.path.join(dest_dir, name + ".restore.tmp")
            blob.download_to_filename(tmp)
            if blob.size != os.path.getsize(tmp) or (
                blob.md5_hash and blob.md5_hash != _md5_b64(tmp)
            ):
                os.remove(tmp)
                raise RuntimeError(f"descarga corrupta (verificación): {name}")
            os.replace(tmp, os.path.join(dest_dir, name))
            restored.append(name)
        logger.warning("[MODEL-BACKUP] BUNDLE restaurado desde gs://%s/%s", MODEL_BUCKET, prefix)
        return {"status": "restored", "source": f"gs://{MODEL_BUCKET}/{prefix}", "files": restored}
    except Exception as exc:
        logger.error("[MODEL-BACKUP] Restauración falló: %s", exc)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


async def run_scheduled_backup() -> dict:
    """Una pasada del backup programado. El fallo se alerta; el loop sobrevive."""
    try:
        return await backup_model(default_model_path())
    except Exception as exc:
        send_alert(
            AlertLevel.ERROR, "model_backup",
            f"Backup programado falló: {type(exc).__name__}: {exc}", exc=exc,
        )
        return {"status": "error", "error": str(exc)}


async def _model_backup_loop() -> None:
    """Backup programado: primera pasada tras 60 s, luego cada N horas."""
    interval = max(1, settings.model_backup_interval_hours) * 3600
    await asyncio.sleep(GCS_BACKUP_DELAY_S)
    while True:
        result = await run_scheduled_backup()
        logger.warning(
            "[MODEL-BACKUP] programado → %s (próximo en %dh)",
            result.get("status"), settings.model_backup_interval_hours,
        )
        await asyncio.sleep(interval)

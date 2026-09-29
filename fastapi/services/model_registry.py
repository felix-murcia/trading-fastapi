"""
Model Registry — certificación e integridad de bundles de modelo (P0-5).

Un "bundle" = zip de PPO + (opcional) estadísticas VecNormalize + manifest JSON
con hashes SHA-256, forma de observación y procedencia. Reglas:

- Sin manifest → el bundle NO está certificado → RuntimeError (fail-fast).
- Hash distinto al certificado → artefacto alterado/reemplazado sin recertificar
  → RuntimeError.
- Si el manifest declara vec_norm_file, ese archivo es OBLIGATORIO y su hash
  debe coincidir. Si no lo declara, el modelo se sirve en crudo (por diseño).

Solo usa stdlib: se invoca en arranque de inferencia y en el proceso de
entrenamiento/promoción sin dependencias de torch ni SB3.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MANIFEST_SUFFIX = ".manifest.json"


def manifest_path_for(model_path: str) -> str:
    """Ruta del manifest que certifica a `model_path`."""
    return os.path.splitext(model_path)[0] + MANIFEST_SUFFIX


def sha256_of(path: str) -> str:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        raise RuntimeError(f"[MODEL-REGISTRY] Archivo requerido ausente: {path}")


def build_manifest(
    model_path: str,
    *,
    obs_shape,
    vec_norm_file: str | None,
    source: str,
    features: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    """Construye el manifest de un bundle a partir de sus archivos reales.

    `vec_norm_file` es el NOMBRE (basename) del pkl junto al zip, o None si el
    modelo se entrenó sin normalización. Calcula los hashes aquí: si el archivo
    no existe o no es legible, lanza RuntimeError (nunca certifica lo que no
    puede leer).
    """
    if not source:
        raise RuntimeError("[MODEL-REGISTRY] source es obligatorio (procedencia).")

    model_sha = sha256_of(model_path)
    norm_sha = None
    if vec_norm_file:
        norm_path = os.path.join(os.path.dirname(model_path), vec_norm_file)
        norm_sha = sha256_of(norm_path)

    manifest = {
        "schema_version": 1,
        "model_file": os.path.basename(model_path),
        "model_sha256": model_sha,
        "obs_shape": [int(d) for d in obs_shape],
        "vec_norm_file": vec_norm_file,
        "vec_norm_sha256": norm_sha,
        "features": list(features) if features is not None else None,
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        manifest["extra"] = extra
    return manifest


def write_manifest(model_path: str, manifest: dict) -> str:
    """Escribe el manifest de forma atómica (tmp + rename)."""
    mpath = manifest_path_for(model_path)
    tmp = mpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, mpath)
    return mpath


def load_manifest(model_path: str) -> dict:
    """Carga el manifest de `model_path`. RuntimeError si falta o está corrupto."""
    mpath = manifest_path_for(model_path)
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"[MODEL-REGISTRY] Manifest ausente: {mpath} — bundle no certificado. "
            "Generarlo con build_manifest/write_manifest al entrenar/promover."
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"[MODEL-REGISTRY] Manifest corrupto ({mpath}): {exc}")

    for key in ("schema_version", "model_sha256", "obs_shape", "vec_norm_file"):
        if key not in manifest:
            raise RuntimeError(f"[MODEL-REGISTRY] Manifest incompleto ({mpath}): falta '{key}'.")
    return manifest


def validate_bundle(model_path: str) -> dict:
    """Valida el bundle contra su manifest. Devuelve el manifest o RuntimeError.

    Comprueba: manifest presente, hash del zip, y —si el manifest declara
    vec_norm_file— presencia y hash de las estadísticas.
    """
    manifest = load_manifest(model_path)

    actual = sha256_of(model_path)
    if actual != manifest["model_sha256"]:
        raise RuntimeError(
            f"[MODEL-REGISTRY] Hash del modelo NO coincide con el manifest: {model_path}\n"
            f"  manifest: {manifest['model_sha256']}\n"
            f"  archivo : {actual}\n"
            "El artefacto cambió sin recertificar (¿retrain sin write_manifest?)."
        )

    vec_norm_file = manifest.get("vec_norm_file")
    if vec_norm_file:
        vec_path = os.path.join(os.path.dirname(model_path), vec_norm_file)
        actual_norm = sha256_of(vec_path)
        if actual_norm != manifest.get("vec_norm_sha256"):
            raise RuntimeError(
                f"[MODEL-REGISTRY] Hash de VecNormalize NO coincide con el manifest: {vec_path}\n"
                f"  manifest: {manifest.get('vec_norm_sha256')}\n"
                f"  archivo : {actual_norm}"
            )

    logger.info(
        "[MODEL-REGISTRY] Bundle verificado: %s (source=%s, vec_norm=%s)",
        manifest["model_file"], manifest["source"], vec_norm_file or "none",
    )
    return manifest


def assert_obs_shape(manifest: dict, obs_shape) -> None:
    """Comprueba que la forma de observación del modelo cargado = la certificada."""
    if obs_shape is None:
        raise RuntimeError(
            "[MODEL-REGISTRY] Modelo cargado sin observation_space; contrato indeterminado."
        )
    declared = tuple(int(d) for d in manifest["obs_shape"])
    actual = tuple(int(d) for d in obs_shape)
    if actual != declared:
        raise RuntimeError(
            f"[MODEL-REGISTRY] obs_shape incoherente: manifest {declared} vs modelo {actual}. "
            "El zip no corresponde al manifest que lo certifica."
        )


async def record_promotion(manifest: dict, gate: dict, *, symbol: str) -> None:
    """P1-2: registra la promoción en la tabla model_registry (historial vivo).

    `symbol` es OBLIGOTORIO (P3-1c): cada promoción declara el mundo al que
    pertenece — la unicidad de activo es POR PAR (`idx_model_registry_one_active_per_symbol`),
    así que el UPDATE solo desactiva la fila activa de ESE par.

    Desactiva la fila activa anterior e inserta la nueva dentro de UNA
    transacción sobre la MISMA conexión, con orden garantizado (UPDATE →
    INSERT): o queda exactamente un bundle activo para ese par o la operación
    entera falla — sin ventana con 0 o 2 activos del mismo par.

    Nota P2-2: la versión con CTE (`WITH deactivated AS (UPDATE …) INSERT …`)
    falló en vivo el 2026-09-28 con el índice de unicidad. Las
    sub-sentencias de un CTE de PostgreSQL no ven sus mutuos efectos y su
    orden es indefinido: el INSERT comprobó la unicidad antes de que el
    UPDATE se aplicara. Con la tabla vacía (primera promoción) no se
    manifestó; con una fila activa ya existente, sí.
    """
    from db.connection import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE model_registry SET is_active = FALSE WHERE is_active AND symbol = $1",
                symbol,
            )
            await conn.execute(
                """
                INSERT INTO model_registry
                    (symbol, model_sha256, vec_norm_sha256, source, obs_shape, extra, gate_reason, is_active)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, TRUE)
                """,
                symbol,
                manifest["model_sha256"],
                manifest.get("vec_norm_sha256"),
                manifest["source"],
                json.dumps([int(d) for d in manifest["obs_shape"]]),
                json.dumps(
                    {"gate": gate, "manifest_extra": manifest.get("extra", {})},
                    default=float,
                    ensure_ascii=False,
                ),
                gate.get("reason"),
            )
    logger.info(
        "[MODEL-REGISTRY] Promoción registrada: symbol=%s sha=%s… reason=%s",
        symbol, manifest["model_sha256"][:12], gate.get("reason"),
    )

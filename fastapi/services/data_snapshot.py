"""
P3-2a: Snapshot de datos de entrenamiento — retrain reproducible.

Congela el DataFrame de features exactamente tal y como lo consumieron el
entreno y el gate walk-forward. Sin snapshot no hay forma de repetir un
experimento ni de auditar qué datos produjeron un candidato concreto
(hallazgo de auditoría: "sin snapshot de datos → retrain no reproducible").

Almacenamiento: {PPO_OUTPUT_DIR}/snapshots/<utc>__<sha12>__<purpose>.csv.gz
- <sha12> es el prefijo del sha256 del CONTENIDO (csv antes de comprimir):
  identifica los datos, no la compresión (gzip incrusta mtime → bytes no
  deterministas, por eso se hashea el csv).
- Verificación post-escritura: relectura del fichero; si filas/columnas no
  cuadran con el df original, falla ruidosamente (sin fallos silenciosos).
"""

import gzip
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

SNAPSHOT_KEEP = 10  # retención: se conservan los N snapshots más recientes


@dataclass(frozen=True)
class SnapshotInfo:
    path: str
    sha256: str  # sha256 del contenido csv (antes de gzip)
    rows: int
    columns: int


def snapshot_dir() -> str:
    base = os.getenv("PPO_OUTPUT_DIR", "/app/ml")
    return os.path.join(base, "snapshots")


def save_snapshot(df: pd.DataFrame, purpose: str) -> SnapshotInfo:
    """Escribe el df como snapshot comprimido y verifica la lectura.

    Lanza sobre df vacío o si la verificación de lectura falla.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError(f"save_snapshot: df inválido o vacío (purpose={purpose!r})")

    safe_purpose = re.sub(r"[^a-z0-9_]+", "_", purpose.lower()).strip("_") or "snapshot"
    csv_text = df.to_csv(index=False)
    content_sha = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = snapshot_dir()
    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(out_dir, f"{ts}__{content_sha[:12]}__{safe_purpose}.csv.gz")
    tmp_path = final_path + ".tmp"

    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
            f.write(csv_text)
        # Verificación: relectura del artefacto → mismas dimensiones que el df.
        # compression explícito: el sufijo ".tmp" impide la inferencia de pandas.
        check = pd.read_csv(tmp_path, compression="gzip")
        if check.shape != df.shape:
            raise RuntimeError(
                f"save_snapshot: verificación fallida para {final_path}: "
                f"leído {check.shape} != original {df.shape}"
            )
        os.replace(tmp_path, final_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    info = SnapshotInfo(path=final_path, sha256=content_sha, rows=int(df.shape[0]), columns=int(df.shape[1]))
    logger.warning(
        "[SNAPSHOT] %s rows=%d cols=%d sha=%s",
        os.path.basename(final_path), info.rows, info.columns, content_sha[:12],
    )
    return info


def list_snapshots() -> list[str]:
    """Rutas de snapshots válidos, más recientes primero (orden por nombre=UTC)."""
    out_dir = snapshot_dir()
    if not os.path.isdir(out_dir):
        return []
    names = [n for n in os.listdir(out_dir) if n.endswith(".csv.gz")]
    return [os.path.join(out_dir, n) for n in sorted(names, reverse=True)]


def prune_snapshots(keep: int = SNAPSHOT_KEEP) -> list[str]:
    """Elimina los snapshots más allá de los `keep` más recientes. Devuelve borrados."""
    if keep < 1:
        raise ValueError(f"prune_snapshots: keep debe ser >= 1 (got {keep})")
    paths = list_snapshots()
    removed: list[str] = []
    for path in paths[keep:]:
        try:
            os.remove(path)
            removed.append(path)
        except OSError as exc:
            logger.error("[SNAPSHOT] No se pudo purgar %s: %s", path, exc)
    if removed:
        logger.warning("[SNAPSHOT] Purga: %d eliminados, %d conservados", len(removed), keep)
    return removed

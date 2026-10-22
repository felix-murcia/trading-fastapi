"""
P3-2a (P2-5: parquet) — Snapshot de datos de entrenamiento: retrain reproducible.

Congela el DataFrame de features exactamente tal y como lo consumieron el
entreno y el gate walk-forward. Sin snapshot no hay forma de repetir un
experimento ni de auditar qué datos produjaron un candidato concreto
(hallazgo de auditoría: "sin snapshot de datos → retrain no reproducible").

Almacenamiento: {PPO_OUTPUT_DIR}/snapshots/<first_ts>_<last_ts>__<sha12>__<purpose>.parquet
- <first_ts>_<last_ts>: rango temporal REAL de los datos (columna `time`,
  MIN/MAX — robusto al orden de llegada: el MCP entrega velas descendentes,
  P0). Un vistazo al nombre dice qué periodo congeló.
- <sha12>: prefijo del sha256 del contenido canónico (csv del df, previo a
  cualquier formato): identifica los DATOS, no el envoltorio — parquet puede
  variar bytes con la versión de pyarrow, el csv del df es estable.
- Verificación post-escritura: relectura del artefacto; si filas/columnas no
  cuadran con el df original, falla ruidosamente (sin fallos silenciosos).

Nadie LEE snapshots (write-only: save/list/prune) → no hay migración de
lectores. `list_snapshots`/`prune` reconocen `.parquet` Y los legacy
`.csv.gz` para que la retención purgue también los antiguos.
"""

import hashlib
import logging
import os
import re
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

SNAPSHOT_KEEP = 10  # retención: se conservan los N snapshots más recientes
_SNAPSHOT_EXTS = (".parquet", ".csv.gz")  # csv.gz = legado P3-2a pre-parquet


@dataclass(frozen=True)
class SnapshotInfo:
    path: str
    sha256: str  # sha256 del contenido canónico (csv del df)
    rows: int
    columns: int


def snapshot_dir() -> str:
    base = os.getenv("PPO_OUTPUT_DIR", "/app/ml")
    return os.path.join(base, "snapshots")


def _range_label(df: pd.DataFrame) -> str:
    """`<first>_<last>` en UTC — MIN/MAX de la columna `time`.

    Soporta los tres formatos reales: ISO del MCP ("...Z"), epoch de
    `mt5_client.get_candles` (segundos) y datetime64 (fixtures). Min/Max en
    vez de primera/última fila: el orden de llegada es indiferente.
    """
    if "time" not in df.columns:
        raise ValueError(
            "save_snapshot: el df no tiene columna 'time' — el nombre por "
            "rango exige las velas originales"
        )
    times = df["time"]
    if pd.api.types.is_numeric_dtype(times):
        ts = pd.to_datetime(times, unit="s", utc=True, errors="raise")
    else:
        ts = pd.to_datetime(times, utc=True, errors="raise")
    if ts.isna().any():
        raise ValueError("save_snapshot: columna 'time' con valores no parseables")
    first = ts.min().strftime("%Y%m%dT%H%M%SZ")
    last = ts.max().strftime("%Y%m%dT%H%M%SZ")
    return f"{first}_{last}"


def save_snapshot(df: pd.DataFrame, purpose: str) -> SnapshotInfo:
    """Escribe el df como parquet y verifica la relectura.

    Lanza sobre df vacío, sin columna `time`, o si la verificación falla.
    Mismo rango+hash que un snapshot idéntico anterior → mismo fichero
    (deduplicación: datos idénticos son el mismo snapshot).
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError(f"save_snapshot: df inválido o vacío (purpose={purpose!r})")

    safe_purpose = re.sub(r"[^a-z0-9_]+", "_", purpose.lower()).strip("_") or "snapshot"
    rng = _range_label(df)
    # Hash sobre el csv canónico: identidad de los DATOS, no del formato.
    csv_text = df.to_csv(index=False)
    content_sha = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()

    out_dir = snapshot_dir()
    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(
        out_dir, f"{rng}__{content_sha[:12]}__{safe_purpose}.parquet"
    )
    tmp_path = final_path + ".tmp"

    try:
        df.to_parquet(tmp_path, engine="pyarrow", index=False)
        # Verificación: relectura del artefacto → mismas dimensiones que el df.
        check = pd.read_parquet(tmp_path)
        if check.shape != df.shape:
            raise RuntimeError(
                f"save_snapshot: verificación fallida para {final_path}: "
                f"leído {check.shape} != original {df.shape}"
            )
        os.replace(tmp_path, final_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    info = SnapshotInfo(path=final_path, sha256=content_sha,
                        rows=int(df.shape[0]), columns=int(df.shape[1]))
    logger.warning(
        "[SNAPSHOT] %s rows=%d cols=%d sha=%s",
        os.path.basename(final_path), info.rows, info.columns, content_sha[:12],
    )
    return info


def list_snapshots() -> list[str]:
    """Rutas de snapshots válidos, más recientes primero.

    Orden por nombre (timestamp de prefijo: rango de datos en `.parquet`,
    UTC de escritura en los legados `.csv.gz` — el rango de una ventana
    rodante avanza con el tiempo, así que ambos ordenan en cómputo general).
    Reconoce `.parquet` y `.csv.gz` (legado) para que la retención purgue
    también los antiguos.
    """
    out_dir = snapshot_dir()
    if not os.path.isdir(out_dir):
        return []
    names = [
        n for n in os.listdir(out_dir)
        if n.endswith(_SNAPSHOT_EXTS) and not n.endswith(".tmp")
    ]
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

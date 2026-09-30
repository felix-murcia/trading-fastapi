#!/usr/bin/env python3
"""
M-1 (plan 2026-09-30): versiona los artefactos de la última promoción.

Gatillo: cron de usuario cada 5 min (crontab; ver PROTOCOLO_ANTI_BUCLE.md).
`auto_retrain._write_promotion_marker` escribe `last_promotion.json` JUNTO al
bundle al final de cada promoción exitosa; mientras ese marcador difiera de
HEAD, hay artefactos de promoción sin versionar. Entonces:

1. valida que el zip en disco sea EXACTAMENTE el del marcador (sha256) —
   jamás versionar un estado a medias ni un bundle ajeno;
2. `git add fastapi/ml` (los zips de ml/ están exentos de `*.zip` desde M-1;
   backups/ y snapshots/ siguen ignorados por sus propias reglas);
3. commitea con mensaje `M-1 promoción <symbol>: <model> sha=... — gate: ...`,
   dejando el propio marcador en el commit como evidencia permanente.

Sin marcador sucio → no-op (exit 0). Cualquier fallo → exit 1 con el marcador
en su sitio: el próximo cron reintenta y `git status` muestra la pendencia —
el fallo nunca es silencioso. El alcance del commit es EXCLUSIVAMENTE
`fastapi/ml`: jamás toca código en progreso.

Uso: python3 scripts/commit_promotion.py [REPO]
     (por defecto, la raíz del repo que contiene este script)
"""
import hashlib
import json
import os
import subprocess
import sys

_DEFAULT_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER_NAME = "last_promotion.json"
# Directorios de ml/ que no son promociones (backups locales y snapshots de
# train): sus contenidos no interesan al versionado de bundles.
_SKIP_DIRS = {"backups", "snapshots", "__pycache__"}


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=check,
    )


def find_pending_markers(repo: str) -> list[str]:
    """Rel paths (desde repo) de marcadores cuyo contenido difiere de HEAD.

    Un marcador nuevo (`??`) o modificado (`M`) = promoción sin versionar.
    Los que ya coinciden con HEAD son no-ops.
    """
    ml_dir = os.path.join(repo, "fastapi", "ml")
    if not os.path.isdir(ml_dir):
        return []
    pending: list[str] = []
    for dirpath, dirs, files in os.walk(ml_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if MARKER_NAME in files:
            rel = os.path.relpath(os.path.join(dirpath, MARKER_NAME), repo)
            status = _git(repo, "status", "--porcelain", "--", rel).stdout.strip()
            if status:
                pending.append(rel)
    return sorted(pending)


def validate_marker(repo: str, rel_marker: str) -> dict:
    """Carga el marcador y exige que el bundle en disco sea su sha256 exacto."""
    path = os.path.join(repo, rel_marker)
    with open(path, encoding="utf-8") as f:
        marker = json.load(f)
    for key in ("symbol", "model", "model_sha256", "reason"):
        if not marker.get(key):
            raise ValueError(f"marcador sin '{key}': {rel_marker}")
    bundle = os.path.join(os.path.dirname(path), marker["model"])
    digest = hashlib.sha256()
    with open(bundle, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != marker["model_sha256"]:
        raise ValueError(
            f"sha256 distinto para {bundle}: "
            f"{digest.hexdigest()[:12]} != {marker['model_sha256'][:12]}"
        )
    return marker


def main(repo: str) -> int:
    pending = find_pending_markers(repo)
    if not pending:
        return 0
    print(f"[M-1] promoción pendiente de versionar: {', '.join(pending)}")

    markers = []
    for rel in pending:
        try:
            markers.append(validate_marker(repo, rel))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
            print(f"[M-1] validación fallida, NO se commitea: {e}", file=sys.stderr)
            return 1

    _git(repo, "add", "--", "fastapi/ml")
    staged = _git(repo, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("[M-1] nada staged tras git add — estado incoherente", file=sys.stderr)
        return 1
    if staged.returncode > 1:
        print(f"[M-1] git diff falló: {staged.stderr.strip()}", file=sys.stderr)
        return 1

    # La promoción más reciente firma el mensaje (si hay varias en la ventana
    # de cron, todas quedan en el commit).
    latest = max(markers, key=lambda m: m.get("created_at", ""))
    msg = (
        f"M-1 promoción {latest['symbol']}: {latest['model']} "
        f"sha={latest['model_sha256'][:12]} — gate: {latest['reason']}"
    )
    if len(markers) > 1:
        msg += f" (+{len(markers) - 1} promociones más en la ventana)"
    commit = _git(repo, "commit", "-m", msg, check=False)
    if commit.returncode != 0:
        print(f"[M-1] commit falló: {commit.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"[M-1] {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1])) if len(sys.argv) > 1 else main(_DEFAULT_REPO))

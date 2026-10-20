#!/usr/bin/env python3
"""
Versiona el REGISTRO DIARIO INMUTABLE (append-only) en git.

Gatillo: cron de usuario (una vez al día, p. ej. 23:55) — el mismo patrón
que `scripts/commit_promotion.py` (que versiona el bundle cuando hay una
promoción).

Por qué (Félix, 2026-10-19/20): la confianza no puede depender de quien
narra. El sistema escribe sus propios números una vez por hora en
`fastapi/ops/daily_ledger.jsonl`; este script los mete en el historial de git
⇒ **cualquier retoque posterior de una línea deja rastro** (`git log -p`).

Reglas:
- sólo versiona `fastapi/ops/daily_ledger.jsonl` (jamás código en progreso);
- si el fichero no cambió ⇒ no-op (exit 0);
- si git falla ⇒ exit 1 y el fichero queda pendiente (el próximo cron
  reintenta; `git status` muestra la pendencia — el fallo nunca es silencioso).

Uso: python3 scripts/commit_daily_ledger.py [REPO]
"""
import subprocess
import sys
from pathlib import Path

LEDGER_REL = "fastapi/ops/daily_ledger.jsonl"


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    ledger = repo / LEDGER_REL
    if not ledger.exists():
        print(f"[DAILY-LEDGER] sin {LEDGER_REL} — nada que versionar")
        return 0

    st = _run(["git", "status", "--porcelain", "--", LEDGER_REL], repo)
    if st.returncode != 0:
        print(f"[DAILY-LEDGER] git status falló: {st.stderr.strip()[:200]}")
        return 1
    if not st.stdout.strip():
        print("[DAILY-LEDGER] sin cambios pendientes — no-op")
        return 0

    n_lines = 0
    try:
        n_lines = sum(1 for _ in ledger.open(encoding="utf-8"))
    except OSError:
        pass

    _run(["git", "add", "--", LEDGER_REL], repo)
    msg = f"registro diario inmutable: {n_lines} línea(s) del ledger (append-only)"
    c = _run(["git", "commit", "-m", msg, "--", LEDGER_REL], repo)
    if c.returncode != 0:
        print(f"[DAILY-LEDGER] commit falló: {c.stderr.strip()[:300]}")
        return 1
    p = _run(["git", "push", "origin", "master"], repo)
    if p.returncode != 0:
        print(f"[DAILY-LEDGER] push falló: {p.stderr.strip()[:200]} (el commit queda local)")
        return 1
    print(f"[DAILY-LEDGER] versionadas {n_lines} línea(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

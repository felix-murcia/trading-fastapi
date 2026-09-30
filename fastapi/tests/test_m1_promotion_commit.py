"""
M-1 (plan 2026-09-30): promociones versionadas — marcador + commit de cron.

`auto_retrain._write_promotion_marker` deja `last_promotion.json` junto al
bundle al final de cada promoción; `scripts/commit_promotion.py` (cron cada
5 min) valida el sha256, versiona `fastapi/ml` exclusivamente y deja el
marcador como evidencia en el historial. Sin marcador sucio = no-op; cualquier
fallo = exit 1 con la pendencia visible en `git status` (jamás silencioso).

Los tests de script corren contra un repo git temporal que copia el
`.gitignore` REAL del repo: la exención `!fastapi/ml/*.zip` queda cubierta —
si alguien la rompe, el bundle no se versiona y el test falla.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock

from services.alerting import AlertLevel

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "commit_promotion.py")
MARKER = "last_promotion.json"


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _rev(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_repo(tmp_path):
    """Repo git temporal con el .gitignore REAL del proyecto."""
    repo = tmp_path / "repo"
    (repo / "fastapi" / "ml").mkdir(parents=True)
    shutil.copy(os.path.join(REPO_ROOT, ".gitignore"), repo / ".gitignore")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@local")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _write_promotion(repo, symbol="EURUSD", model="ppo_trading_bot_v3.zip", sha=None):
    """Simula la escritura que hace auto_retrain al final de la promoción."""
    ml = repo / "fastapi" / "ml"
    data = b"FAKE-BUNDLE-" + model.encode()
    (ml / model).write_bytes(data)
    digest = sha or hashlib.sha256(data).hexdigest()
    (ml / MARKER).write_text(json.dumps({
        "symbol": symbol,
        "model": model,
        "model_sha256": digest,
        "reason": "gate walk-forward ok",
        "created_at": "2026-09-30T20:00:00+00:00",
    }), encoding="utf-8")
    return digest


def _run_script(repo):
    return subprocess.run(
        [sys.executable, SCRIPT, str(repo)],
        capture_output=True, text=True,
    )


class TestCommitScript:
    """scripts/commit_promotion.py contra un repo temporal."""

    def test_no_pending_marker_is_noop(self, tmp_path):
        repo = _init_repo(tmp_path)
        before = _rev(repo)
        res = _run_script(repo)
        assert res.returncode == 0, res.stderr
        assert _rev(repo) == before

    def test_pending_promotion_is_committed_with_evidence(self, tmp_path):
        repo = _init_repo(tmp_path)
        digest = _write_promotion(repo)

        res = _run_script(repo)
        assert res.returncode == 0, res.stderr

        msg = _git(repo, "log", "-1", "--pretty=%B").stdout
        assert "M-1 promoción EURUSD: ppo_trading_bot_v3.zip" in msg
        assert digest[:12] in msg
        assert "gate walk-forward ok" in msg
        # El bundle queda versionado (exención !fastapi/ml/*.zip operativa) y
        # el marcador viaja en el commit como evidencia permanente.
        tracked = _git(repo, "ls-files", "--", "fastapi/ml").stdout
        assert "fastapi/ml/ppo_trading_bot_v3.zip" in tracked
        assert f"fastapi/ml/{MARKER}" in tracked
        # Árbol limpio tras el commit → segunda corrida = no-op.
        assert not _git(repo, "status", "--porcelain", "--", "fastapi/ml").stdout
        before = _rev(repo)
        assert _run_script(repo).returncode == 0
        assert _rev(repo) == before

    def test_sha_mismatch_blocks_commit(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_promotion(repo, sha="a" * 64)   # sha que NO es el del zip
        before = _rev(repo)
        res = _run_script(repo)
        assert res.returncode == 1
        assert "NO se commitea" in res.stderr
        assert _rev(repo) == before
        # La pendencia sigue visible: el próximo cron reintenta.
        assert _git(repo, "status", "--porcelain", "--", f"fastapi/ml/{MARKER}").stdout

    def test_corrupt_marker_blocks_commit(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_promotion(repo)
        (repo / "fastapi" / "ml" / MARKER).write_text("{corrupto", encoding="utf-8")
        before = _rev(repo)
        assert _run_script(repo).returncode == 1
        assert _rev(repo) == before

    def test_backup_markers_do_not_trigger(self, tmp_path):
        """backups/ y snapshots/ no son promociones: su marcador no dispara."""
        repo = _init_repo(tmp_path)
        ml = repo / "fastapi" / "ml"
        for sub in ("backups", "snapshots"):
            d = ml / sub
            d.mkdir()
            (d / "x.zip").write_bytes(b"b")
            (d / MARKER).write_text(json.dumps({
                "symbol": "EURUSD", "model": "x.zip",
                "model_sha256": hashlib.sha256(b"b").hexdigest(),
                "reason": "r", "created_at": "2026-09-30T20:00:00+00:00",
            }), encoding="utf-8")
        before = _rev(repo)
        res = _run_script(repo)
        assert res.returncode == 0, res.stderr
        assert _rev(repo) == before


class TestWritePromotionMarker:
    """auto_retrain._write_promotion_marker: escritura y degradación."""

    def test_marker_written_next_to_bundle(self, tmp_path):
        from services.auto_retrain import _write_promotion_marker

        bundle = tmp_path / "ppo_trading_bot_v3.zip"
        bundle.write_bytes(b"bundle-bytes")
        _write_promotion_marker(
            symbol="EURUSD", model_path=str(bundle), reason="gate ok"
        )

        marker = json.loads((tmp_path / MARKER).read_text(encoding="utf-8"))
        assert marker["symbol"] == "EURUSD"
        assert marker["model"] == "ppo_trading_bot_v3.zip"
        assert marker["model_sha256"] == hashlib.sha256(b"bundle-bytes").hexdigest()
        assert marker["reason"] == "gate ok"
        created = datetime.fromisoformat(marker["created_at"])
        assert created.tzinfo is not None and created.utcoffset().total_seconds() == 0

    def test_write_failure_alerts_and_does_not_raise(self, tmp_path, monkeypatch):
        """La promoción ya está viva: sin rollback, pero el fallo es alerta."""
        from services import auto_retrain as ar

        bundle = tmp_path / "ppo_trading_bot_v3.zip"
        bundle.write_bytes(b"bundle-bytes")
        alerts = MagicMock()
        monkeypatch.setattr(ar, "send_alert", alerts)
        monkeypatch.setattr(
            "os.replace", MagicMock(side_effect=OSError("read-only fs"))
        )

        ar._write_promotion_marker(symbol="EURUSD", model_path=str(bundle), reason="x")

        alerts.assert_called_once()
        level, component = alerts.call_args.args[:2]
        assert level == AlertLevel.ERROR
        assert component == "auto_retrain"
        assert not (tmp_path / MARKER).exists()

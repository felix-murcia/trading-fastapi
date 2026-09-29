"""
P3-2b: services/model_backup.py — backup verificado del bundle v3.

Cubre los hallazgos de la auditoría:
- ruta correcta (v3, no v2)
- bundle completo (zip + vec_norm + manifest)
- verificación sha256 local / size+md5 GCS
- GCS mejor esfuerzo con alerta (sin credenciales ADC no silencia el fallo)
- restore verificado del bundle
"""
import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest

from services import model_backup as mb
from services.model_registry import build_manifest, sha256_of, write_manifest


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Bundle certificado mínimo en PPO_OUTPUT_DIR=tmp_path."""
    monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(mb.settings, "model_backup_enabled", False)
    zip_path = tmp_path / "ppo_trading_bot_v3.zip"
    zip_path.write_bytes(b"fake-model-bytes-v3")
    vec = tmp_path / "ppo_trading_bot_v3_vec_norm.pkl"
    vec.write_bytes(b"fake-vecnorm-bytes")
    write_manifest(str(zip_path), build_manifest(
        str(zip_path), obs_shape=(20, 24), vec_norm_file=vec.name, source="test",
    ))
    return str(zip_path)


class TestDefaultPath:
    def test_default_model_path_is_v3_not_v2(self, tmp_path, monkeypatch):
        """Auditoría: backup apuntaba al modelo v2 (ppo_trading_bot.zip)."""
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        assert mb.default_model_path() == str(tmp_path / "ppo_trading_bot_v3.zip")

    def test_source_has_no_v2_default_path(self):
        with open(mb.__file__) as f:
            src = f.read()
        assert '"ppo_trading_bot.zip"' not in src, "la ruta por defecto debe ser v3"


class TestLocalBackup:
    @pytest.mark.asyncio
    async def test_local_copies_bundle_and_verifies(self, bundle):
        result = await mb.backup_model(bundle)

        assert result["status"] == "local_success_gcs_disabled"
        assert result["local_verified"] is True
        sha12 = result["model_sha256"][:12]
        dest = os.path.join(mb.backups_dir(), sha12)
        for name in ("ppo_trading_bot_v3.zip",
                     "ppo_trading_bot_v3_vec_norm.pkl",
                     "ppo_trading_bot_v3.manifest.json"):
            copied = os.path.join(dest, name)
            assert os.path.exists(copied), name
            assert sha256_of(copied) == sha256_of(
                os.path.join(os.path.dirname(bundle), name)
            )
        meta = json.load(open(os.path.join(dest, "backup.json")))
        assert meta["model_sha256"] == result["model_sha256"]

    @pytest.mark.asyncio
    async def test_same_model_is_idempotent(self, bundle):
        first = await mb.backup_model(bundle)
        second = await mb.backup_model(bundle)

        assert first["local"]["fresh"] is True
        assert second["local"]["fresh"] is False   # sha-keyed: re-verifica, no duplica

    @pytest.mark.asyncio
    async def test_missing_model_skips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        result = await mb.backup_model()
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_missing_manifest_is_error_and_alerts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(mb, "send_alert", MagicMock())
        zip_path = tmp_path / "ppo_trading_bot_v3.zip"
        zip_path.write_bytes(b"uncertified")

        result = await mb.backup_model(str(zip_path))

        assert result["status"] == "error"
        assert "Manifest ausente" in result["error"] or "manifest" in result["error"].lower()
        mb.send_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_declared_vec_norm_missing_is_error(self, bundle, monkeypatch):
        monkeypatch.setattr(mb, "send_alert", MagicMock())
        os.remove(os.path.join(os.path.dirname(bundle), "ppo_trading_bot_v3_vec_norm.pkl"))

        result = await mb.backup_model(bundle)

        assert result["status"] == "error"
        assert "vec_norm" in result["error"]
        mb.send_alert.assert_called_once()


class TestGCS:
    @staticmethod
    def _fake_gcs(monkeypatch, *, fail_at_upload=False):
        class FakeBlob:
            def __init__(self, name):
                self.name = name
                self.size = None
                self.md5_hash = None

            def upload_from_filename(self, src):
                if fail_at_upload:
                    raise RuntimeError("ADC credentials missing")
                import base64, hashlib
                with open(src, "rb") as f:
                    data = f.read()
                self.size = len(data)
                self.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()

            def reload(self):
                pass

            def download_to_filename(self, path):
                raise AssertionError("no usado en estos tests")

        class FakeBucket:
            def __init__(self):
                self.uploaded: dict[str, FakeBlob] = {}

            def blob(self, name):
                b = FakeBlob(name)
                self.uploaded[name] = b
                return b

            def list_blobs(self, prefix=""):
                return [b for n, b in self.uploaded.items() if n.startswith(prefix)]

        class FakeClient:
            def __init__(self):
                self._bucket = FakeBucket()

            def bucket(self, name):
                return self._bucket

        client = FakeClient()
        monkeypatch.setattr(mb, "_get_gcs_client", lambda: client)
        return client

    @pytest.mark.asyncio
    async def test_gcs_upload_verified_success(self, bundle, monkeypatch):
        client = self._fake_gcs(monkeypatch)
        monkeypatch.setattr(mb.settings, "model_backup_enabled", True)

        result = await mb.backup_model(bundle)

        assert result["status"] == "success"
        assert result["gcs"]["verified"] is True
        uploaded = list(client._bucket.uploaded)
        assert len(uploaded) == 3   # zip + vec_norm + manifest

    @pytest.mark.asyncio
    async def test_gcs_failure_alerts_but_local_ok(self, bundle, monkeypatch):
        client = self._fake_gcs(monkeypatch, fail_at_upload=True)
        monkeypatch.setattr(mb.settings, "model_backup_enabled", True)
        monkeypatch.setattr(mb, "send_alert", MagicMock())

        result = await mb.backup_model(bundle)

        assert result["status"] == "local_ok_gcs_failed"
        assert "ADC" in result["gcs"]["error"]
        assert result["local_verified"] is True
        mb.send_alert.assert_called_once()   # el fallo GCS es visible

    @pytest.mark.asyncio
    async def test_gcs_reuses_existing_sha(self, bundle, monkeypatch):
        client = self._fake_gcs(monkeypatch)
        monkeypatch.setattr(mb.settings, "model_backup_enabled", True)
        first = await mb.backup_model(bundle)
        assert first["status"] == "success"

        second = await mb.backup_model(bundle)

        assert second["gcs"].get("reused") is True


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_roundtrip_after_deletion(self, bundle, tmp_path, monkeypatch):
        original = {n: sha256_of(os.path.join(tmp_path, n)) for n in (
            "ppo_trading_bot_v3.zip", "ppo_trading_bot_v3_vec_norm.pkl",
            "ppo_trading_bot_v3.manifest.json",
        )}
        backup = await mb.backup_model(bundle)
        assert backup["status"] == "local_success_gcs_disabled"

        # Destruye el bundle activo (simula pérdida de datos)
        for name in original:
            os.remove(tmp_path / name)
        assert not os.path.exists(bundle)

        result = await mb.restore_latest_model()

        assert result["status"] == "restored"
        assert set(result["files"]) == set(original)
        for name, sha in original.items():
            assert sha256_of(os.path.join(tmp_path, name)) == sha

    @pytest.mark.asyncio
    async def test_restore_skips_when_active_exists(self, bundle):
        result = await mb.restore_latest_model()
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_restore_refuses_corrupt_backup(self, bundle, tmp_path):
        await mb.backup_model(bundle)
        sha12 = sha256_of(bundle)[:12]
        # Corrompe el zip del backup
        with open(os.path.join(mb.backups_dir(), sha12, "ppo_trading_bot_v3.zip"), "wb") as f:
            f.write(b"corrupted-bytes")
        for name in ("ppo_trading_bot_v3.zip", "ppo_trading_bot_v3_vec_norm.pkl",
                     "ppo_trading_bot_v3.manifest.json"):
            os.remove(tmp_path / name)

        result = await mb.restore_latest_model()

        # No restaura bytes corruptos: reporta error (ni zip activo incompleto)
        assert result["status"] == "error"
        assert not os.path.exists(bundle)


class TestScheduled:
    @pytest.mark.asyncio
    async def test_run_scheduled_backup_delegates_per_enabled_pair(self, monkeypatch):
        """Cada par habilitado recibe SU backup (P3-1c: un mundo por iteración)."""
        called = {}

        async def fake_backup(*, symbol):
            called[symbol] = called.get(symbol, 0) + 1
            return {"symbol": symbol, "status": "success"}

        monkeypatch.setattr(mb, "backup_model", fake_backup)

        result = await mb.run_scheduled_backup()

        # Config por defecto: un único par habilitado (EURUSD, legado)
        assert called == {"EURUSD": 1}
        assert result == [{"symbol": "EURUSD", "status": "success"}]

    @pytest.mark.asyncio
    async def test_run_scheduled_backup_alerts_on_crash(self, monkeypatch):
        monkeypatch.setattr(mb, "send_alert", MagicMock())

        async def boom(*, symbol):
            raise RuntimeError("disk full")

        monkeypatch.setattr(mb, "backup_model", boom)

        result = await mb.run_scheduled_backup()

        assert result[0]["status"] == "error"
        assert "disk full" in result[0]["error"]
        mb.send_alert.assert_called_once()   # el loop sobrevive, el fallo es visible

    @pytest.mark.asyncio
    async def test_loop_wired_in_lifespan(self):
        """El loop programado se dispara desde lifespan (main.py)."""
        from main import _bg_tasks  # noqa: F401 — conjunto de tareas de fondo
        with open(os.path.join(os.path.dirname(mb.__file__), "..", "main.py")) as f:
            src = f.read()
        assert "_model_backup_loop" in src
        assert "_bg_tasks.add(asyncio.create_task(_model_backup_loop()))" in src

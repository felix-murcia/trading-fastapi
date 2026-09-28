"""
P1-2: trazabilidad y hot-reload de bundles de modelo.

Cobertura (comportamiento, sin red ni DB real):
- record_promotion: historial en model_registry con CTE (exactamente un bundle
  activo por sentencia) y parámetros = hashes/source/gate del manifest.
- load_inference_artifacts: swap ATÓMICO — si VecNormalize falla, la caché en
  memoria sigue sirviendo el bundle anterior (nunca estado a medias).
- reload_inference_artifacts: devuelve el estado en memoria tras recargar.
- GET /api/v1/ai/model/status: token obligatorio + in_sync disco vs memoria.
- schema.sql: índice único parcial que garantiza un único bundle activo.
"""
import json
import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

from main import app

TOKEN = {"x-internal-token": "test-token"}


def _manifest(vec_norm=True):
    return {
        "schema_version": 1,
        "model_file": "ppo_trading_bot_v3.zip",
        "model_sha256": "a" * 64,
        "obs_shape": [20, 24],
        "vec_norm_file": "ppo_trading_bot_v3_vec_norm.pkl" if vec_norm else None,
        "vec_norm_sha256": "b" * 64 if vec_norm else None,
        "source": "auto_retrain_normalized",
        "created_at": "2026-09-28T00:00:00+00:00",
        "extra": {"gate": {"ok": True}},
    }


def _spaces():
    obs = Box(-np.inf, np.inf, shape=(20, 24), dtype=np.float32)
    act = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
    return obs, act


class TestRecordPromotion:
    @pytest.mark.asyncio
    async def test_single_statement_deactivates_and_inserts(self, monkeypatch):
        from services import model_registry as mr

        pool = MagicMock()
        pool.execute = AsyncMock()
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        gate = {"ok": True, "reason": "walk_forward_improved"}
        await mr.record_promotion(_manifest(), gate)

        pool.execute.assert_awaited_once()
        sql, *params = pool.execute.await_args.args
        # Una sola sentencia: desactivar la anterior e insertar la nueva es
        # atómico (CTE) — sin ventana con 0 o 2 bundles activos.
        assert "UPDATE model_registry SET is_active = FALSE WHERE is_active" in sql
        assert "INSERT INTO model_registry" in sql
        assert sql.count("INSERT") == 1
        assert sql.count("UPDATE") == 1

        assert params[0] == "a" * 64
        assert params[1] == "b" * 64
        assert params[2] == "auto_retrain_normalized"
        assert json.loads(params[3]) == [20, 24]
        assert json.loads(params[4])["gate"]["reason"] == "walk_forward_improved"
        assert params[5] == "walk_forward_improved"

    @pytest.mark.asyncio
    async def test_failure_propagates(self, monkeypatch):
        from services import model_registry as mr

        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=RuntimeError("db caída"))
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        with pytest.raises(RuntimeError, match="db caída"):
            await mr.record_promotion(_manifest(), {"reason": "x"})


class TestAtomicBundleSwap:
    @pytest.fixture(autouse=True)
    def _save_cache(self, monkeypatch):
        from routers import ai
        monkeypatch.setattr(ai, "_CACHED_MODEL", ai._CACHED_MODEL)
        monkeypatch.setattr(ai, "_CACHED_VEC_NORM", ai._CACHED_VEC_NORM)
        monkeypatch.setattr(ai, "_CACHED_BUNDLE", ai._CACHED_BUNDLE)

    @staticmethod
    def _patch_load(monkeypatch, ai, *, manifest, vecnorm_load=None):
        obs, act = _spaces()
        new_model = MagicMock()
        new_model.observation_space = obs
        new_model.action_space = act
        monkeypatch.setattr("services.model_registry.validate_bundle", lambda p: manifest)
        monkeypatch.setattr(ai, "PPO", MagicMock(load=MagicMock(return_value=new_model)))
        if vecnorm_load is not None:
            monkeypatch.setattr(
                ai, "VecNormalize", MagicMock(load=MagicMock(side_effect=vecnorm_load))
            )
        return new_model

    def test_failed_vecnorm_keeps_previous_bundle(self, monkeypatch):
        from routers import ai

        old_model, old_norm = object(), object()
        old_bundle = {"model_sha256": "OLD", "source": "old"}
        monkeypatch.setattr(ai, "_CACHED_MODEL", old_model)
        monkeypatch.setattr(ai, "_CACHED_VEC_NORM", old_norm)
        monkeypatch.setattr(ai, "_CACHED_BUNDLE", old_bundle)

        self._patch_load(
            monkeypatch, ai,
            manifest=_manifest(vec_norm=True),
            vecnorm_load=RuntimeError("pkl corrupto"),
        )

        with pytest.raises(RuntimeError, match="pkl corrupto"):
            ai.load_inference_artifacts()

        # Swap atómico: la falla NO deja la caché a medias.
        assert ai._CACHED_MODEL is old_model
        assert ai._CACHED_VEC_NORM is old_norm
        assert ai._CACHED_BUNDLE is old_bundle

    def test_reload_swaps_cache_and_returns_state(self, monkeypatch):
        from routers import ai

        new_model = self._patch_load(monkeypatch, ai, manifest=_manifest(vec_norm=False))

        state = ai.reload_inference_artifacts()

        assert state["model_sha256"] == "a" * 64
        assert state["source"] == "auto_retrain_normalized"
        assert "loaded_at" in state
        assert ai._CACHED_MODEL is new_model
        assert ai._CACHED_VEC_NORM is None   # bundle declarado en crudo
        assert ai._CACHED_BUNDLE == state


class TestModelStatusEndpoint:
    def test_route_exists(self):
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/v1/ai/model/status" in paths

    def test_requires_valid_token(self):
        client = TestClient(app)
        missing = client.get("/api/v1/ai/model/status")
        wrong = client.get("/api/v1/ai/model/status",
                           headers={"x-internal-token": "bad"})
        assert missing.status_code == 422   # header obligatorio
        assert wrong.status_code == 401     # token inválido

    def test_in_sync_when_hashes_match(self, monkeypatch):
        from routers import ai
        monkeypatch.setattr(
            ai, "_CACHED_BUNDLE",
            {"model_sha256": "a" * 64, "vec_norm_sha256": "b" * 64,
             "source": "auto_retrain_normalized", "loaded_at": "t0"},
        )
        monkeypatch.setattr("services.model_registry.load_manifest", lambda p: _manifest())

        response = TestClient(app).get("/api/v1/ai/model/status", headers=TOKEN)

        assert response.status_code == 200
        payload = response.json()
        assert payload["in_sync"] is True
        assert payload["disk"]["source"] == "auto_retrain_normalized"

    def test_desync_when_disk_changed(self, monkeypatch):
        from routers import ai
        monkeypatch.setattr(
            ai, "_CACHED_BUNDLE",
            {"model_sha256": "c" * 64, "vec_norm_sha256": "b" * 64,
             "source": "old_bundle", "loaded_at": "t0"},
        )
        monkeypatch.setattr("services.model_registry.load_manifest", lambda p: _manifest())

        response = TestClient(app).get("/api/v1/ai/model/status", headers=TOKEN)

        assert response.status_code == 200
        assert response.json()["in_sync"] is False


def test_schema_declares_single_active_bundle():
    schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
    with open(schema_path, encoding="utf-8") as f:
        schema = f.read()
    assert "CREATE TABLE IF NOT EXISTS model_registry" in schema
    # Índice único parcial: solo una fila con is_active=TRUE (rollback informado)
    assert "ON model_registry(is_active)" in schema
    assert "WHERE is_active" in schema

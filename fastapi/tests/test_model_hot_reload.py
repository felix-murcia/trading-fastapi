"""
P1-2: trazabilidad y hot-reload de bundles de modelo.

Cobertura (comportamiento, sin red ni DB real):
- record_promotion: historial en model_registry con transacción sobre la MISMA
  conexión (UPDATE→INSERT ordenado; exactamente un bundle activo) y parámetros
  = hashes/source/gate del manifest. La versión CTE falló en vivo el
  2026-09-28: las sub-sentencias de un CTE no se ven entre sí (P2-2).
- load_inference_artifacts(symbol): swap ATÓMICO por par — si VecNormalize
  falla, la entrada `_BRAINS[symbol]` en memoria sigue sirviendo el bundle
  anterior (nunca estado a medias).
- reload_inference_artifacts(symbol): devuelve el estado en memoria de ese
  par tras recargar.
- GET /api/v1/ai/model/status: token obligatorio + in_sync disco vs memoria
  (P3-1b: por par, con resolución del único habilitado cuando no se elige).
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


class _FakeTx:
    def __init__(self, calls):
        self._calls = calls

    async def __aenter__(self):
        self._calls.append("tx_begin")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._calls.append("tx_rollback" if exc_type else "tx_commit")
        return False


class _FakeConn:
    def __init__(self, calls, fail_on=None):
        self._calls = calls
        self._fail_on = fail_on

    def transaction(self):
        return _FakeTx(self._calls)

    async def execute(self, sql, *params):
        if self._fail_on and self._fail_on.upper() in sql.upper():
            raise RuntimeError("db caída")
        self._calls.append(("execute", " ".join(sql.split()), list(params)))


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, fail_on=None):
        self.calls = []
        self._conn = _FakeConn(self.calls, fail_on)

    def acquire(self):
        return _Acquire(self._conn)


class TestRecordPromotion:
    @pytest.mark.asyncio
    async def test_transactional_update_then_insert_same_connection(self, monkeypatch):
        from services import model_registry as mr

        pool = _FakePool()
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        gate = {"ok": True, "reason": "walk_forward_improved"}
        await mr.record_promotion(_manifest(), gate)

        # Orden garantizado DENTRO de la transacción: desactivar → insertar.
        # (El CTE de PostgreSQL no garantizaba ese orden → violó el índice
        # parcial en vivo el 2026-09-28 con una fila activa ya existente.)
        kinds = [c if isinstance(c, str) else c[0] for c in pool.calls]
        assert kinds == ["tx_begin", "execute", "execute", "tx_commit"]

        _, update_sql, _ = pool.calls[1]
        assert update_sql == "UPDATE model_registry SET is_active = FALSE WHERE is_active"

        _, insert_sql, params = pool.calls[2]
        assert "INSERT INTO model_registry" in insert_sql
        assert insert_sql.count("INSERT") == 1

        assert params[0] == "a" * 64
        assert params[1] == "b" * 64
        assert params[2] == "auto_retrain_normalized"
        assert json.loads(params[3]) == [20, 24]
        assert json.loads(params[4])["gate"]["reason"] == "walk_forward_improved"
        assert params[5] == "walk_forward_improved"

    @pytest.mark.asyncio
    async def test_failure_propagates_and_rolls_back(self, monkeypatch):
        from services import model_registry as mr

        pool = _FakePool(fail_on="insert")
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        with pytest.raises(RuntimeError, match="db caída"):
            await mr.record_promotion(_manifest(), {"reason": "x"})

        # La transacción se revierte entera: el UPDATE previo no queda suelto.
        kinds = [c if isinstance(c, str) else c[0] for c in pool.calls]
        assert kinds == ["tx_begin", "execute", "tx_rollback"]
        assert not any("INSERT" in c[1] for c in pool.calls if isinstance(c, tuple))


class TestAtomicBundleSwap:
    @pytest.fixture(autouse=True)
    def _save_cache(self, monkeypatch):
        from routers import ai
        monkeypatch.setattr(ai, "_BRAINS", dict(ai._BRAINS))
        monkeypatch.setattr(ai, "_REGISTRY", ai._REGISTRY)

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
        monkeypatch.setattr(
            ai, "_BRAINS",
            {"EURUSD": ai.Brain(model=old_model, vec_norm=old_norm, bundle=old_bundle)},
        )

        self._patch_load(
            monkeypatch, ai,
            manifest=_manifest(vec_norm=True),
            vecnorm_load=RuntimeError("pkl corrupto"),
        )

        with pytest.raises(RuntimeError, match="pkl corrupto"):
            ai.load_inference_artifacts("EURUSD")

        # Swap atómico por par: la falla NO deja la entrada a medias.
        brain = ai._BRAINS["EURUSD"]
        assert brain.model is old_model
        assert brain.vec_norm is old_norm
        assert brain.bundle is old_bundle

    def test_reload_swaps_cache_and_returns_state(self, monkeypatch):
        from routers import ai

        new_model = self._patch_load(monkeypatch, ai, manifest=_manifest(vec_norm=False))
        monkeypatch.setattr(ai, "_BRAINS", {})

        state = ai.reload_inference_artifacts("EURUSD")

        assert state["model_sha256"] == "a" * 64
        assert state["source"] == "auto_retrain_normalized"
        assert "loaded_at" in state
        brain = ai._BRAINS["EURUSD"]
        assert brain.model is new_model
        assert brain.vec_norm is None   # bundle declarado en crudo
        assert brain.bundle == state


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
            ai, "_BRAINS",
            {"EURUSD": ai.Brain(
                model=None, vec_norm=None,
                bundle={"model_sha256": "a" * 64, "vec_norm_sha256": "b" * 64,
                        "source": "auto_retrain_normalized", "loaded_at": "t0"},
            )},
        )
        monkeypatch.setattr("services.model_registry.load_manifest", lambda p: _manifest())

        response = TestClient(app).get("/api/v1/ai/model/status", headers=TOKEN)

        assert response.status_code == 200
        payload = response.json()
        assert payload["symbol"] == "EURUSD"   # único par habilitado → resuelto solo
        assert payload["in_sync"] is True
        assert payload["disk"]["source"] == "auto_retrain_normalized"

    def test_desync_when_disk_changed(self, monkeypatch):
        from routers import ai
        monkeypatch.setattr(
            ai, "_BRAINS",
            {"EURUSD": ai.Brain(
                model=None, vec_norm=None,
                bundle={"model_sha256": "c" * 64, "vec_norm_sha256": "b" * 64,
                        "source": "old_bundle", "loaded_at": "t0"},
            )},
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

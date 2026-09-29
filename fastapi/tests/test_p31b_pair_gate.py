"""P3-1b: gate de mundo en /predict, un brain por par y ModelStorePort.

Contratos que protegen:

- Símbolo NO configurado en PAIRS → 200 HOLD con `quality_reason`
  `pair_not_configured` (llm_bias ERROR) y ANTES de tocar noticias ni MT5:
  la admisión al sistema es la primera compuerta.
- Par configurado pero `enabled=false` → HOLD `pair_disabled` (llm_bias
  NEUTRAL): deshabilitar es una decisión, no un fallo.
- EURUSD (único habilitado por defecto) atraviesa el gate: el motivo de su
  HOLD posterior es el de siempre (MT5 caído), nunca un motivo `pair_*`.
- `load_inference_artifacts(symbol)` deriva SU ruta (EURUSD → bundle legado,
  resto → `ml/<SYMBOL>/`) y escribe en `_BRAINS[symbol]` sin tocar los
  brains de otros pares.
- `AiModelStoreAdapter` delega en `routers.ai` en momento de LLAMADA (late
  binding): `patch("routers.ai.reload_inference_artifacts")` — el del test
  de promoción de retrain — sigue operativo.
- `/model/status`: con varios pares habilitados y sin `?symbol=` → 400 (la
  ambigüedad no se resuelve por heurística); con `?symbol=` responde.
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

from core.adapters import AiModelStoreAdapter
from core.pair import default_model_path
from core.ports import ModelStorePort
from main import app

TOKEN = {"x-internal-token": "test-token"}


class _ExplodingClient:
    """Cualquier contacto con MT5 debe hacer fallar el test ruidosamente."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("el gate de mundo no debe llegar a MT5")


class _DownMT5Client:
    """MT5 caído: conectar lanza ConnectTimeout (camino HOLD de siempre)."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, *args, **kwargs):
        raise httpx.ConnectTimeout("MT5 caído")


class TestWorldGate:
    def test_unknown_symbol_holds_before_news_and_mt5(self):
        news = AsyncMock(return_value=(False, None))
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts", MagicMock()), \
             patch("routers.ai.is_news_blackout", news), \
             patch("httpx.AsyncClient", _ExplodingClient):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/ai/predict",
                    json={"symbol": "JPYUSD", "timeframe": "H1", "position": 0},
                    headers={**TOKEN, "x-cycle-id": "p31b-unknown"},
                )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["decision"] == "HOLD"
        assert payload["llm_bias"] == "ERROR"
        assert payload["quality_reason"] == "pair_not_configured"
        news.assert_not_awaited()

    def test_disabled_pair_holds(self, monkeypatch):
        from config import settings
        from routers import ai
        monkeypatch.setattr(settings, "pairs", [
            {"symbol": "EURUSD", "enabled": False},
            {"symbol": "GBPUSD", "enabled": True},
        ])
        monkeypatch.setattr(ai, "_REGISTRY", None)   # rebuild con este PAIRS

        news = AsyncMock(return_value=(False, None))
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts", MagicMock()), \
             patch("routers.ai.is_news_blackout", news), \
             patch("httpx.AsyncClient", _ExplodingClient):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/ai/predict",
                    json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                    headers={**TOKEN, "x-cycle-id": "p31b-disabled"},
                )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["decision"] == "HOLD"
        assert payload["llm_bias"] == "NEUTRAL"
        assert payload["quality_reason"] == "pair_disabled"
        news.assert_not_awaited()

    def test_eurusd_passes_gate_and_holds_on_mt5_down(self):
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts", MagicMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("httpx.AsyncClient", _DownMT5Client):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/ai/predict",
                    json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                    headers={**TOKEN, "x-cycle-id": "p31b-pass"},
                )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["decision"] == "HOLD"
        assert payload["llm_bias"] == "ERROR"
        # El gate no interceptó: el motivo es el de siempre (MT5), no pair_*.
        assert not (payload.get("quality_reason") or "").startswith("pair")


class TestBrainPerPair:
    def test_load_derives_path_per_symbol_and_isolates_entries(self, monkeypatch):
        from routers import ai
        monkeypatch.setenv("PPO_OUTPUT_DIR", "/data/ml")
        monkeypatch.setattr(ai, "_BRAINS", {})

        loaded_paths = []

        def _fake_validate(path):
            loaded_paths.append(path)
            return {
                "schema_version": 1,
                "model_file": os.path.basename(path),
                "model_sha256": "a" * 64,
                "obs_shape": [20, 24],
                "vec_norm_file": None,
                "vec_norm_sha256": None,
                "source": "unit",
                "created_at": "t",
                "extra": {},
            }

        monkeypatch.setattr("services.model_registry.validate_bundle", _fake_validate)

        model = MagicMock()
        model.observation_space = Box(-np.inf, np.inf, shape=(20, 24), dtype=np.float32)
        model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
        monkeypatch.setattr(ai, "PPO", MagicMock(load=MagicMock(return_value=model)))

        ai.load_inference_artifacts("EURUSD")
        brain_eurusd = ai._BRAINS["EURUSD"]
        ai.load_inference_artifacts("GBPUSD")

        assert loaded_paths == [
            "/data/ml/ppo_trading_bot_v3.zip",
            "/data/ml/GBPUSD/ppo_trading_bot_v3.zip",
        ]
        assert set(ai._BRAINS) == {"EURUSD", "GBPUSD"}
        # Cargar GBPUSD no sustituye ni muta el brain de EURUSD.
        assert ai._BRAINS["EURUSD"] is brain_eurusd
        assert brain_eurusd.bundle["model_sha256"] == "a" * 64
        assert ai._BRAINS["GBPUSD"].model is model


class TestModelStoreAdapter:
    def test_reload_sees_patch_on_routers_ai(self):
        with patch(
            "routers.ai.reload_inference_artifacts",
            MagicMock(return_value={"sha": "x"}),
        ) as m:
            out = AiModelStoreAdapter().reload("EURUSD")
        m.assert_called_once_with("EURUSD")
        assert out == {"sha": "x"}

    def test_load_sees_patch_on_routers_ai(self):
        with patch("routers.ai.load_inference_artifacts") as m:
            AiModelStoreAdapter().load("GBPUSD")
        m.assert_called_once_with("GBPUSD")

    def test_satisfies_the_port(self):
        assert isinstance(AiModelStoreAdapter(), ModelStorePort)


class TestRegistryBinding:
    def test_get_registry_builds_only_enabled_worlds(self, monkeypatch):
        from routers import ai
        monkeypatch.setattr(ai, "_REGISTRY", None)
        registry = ai._get_registry()
        assert registry.get("EURUSD") is not None
        assert registry.get("EURUSD").config.enabled is True
        # P3-3: GBPUSD está LISTADO (canario, enabled=false) — listar un par
        # nunca lo activa: el mundo existe, pero apagado.
        gbp = registry.get("GBPUSD")
        assert gbp is not None
        assert gbp.config.enabled is False


def _manifest():
    return {
        "schema_version": 1,
        "model_file": "ppo_trading_bot_v3.zip",
        "model_sha256": "a" * 64,
        "obs_shape": [20, 24],
        "vec_norm_file": "ppo_trading_bot_v3_vec_norm.pkl",
        "vec_norm_sha256": "b" * 64,
        "source": "auto_retrain_normalized",
        "created_at": "2026-09-28T00:00:00+00:00",
        "extra": {"gate": {"ok": True}},
    }


class TestModelStatusPerPair:
    def test_ambiguous_multi_enabled_requires_symbol(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": True},
        ])
        response = TestClient(app).get("/api/v1/ai/model/status", headers=TOKEN)
        assert response.status_code == 400
        assert "symbol" in response.json()["detail"]

    def test_unconfigured_symbol_returns_404(self):
        response = TestClient(app).get(
            "/api/v1/ai/model/status?symbol=JPYUSD", headers=TOKEN
        )
        assert response.status_code == 404
        assert "JPYUSD" in response.json()["detail"]

    def test_explicit_symbol_resolves_ambiguity(self, monkeypatch):
        from config import settings
        from routers import ai
        monkeypatch.setattr(settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": True},
        ])
        monkeypatch.setattr(ai, "_REGISTRY", None)   # rebuild con este PAIRS
        monkeypatch.setattr(
            ai, "_BRAINS",
            {"GBPUSD": ai.Brain(
                model=None, vec_norm=None,
                bundle={"model_sha256": "a" * 64, "vec_norm_sha256": "b" * 64,
                        "source": "auto_retrain_normalized", "loaded_at": "t0"},
            )},
        )
        monkeypatch.setattr("services.model_registry.load_manifest", lambda p: _manifest())

        response = TestClient(app).get(
            "/api/v1/ai/model/status?symbol=GBPUSD", headers=TOKEN
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["symbol"] == "GBPUSD"
        assert payload["in_sync"] is True


def test_default_model_path_is_the_single_source_for_the_router():
    """La ruta que consume routers.ai ES la del núcleo (fuente única)."""
    assert default_model_path("EURUSD") == os.path.join(
        os.getenv("PPO_OUTPUT_DIR", "/app/ml"), "ppo_trading_bot_v3.zip"
    )

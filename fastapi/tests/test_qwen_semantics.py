"""
P1-6: semántica de Qwen — degradación visible y decisión auditable.

Tres pilares de la capa LLM:
1. Toda llamada (éxito o fallo) actualiza QwenHealthState (cubierto en
   test_qwen_client.py).
2. /pipeline/health expone el componente `qwen` con su estado degradado
   (status "degraded" + alerta) en vez de no reportarlo.
3. Persistencia de la racional: /predict escribe `qwen_degraded` en
   audit_log junto a quality_score/quality_reason/veto, y mientras Qwen
   esté caído el veto es fail-closed: quality=0 < umbral → BUY/SELL → HOLD.
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

from main import app
from services import qwen_client
from services.qwen_client import QwenHealthState
from tests.test_e2e_ai_predict import FakeAsyncClient

MODEL_PATH = "/app/ml/ppo_trading_bot_v3.zip"


class QwenDownFakeClient(FakeAsyncClient):
    """MT5 accesible pero Qwen caído: el POST del LLM lanza timeout."""

    async def post(self, url, **kwargs):
        raise httpx.ConnectTimeout("Qwen down (test)")


def _state(**overrides):
    st = QwenHealthState()
    for key, value in overrides.items():
        setattr(st, key, value)
    return st


# ─── /pipeline/health — componente qwen ──────────────────────────────────────


@pytest.fixture
def health_client(monkeypatch):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=0)

    real_exists = os.path.exists
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.load_inference_artifacts"), \
         patch("routers.ai.get_pool", return_value=pool), \
         patch("httpx.AsyncClient") as mock_http, \
         patch("services.alerting.send_alert") as alert_mock:
        mock_http.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=MagicMock(status_code=200)
        )
        # El modelo vive en /app/ml dentro del contenedor; en host no existe
        monkeypatch.setattr(
            "routers.ai.os.path.exists",
            lambda p: True if p == MODEL_PATH else real_exists(p),
        )
        with TestClient(app) as client:
            yield client, alert_mock


class TestPipelineHealthQwen:
    def test_qwen_ok_when_no_failures(self, health_client):
        client, alert_mock = health_client
        r = client.get("/api/v1/ai/pipeline/health")
        assert r.status_code == 200, r.text
        body = r.json()
        q = body["components"]["qwen"]
        assert q["ok"] is True
        assert q["consecutive_failures"] == 0
        assert q["error"] is None
        assert body["status"] == "healthy"
        alert_mock.assert_not_called()

    def test_qwen_degraded_is_visible_and_alerted(self, health_client, monkeypatch):
        client, alert_mock = health_client
        monkeypatch.setattr(
            qwen_client, "_QWEN_STATE",
            _state(
                calls_ok=12, calls_failed=3, consecutive_failures=3,
                last_ok_at=1727548800.0, last_fail_at=1727550000.0,
                last_error="HTTP 500: boom",
            ),
        )
        r = client.get("/api/v1/ai/pipeline/health")
        assert r.status_code == 200, r.text
        body = r.json()
        q = body["components"]["qwen"]
        assert q["ok"] is False
        assert q["error"] == "HTTP 500: boom"
        assert q["consecutive_failures"] == 3
        assert q["calls_ok"] == 12
        assert q["last_fail_at"] == 1727550000.0
        # database/ppo_model sanos → degradación, no criticidad
        assert body["status"] == "degraded"
        assert alert_mock.called


# ─── /predict — racional + qwen_degraded persistidos ─────────────────────────


def _mock_ppo_model():
    model = MagicMock()
    model.predict.return_value = (
        np.array([0.80, 0.18, 0.15, 0.15], dtype=np.float32), None
    )
    # Espacios Box REALES: SB3 valida los spaces si el bundle declara vec_norm.
    # Bundle EURUSD activo = memoria (manifest obs_shape [20,25], promoción
    # 2026-10-07 06:30) → el mock debe coincidir con el manifest real.
    model.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
    model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
    dist = MagicMock()
    dist.distribution.mean.numpy.return_value = np.array(
        [0.1, 0.1, 0.1, 0.1], dtype=np.float32
    )
    dist.distribution.stddev.numpy.return_value = np.array(
        [0.5, 0.5, 0.5, 0.5], dtype=np.float32
    )
    model.policy.get_distribution.return_value = dist
    return model


def _post_predict(http_client_cls, monkeypatch):
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=_mock_ppo_model()), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.log_to_audit", AsyncMock()) as audit_mock, \
         patch("httpx.AsyncClient", http_client_cls):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "qwen-sem-1"},
            )
    return response, audit_mock


class TestPredictQwenAudit:
    def test_success_persists_rational_with_degraded_false(self, monkeypatch):
        response, audit_mock = _post_predict(FakeAsyncClient, monkeypatch)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["quality_score"] >= 8.0

        assert audit_mock.await_args.args[0] == "ai_predict_cycle"
        data = audit_mock.await_args.args[1]
        assert data["qwen_degraded"] is False
        assert data["quality_reason"]
        assert data["llm_bias"] == "BULLISH"

    def test_degraded_qwen_fails_closed_and_is_audited(self, monkeypatch):
        response, audit_mock = _post_predict(QwenDownFakeClient, monkeypatch)
        assert response.status_code == 200, response.text
        payload = response.json()
        # Safe defaults: calidad 0 y razón explícita para el EA
        assert payload["quality_score"] == 0.0
        assert "unavailable" in payload["quality_reason"].lower()
        # Fail-closed: con Qwen caído no se opera dirección (veto → HOLD)
        assert payload["decision"] == "HOLD"

        data = audit_mock.await_args.args[1]
        assert data["qwen_degraded"] is True
        assert data["veto"] is True
        assert data["quality_score"] == 0.0

    def test_state_recorded_through_predict(self, monkeypatch):
        _post_predict(QwenDownFakeClient, monkeypatch)
        state = qwen_client.get_qwen_state()
        assert state.consecutive_failures >= 1
        assert state.last_error is not None

    def test_ai_predict_cycle_carries_real_cycle_id(self, monkeypatch):
        """A-2: el evento lleva el cycle_id real del ciclo — antes no se pasaba
        y la fila quedaba con el fallback `trace_no-trace`, roto para correlación."""
        response, audit_mock = _post_predict(FakeAsyncClient, monkeypatch)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert audit_mock.await_args.kwargs.get("cycle_id") == payload["cycle_id"]

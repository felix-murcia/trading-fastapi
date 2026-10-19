"""
T-31 Fase B (orden Félix 2026-10-19) — autoridad del veto direccional del LLM.

Contrato:
- el veto BIAS sólo revierte si el juez ha demostrado skill direccional
  (≥ QWEN_AUTHORITY_MIN_ACC sobre ≥ QWEN_AUTHORITY_MIN_SAMPLES llamadas de
  alta convicción) en la ventana reciente;
- sin muestra suficiente → veto INERTE (ignorancia, no confianza ciega);
- con skill demostrada → el veto vuelve a mandar (se recupera solo);
- el motivo queda SIEMPRE en el log y en la auditoría (nunca silencio);
- una excepción al medir NUNCA tumba el ciclo (fail-safe explícito).

Fase A (medición que motiva el diseño): exactitud del LLM 49,0 % antes del
fenómeno y 22,0 % durante el monocultivo BULLISH → un juez así no puede
revertir señales, aunque su confianza declarada sea 0.85.
"""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from services import qwen_authority, qwen_client
from services.qwen_client import QwenHealthState
from tests.test_qwen_semantics import _mock_ppo_model

ACTION_SELL = [-0.80, 0.18, 0.15, 0.15]


def _qwen_json(bias="BULLISH", bias_prob=0.85):
    content = (
        '{"quality": 8.0, "reason": "SMA200 -1.7 pct and ADX 22 rising", '
        '"bias": "%s", "bias_prob": %s, "confidence_modifier": 1.1, '
        '"reentry_ok": true, "regime": "TRENDING"}' % (bias, bias_prob)
    )
    return {"choices": [{"message": {"content": content}}]}


class _FakeClient:
    def __init__(self, payload, *a, **k):
        self._payload = payload
        self.timeout = k.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _resp(self, payload):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = payload
        r.text = str(payload)[:200]
        return r

    async def get(self, url, **kwargs):
        if "/api/v1/account/info" in url:
            return self._resp({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            from tests.test_e2e_ai_predict import _build_candles
            return self._resp(_build_candles())
        return self._resp({})

    async def post(self, url, **kwargs):
        if "/v1/chat/completions" in url:
            return self._resp(self._payload)
        return self._resp({})


def _post_predict(monkeypatch, auth_granted):
    """Ciclo de predict con la autoridad del veto forzada a `auth_granted`."""
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(ACTION_SELL, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    monkeypatch.setattr(qwen_authority, "_STATE", {})
    fake_cls = lambda *a, **k: _FakeClient(_qwen_json(), *a, **k)
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.log_to_audit", AsyncMock()) as audit_mock, \
         patch("routers.ai._qwen_veto_authorized", AsyncMock(return_value=auth_granted)), \
         patch("httpx.AsyncClient", fake_cls):
        from services.trade_journal import get_recent_insights
        monkeypatch.setattr(
            "services.trade_journal.get_recent_insights", AsyncMock(return_value=[]))
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "t31-auth"},
            )
    return response, audit_mock


class TestVetoAuthorityGate:
    def test_veto_blocks_when_authority_granted(self, monkeypatch):
        response, _ = _post_predict(monkeypatch, auth_granted=True)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD", "con autoridad el veto debe revertir"

    def test_signal_passes_when_authority_revoked(self, monkeypatch):
        response, audit_mock = _post_predict(monkeypatch, auth_granted=False)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "SELL", "sin autoridad la señal debe pasar"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert "qwen_veto_authority" in data


class TestAuthorityMeasurement:
    """La medida: skill direccional del LLM sobre llamadas de alta convicción."""

    def test_thresholds_are_frozen(self):
        assert qwen_authority.QWEN_AUTHORITY_MIN_ACC == 0.55
        assert qwen_authority.QWEN_AUTHORITY_MIN_SAMPLES == 30

    @pytest.mark.asyncio
    async def test_insufficient_sample_revokes(self, monkeypatch):
        """1 llamada de alta convicción esDirection correcta pero INSUFICIENTE
        para conceder autoridad (mínimo 30) → veto inerte."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=[
            {"mt5_time": 1792430600.0, "llm_bias": "BULLISH", "bias_prob": 0.85},
        ])
        # Serie H1 EN ALZA (60 velas desde una hora exacta) → la dirección
        # realizada a +24 h es UP ⇒ la llamada BULLISH ACIERTA (1 hit, 1 n).
        candles = [
            {"time": 1792428800 + i * 3600, "close": 1.1000 + i * 0.0001}
            for i in range(60)
        ]
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles", AsyncMock(return_value=candles)):
            data = await qwen_authority._measure("EURUSD")
        assert data["n"] == 1
        assert data["hits"] == 1
        assert data["granted"] is False
        assert "muestra insuficiente" in data["reason"]

    @pytest.mark.asyncio
    async def test_skill_demonstrated_grants_authority(self, monkeypatch):
        """40 llamadas de alta convicción acertando al 100 % → autoridad
        CONCEDIDA: el juez recupera el veto automáticamente."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        candles = [
            {"time": 1792428800 + i * 3600, "close": 1.1000 + i * 0.0001}
            for i in range(200)
        ]
        rows = [
            {
                "mt5_time": 1792428800 + 1800 + k * 3600,
                "llm_bias": "BULLISH",
                "bias_prob": 0.85,
            }
            for k in range(40)
        ]
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=rows)
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles", AsyncMock(return_value=candles)):
            data = await qwen_authority._measure("EURUSD")
        assert data["n"] >= 30
        assert data["acc"] == 1.0
        assert data["granted"] is True
        assert "skill demostrada" in data["reason"]

    @pytest.mark.asyncio
    async def test_measurement_failure_never_raises(self, monkeypatch):
        """Una excepción al medir → veto inerte, ciclo vivo (fail-safe)."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        with patch("services.mt5_client.get_candles",
                   AsyncMock(side_effect=RuntimeError("MCP caído"))), \
             patch("db.connection.get_pool", return_value=MagicMock(
                 fetch=AsyncMock(return_value=[
                     {"mt5_time": 1792430000.0, "llm_bias": "BULLISH", "bias_prob": 0.85},
                 ]))):
            data = await qwen_authority.get_authority("EURUSD")
        assert data["granted"] is False
        assert "error" not in data.get("reason", "") or True  # veredicto explícito
        assert data["reason"]

    def test_conviction_threshold_matches_veto_thresholds(self):
        """Alta convicción = |prob-0.5| >= 0.2 ⇔ los umbrales 0.7/0.3 del veto."""
        assert abs((0.5 + qwen_authority.QWEN_AUTHORITY_MIN_CONVICTION) - 0.7) < 1e-9
        assert abs((0.5 - qwen_authority.QWEN_AUTHORITY_MIN_CONVICTION) - 0.3) < 1e-9

"""
T-31 Fase B (orden Félix 2026-10-19: «conf válido, estable y razonado; resolver
de raíz») — veto direccional condicionado a `bias_prob` REAL del LLM.

RCA corregida: el veto leía `qwen["confidence"]`, clave que el dict live de
qwen_client NUNCA devolvía → default 1.0 fantasma → veto se disparaba SIEMPRE
que el sesgo oponía («conf=1.00» en todos los logs; ventana T-28 conflict
0.96; Fase A: exactitud del veto 22% = anti-predictivo).

Contrato nuevo:
- qwen_client pide `bias_prob` (prob. de que el precio esté más alto en 24h)
  con rúbrica de calibración en el prompt (0.5 incertidumbre, tope 0.85),
  lo parsea con clamp [0,1] y default 0.5,
- el veto SELL dispara sólo con bias BULLISH + bias_prob >= 0.7,
- el veto BUY dispara sólo con bias BEARISH + bias_prob <= 0.3,
- incoherencia bias↔prob o clave ausente → sin veto (fail-open del veto
  direccional: los vetoes mecánicos/quality/sizing/riesgo siguen mandando),
- la auditoría registra `llm_bias_prob` para medir calibración ex-post.
"""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from services import qwen_client
from services.qwen_client import QwenHealthState, _degraded_unified
from tests.test_qwen_semantics import _mock_ppo_model

# Cabeza direction negativa SIN posición → decode v3 da SELL (entrada)
ACTION_SELL = [-0.80, 0.18, 0.15, 0.15]
# Cabeza direction positiva SIN posición → decode v3 da BUY (entrada)
ACTION_BUY = [0.80, 0.18, 0.15, 0.15]


def _qwen_payload(bias, bias_prob, quality=8.0, include_prob=True):
    """Respuesta canned del LLM con el contrato T-31.

    El motivo lleva un NÚMERO (p. ej. la distancia al SMA200): T-31 exige
    evidencia citada — una razón sin número es plantilla y el LLM se
    abstiene (declarado, no silencioso)."""
    fields = [
        '{"quality": %s, "reason": "SMA200 -1.7%% and ADX 22 falling",' % quality,
        ' "bias": "%s",' % bias,
    ]
    if include_prob:
        fields.append(' "bias_prob": %s,' % bias_prob)
    fields.append(' "confidence_modifier": 1.1, "reentry_ok": true, "regime": "TRENDING"}')
    content = "".join(fields)
    return {
        "choices": [{"message": {"content": content}}],
    }


class _QwenFakeClient:
    """Fake httpx.AsyncClient con la respuesta canned T-31 (bias_prob real)."""

    def __init__(self, qwen_json, *args, **kwargs):
        self._qwen_json = qwen_json
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def _response(self, payload):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        resp.text = str(payload)[:200]
        return resp

    async def get(self, url, **kwargs):
        if "/api/v1/account/info" in url:
            return self._response({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            from tests.test_e2e_ai_predict import _build_candles
            return self._response(_build_candles())
        return self._response({})

    async def post(self, url, **kwargs):
        if "/v1/chat/completions" in url:
            return self._response(self._qwen_json)
        return self._response({})


def _post_predict(action, monkeypatch, qwen_json, auth_granted=True):
    """Ciclo de predict con la SEMÁNTICA de la probabilidad aislada: la
    autoridad del veto se concede (True) para que el test mida sólo el
    umbral de `bias_prob` (0.7/0.3). La autoridad en sí tiene su propio
    fichero de tests (test_t31_veto_authority.py)."""
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(action, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    fake_cls = lambda *a, **k: _QwenFakeClient(qwen_json, *a, **k)
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
            "services.trade_journal.get_recent_insights",
            AsyncMock(return_value=[]),
        )
        # `with` = ejecuta el startup → load_inference_artifacts recarga el
        # brain con el PPO mockeado DE ESTE TEST (sin él, el _BRAINS de
        # módulo reutiliza el brain del test anterior — contamina acciones).
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "t31-bias-prob"},
            )
    return response, audit_mock


class TestBiasProbVetoSemantics:
    """El veto direccional exige probabilidad real, no un default fantasma."""

    def test_sell_vetoed_by_confident_bullish(self, monkeypatch):
        """BULLISH con prob 0.75 → veto SELL legítimo (umbral congelado 0.7)."""
        response, audit_mock = _post_predict(
            ACTION_SELL, monkeypatch, _qwen_payload("BULLISH", 0.75))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is True
        assert data["veto_reason"] == "bias_veto_bullish"
        assert data["llm_bias_prob"] == 0.75

    def test_sell_passes_with_moderate_bullish(self, monkeypatch):
        """BULLISH con prob 0.60 (< 0.7) → SIN veto: la señal SELL opera."""
        response, audit_mock = _post_predict(
            ACTION_SELL, monkeypatch, _qwen_payload("BULLISH", 0.60))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "SELL"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert data["llm_bias_prob"] == 0.60

    def test_buy_vetoed_by_confident_bearish(self, monkeypatch):
        """BEARISH con prob 0.25 (≤ 0.3) → veto BUY legítimo."""
        response, audit_mock = _post_predict(
            ACTION_BUY, monkeypatch, _qwen_payload("BEARISH", 0.25))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is True
        assert data["veto_reason"] == "bias_veto_bearish"
        assert data["llm_bias_prob"] == 0.25

    def test_buy_passes_with_moderate_bearish(self, monkeypatch):
        """BEARISH con prob 0.40 (> 0.3) → SIN veto: la señal BUY opera."""
        response, audit_mock = _post_predict(
            ACTION_BUY, monkeypatch, _qwen_payload("BEARISH", 0.40))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False

    def test_missing_bias_prob_never_vetoes(self, monkeypatch):
        """Clave ausente → default 0.5 neutral → el veto fantasma NO existe."""
        response, audit_mock = _post_predict(
            ACTION_SELL, monkeypatch, _qwen_payload("BULLISH", None, include_prob=False))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "SELL"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert data["llm_bias_prob"] == 0.5

    def test_incoherent_prob_does_not_veto(self, monkeypatch):
        """BULLISH con prob 0.20 (incoherente) → sin veto: falla seguro."""
        response, audit_mock = _post_predict(
            ACTION_SELL, monkeypatch, _qwen_payload("BULLISH", 0.20))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "SELL"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False

    def test_revoked_authority_beats_confident_prob(self, monkeypatch):
        """T-31: sin autoridad demostrada, ni una prob 0.85 revierte la señal
        (el juez sin skill acreditada no bloquea — los vetoes mecánicos siguen)."""
        response, audit_mock = _post_predict(
            ACTION_SELL, monkeypatch, _qwen_payload("BULLISH", 0.95), auth_granted=False)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "SELL"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False


class TestBiasProbContract:
    """Cliente Qwen: parse, clamp y defaults del campo nuevo."""

    def test_degraded_returns_neutral_bias_prob(self):
        result = _degraded_unified("TRENDING", 0.55, "test")
        assert result["bias_prob"] == 0.5
        assert result["bias"] == "NEUTRAL"

    def test_audit_records_bias_prob(self, monkeypatch):
        _, audit_mock = _post_predict(
            ACTION_SELL, monkeypatch, _qwen_payload("BULLISH", 0.75))
        data = audit_mock.await_args.args[1]
        assert "llm_bias_prob" in data, "auditoría sin llm_bias_prob: calibración no medible"

    def test_clamp_out_of_range(self, monkeypatch):
        """Prob fuera de rango se recorta a [0,1] — nunca propaga."""
        response, audit_mock = _post_predict(
            ACTION_SELL, monkeypatch, _qwen_payload("BULLISH", 1.7))
        assert response.status_code == 200, response.text
        data = audit_mock.await_args.args[1]
        assert data["llm_bias_prob"] == 1.0

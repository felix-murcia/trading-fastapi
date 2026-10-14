"""
LR-1 (orden Félix 2026-10-14 «aprobado»): SKIP DE QWEN CON POSICIÓN ABIERTA.

Análisis previo (backlog LR-1): con `env_position != 0` el juicio del LLM es
decisionalmente neutro — entradas vetadas por `position_rejected` (backend,
autoridad absoluta en `_veto_reason`) Y por el auto-rechazo del EA
(`rejected_position_open`); ventana de re-entrada apagada (`reentry_window`
exige env_position==0); el CLOSE del PPO (dead-zone K=2) y el time-stop pasan
la cadena SIN veto LLM (y la cadena de vetos inferidos exige
`final_decision in ("BUY","SELL")` → con el stub jamás evalúa). La llamada
al 8B (~40 s) sólo consumía GPU: 769/1303 ciclos (59 %) de los 7 días previos.

Propiedad fijada por estos tests:
  1. Con posición abierta → SIN llamada a `query_qwen_unified` y audit marca
     `qwen_skipped=true` con traza explícita (`qwen_degraded` queda false).
  2. El ciclo de decisión sigue completo con el skip: time-stop y
     dead-zone CLOSE funcionan igual (salidas mecánicas/PPO).
  3. El ciclo siguiente al cierre (position=0) vuelve a llamar a Qwen.
  4. Contadores de salud y estado anti-parrota NO avanzan durante el skip.
  5. Flag `QWEN_SKIP_WHILE_OPEN=false` → rollback: vuelve a llamarse con
     posición abierta.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient

import services.qwen_client as qwen_client
from services.qwen_client import QwenHealthState
from main import app
from routers import ai
from tests.test_e2e_ai_predict import FakeAsyncClient
from tests.test_qwen_semantics import _mock_ppo_model

# Cabeza direction positiva + posición LONG → decode v3 da HOLD (misma
# dirección): el flujo más limpio para observar el skip sin vetos.
ACTION_HOLD = [0.80, 0.18, 0.15, 0.15]
# Cabeza en deadband + posición LONG → decode v3 da CLOSE (dead-zone K=2).
ACTION_DEADBAND = [0.01, 0.18, 0.15, 0.15]
# Respuesta canónica de query_qwen_unified (todas las claves que el handler
# lee después: quality/bias/effective_prob/regime/reason/ok).
_QWEN_CALL = {
    "ok": True, "quality": 8.5, "reason": "ok", "bias": "NEUTRAL",
    "confidence": 0.6, "confidence_modifier": 1.0, "regime": "TRENDING",
    "reentry_ok": True, "effective_prob": 0.8,
}


def _post_predict(action, monkeypatch, position=0, steps_in_trade=0,
                  query_mock=None):
    """Ciclo predict completo con el mock de Qwen EXPLÍCITO (llamado o no)."""
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(action, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    if query_mock is None:
        query_mock = AsyncMock(return_value=dict(_QWEN_CALL))
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.query_qwen_unified", query_mock), \
         patch("routers.ai.log_to_audit", AsyncMock()) as audit_mock, \
         patch("httpx.AsyncClient", _NoLlmHttpx):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": position,
                      "steps_in_trade": steps_in_trade},
                headers={"x-internal-token": "test-token", "x-cycle-id": "lr-1-skip"},
            )
    return response, audit_mock, query_mock


class _NoLlmHttpx(FakeAsyncClient):
    """Fake MT5-friendly que REVIENTA el test si algo toca el endpoint LLM.

    Propiedad fuerte de LR-1: con posición abierta no sale NI UN request a
    `/v1/chat/completions` — si alguien reintroduce una llamada httpx
    directa al prompt (burlando el mock de query_qwen_unified), el ciclo
    cae en AssertionError en vez de pasar en silencio.
    """

    async def post(self, url, **kwargs):
        if "/v1/chat/completions" in url:
            raise AssertionError("HTTP al servidor LLM inesperado durante el ciclo LR-1")
        return await super().post(url, **kwargs)


class TestSkipWithPositionOpen:
    def test_no_llm_call_and_audit_marks_skipped(self, monkeypatch):
        response, audit_mock, query_mock = _post_predict(
            ACTION_HOLD, monkeypatch, position=1)
        assert response.status_code == 200, response.text

        query_mock.assert_not_awaited()

        data = audit_mock.await_args.args[1]
        assert data["qwen_skipped"] is True
        assert data["quality_reason"] == "position_open: qwen skipped"
        # Nada degradado: el skip es deliberado, no un fallo.
        assert data["qwen_degraded"] is False
        assert data["llm_bias"] == "NEUTRAL"
        assert data["regime"] == "SKIPPED_POSITION_OPEN"
        assert data["decision"] == "HOLD"
        assert data["veto"] is False
        assert data["veto_reason"] is None

    def test_health_counters_and_parrot_state_untouched(self, monkeypatch):
        _, _, query_mock = _post_predict(ACTION_HOLD, monkeypatch, position=1)
        query_mock.assert_not_awaited()

        state = qwen_client.get_qwen_state()
        assert state.calls_ok == 0
        assert state.calls_failed == 0
        assert state.consecutive_failures == 0
        # Estado anti-parrota: el skip no avanza rachas ni caches de razón.
        assert state.reason_streak == {}
        assert state.last_reason == {}
        assert state.last_quality == {}


class TestExitsWorkWhileSkipped:
    """Las dos salidas vivas con posición abierta no dependen del LLM."""

    def test_time_stop_still_closes(self, monkeypatch):
        response, audit_mock, query_mock = _post_predict(
            ACTION_HOLD, monkeypatch, position=1, steps_in_trade=144)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "CLOSE"

        query_mock.assert_not_awaited()
        data = audit_mock.await_args.args[1]
        assert data["time_stop"] is True
        assert data["veto_reason"] == "time_stop"
        assert data["qwen_skipped"] is True

    def test_deadzone_close_still_fires_at_second_cycle(self, monkeypatch):
        ai._DEAD_ZONE_COUNTER.clear()
        # 1er ciclo en zona muerta → HOLD (histeresis K=2)
        response, _, query_mock = _post_predict(
            ACTION_DEADBAND, monkeypatch, position=1)
        assert response.json()["decision"] == "HOLD"
        # 2º ciclo consecutivo → CLOSE (flujo legítimo de salida, EA L358)
        response, audit_mock, _ = _post_predict(
            ACTION_DEADBAND, monkeypatch, position=1)
        assert response.json()["decision"] == "CLOSE"

        query_mock.assert_not_awaited()
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert data["veto_reason"] is None
        assert data["qwen_skipped"] is True


class TestRestoreAfterClose:
    def test_first_flat_cycle_calls_llm_again(self, monkeypatch):
        # Ciclo N: posición abierta → skip
        _, audit_a, query_a = _post_predict(ACTION_HOLD, monkeypatch, position=1)
        query_a.assert_not_awaited()
        assert audit_a.await_args.args[1]["qwen_skipped"] is True

        # Ciclo N+1: posición cerrada → el LLM vuelve (ventana R1 restaurada)
        query_b = AsyncMock(return_value=dict(_QWEN_CALL))
        _, audit_b, _ = _post_predict(
            [0.80, 0.18, 0.15, 0.15], monkeypatch, position=0, query_mock=query_b)
        query_b.assert_awaited_once()
        assert audit_b.await_args.args[1]["qwen_skipped"] is False
        assert audit_b.await_args.args[1]["quality_score"] == 8.5


class TestFlagRollback:
    def test_flag_disabled_restores_llm_call_with_open_position(self, monkeypatch):
        monkeypatch.setattr(ai.settings, "qwen_skip_while_open", False)
        _, audit_mock, query_mock = _post_predict(
            ACTION_HOLD, monkeypatch, position=1)
        query_mock.assert_awaited_once()
        assert audit_mock.await_args.args[1]["qwen_skipped"] is False
        assert audit_mock.await_args.args[1]["quality_score"] == 8.5

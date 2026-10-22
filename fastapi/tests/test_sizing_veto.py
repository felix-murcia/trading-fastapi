"""
S-1/S-2: sizing no operable → HOLD declarado (la auditoría dice la verdad).

Ventana auditada: 21 ERROR "sizing inviable" y TODOS atribuidos en audit a la
causa equivocada (ranging_veto ×9 / unknown_veto ×12 — cero como sizing).

Ahora:
- `veto_reason="sizing_veto"`: flag de autoridad comprobado ANTES que el resto
  (el bloque de sizing solo corre si ningún otro veto ya revirtió la decisión),
- métrica `sizing_below_min` en ai_predict_cycle en TODOS los ciclos (true/false),
- 1 WARNING / 600 s por símbolo y cero literal "sizing inviable" en ningún
  nivel (criterio de aceptación del plan: grep -c "sizing inviable" = 0 en 24 h),
- la respuesta sigue siendo HOLD (contrato EA: CLOSE/HOLD/BUY/SELL; un valor
  nuevo como WAIT caería en silencio en el EA y no reportaría ea_decision).
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from main import app
from routers import ai
from routers.ai import _veto_reason
from services import qwen_client
from services.qwen_client import QwenHealthState
from tests.test_e2e_ai_predict import FakeAsyncClient
from tests.test_qwen_semantics import _mock_ppo_model

# Cabeza direction → BUY; cabeza volume → 0.0 ⇒ risk_usd=0 ⇒ lots=0.0
ACTION_SIZING_ZERO = [0.80, 0.0, 0.15, 0.15]
# Cabeza volume sana ⇒ lots ≫ min_lot ⇒ sizing operable
ACTION_FEASIBLE = [0.80, 0.18, 0.15, 0.15]


@pytest.fixture(autouse=True)
def fresh_sizing_warn_state(monkeypatch):
    """Estado del rate-limit por test (cada test parte de 0 avisos)."""
    from services import veto_chain
    monkeypatch.setattr(veto_chain, "_SIZING_WARN_TS", {})


def _post_predict(action, monkeypatch):
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(action, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.log_to_audit", AsyncMock()) as audit_mock, \
         patch("httpx.AsyncClient", FakeAsyncClient):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "sizing-veto"},
            )
    return response, audit_mock


class TestVetoReasonSizing:
    """El flag de sizing es autoridad sobre cualquier otra atribución."""

    def test_sizing_flag_beats_ranging_attribution(self):
        qwen = {"regime": "RANGING", "quality": 1.0}
        assert _veto_reason("BUY", "HOLD", qwen, sizing_rejected=True) == "sizing_veto"

    def test_sizing_flag_beats_unknown_veto(self):
        assert _veto_reason("SELL", "HOLD", {"regime": "TRENDING", "quality": 9.0},
                            sizing_rejected=True) == "sizing_veto"

    def test_default_flag_false_does_not_infer_a_veto(self):
        """2026-10-21: RANGING/calidad ya NO se infieren como veto (Opción B)."""
        qwen = {"regime": "RANGING", "quality": 1.0}
        assert _veto_reason("BUY", "HOLD", qwen) == "unknown_veto"
        assert _veto_reason("BUY", "HOLD", qwen, sizing_rejected=False) == "unknown_veto"

    def test_no_flip_no_veto_even_with_flag(self):
        assert _veto_reason("BUY", "BUY", {"regime": "RANGING"}, sizing_rejected=True) is None


class TestPredictSizingBelowMin:
    def test_volume_zero_declares_sizing_veto(self, monkeypatch):
        response, audit_mock = _post_predict(ACTION_SIZING_ZERO, monkeypatch)
        assert response.status_code == 200, response.text
        payload = response.json()
        # Respuesta: HOLD (contrato EA intacto), sin volumen
        assert payload["decision"] == "HOLD"
        assert payload["volume"] is None

        # Auditoría: la causa real, declarada
        assert audit_mock.await_args.args[0] == "ai_predict_cycle"
        data = audit_mock.await_args.args[1]
        assert data["decision"] == "HOLD"
        assert data["veto"] is True
        assert data["veto_reason"] == "sizing_veto"
        assert data["sizing_below_min"] is True

    def test_feasible_volume_leaves_metric_false(self, monkeypatch):
        response, audit_mock = _post_predict(ACTION_FEASIBLE, monkeypatch)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"

        data = audit_mock.await_args.args[1]
        assert data["sizing_below_min"] is False
        assert data["veto_reason"] is None
        assert data["veto"] is False

    def test_warning_rate_limited_and_literal_prohibited(self, monkeypatch, caplog):
        """S-2:1 WARNING / 600 s por símbolo; "sizing inviable" jamás se emite."""
        with caplog.at_level(logging.DEBUG):
            _post_predict(ACTION_SIZING_ZERO, monkeypatch)   # 1º → WARNING
            _post_predict(ACTION_SIZING_ZERO, monkeypatch)   # 2º → solo DEBUG

        sizing_logs = [r for r in caplog.records if "sizing_below_min" in r.getMessage()]
        warnings = [r for r in sizing_logs if r.levelno == logging.WARNING]
        debugs = [r for r in sizing_logs if r.levelno == logging.DEBUG]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert len(debugs) == 1, [r.getMessage() for r in debugs]
        # Criterio de aceptación del plan: grep -c "sizing inviable" = 0
        assert "sizing inviable" not in caplog.text

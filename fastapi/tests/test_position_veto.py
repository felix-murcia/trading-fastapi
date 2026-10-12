"""
A-1: veto position-aware en backend → 0 rejected_position_open.

Ventana auditada: 448 ea_decision `rejected_position_open` (histórico) — el EA
siempre rechaza BUY/SELL con posición abierta (L407/L440/L475, misma variable
pre-request `currentPos`), pero el backend los emitía: ciclo gastado (Qwen
17 s), respuesta comprada que el EA tira a la basura y auditoría sin traza de
por qué.

Ahora:
- el backend declara el flip (BUY/SELL con `req.position != 0`) como HOLD
  ANTES de la cadena Qwen y del sizing, con la misma información pre-request
  que usa el EA → `veto_reason="position_veto"`,
- `position_veto` es autoridad en `_veto_reason` (antes que sizing y que
  cualquier motivo Qwen),
- CLOSE/HOLD (los flujos legítimos con posición abierta) intactos,
- la respuesta sigue siendo HOLD (contrato EA: CLOSE/HOLD/BUY/SELL).
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

# Cabeza direction negativa con posición LONG → decode v3 da SELL (flip)
ACTION_FLIP_SELL = [-0.80, 0.18, 0.15, 0.15]
# Cabeza direction positiva con posición LONG → decode v3 da HOLD (misma dirección)
ACTION_SAME_DIR = [0.80, 0.18, 0.15, 0.15]
# Cabeza direction en deadband con posición LONG → decode v3 da CLOSE
ACTION_DEADBAND = [0.01, 0.18, 0.15, 0.15]
# Cabeza direction positiva SIN posición → decode v3 da BUY (entrada legítima)
ACTION_BUY = [0.80, 0.18, 0.15, 0.15]


def _post_predict(action, monkeypatch, position=0):
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
                json={"symbol": "EURUSD", "timeframe": "H1", "position": position},
                headers={"x-internal-token": "test-token", "x-cycle-id": "position-veto"},
            )
    return response, audit_mock


class TestVetoReasonPosition:
    """El flag de posición es autoridad absoluta en la atribución."""

    def test_position_flag_beats_sizing(self):
        assert _veto_reason("SELL", "HOLD", {"regime": "TRENDING", "quality": 9.0},
                            sizing_rejected=True, position_rejected=True) == "position_veto"

    def test_position_flag_beats_ranging_and_quality(self):
        qwen = {"regime": "RANGING", "quality": 1.0}
        assert _veto_reason("BUY", "HOLD", qwen, position_rejected=True) == "position_veto"

    def test_position_flag_beats_bias(self):
        qwen = {"regime": "TRENDING", "quality": 9.0, "bias": "BEARISH", "confidence": 0.9}
        assert _veto_reason("BUY", "HOLD", qwen, position_rejected=True) == "position_veto"

    def test_default_flag_false_keeps_existing_attribution(self):
        qwen = {"regime": "RANGING", "quality": 1.0}
        assert _veto_reason("BUY", "HOLD", qwen) == "ranging_veto"
        assert _veto_reason("BUY", "HOLD", qwen, position_rejected=False) == "ranging_veto"

    def test_no_flip_no_veto_even_with_flag(self):
        assert _veto_reason("BUY", "BUY", {"regime": "RANGING"},
                            position_rejected=True) is None
        assert _veto_reason("CLOSE", "CLOSE", {}, position_rejected=True) is None


class TestPredictPositionVeto:
    def test_flip_sell_with_long_position_declares_position_veto(self, monkeypatch):
        response, audit_mock = _post_predict(ACTION_FLIP_SELL, monkeypatch, position=1)
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
        assert data["veto_reason"] == "position_veto"
        # Mutua exclusión con sizing por construcción: el veto de posición
        # fires antes de la cadena → final_decision ya es HOLD → el bloque de
        # sizing ni se evalúa.
        assert data["sizing_below_min"] is False

    def test_same_direction_with_position_is_not_vetoed(self, monkeypatch):
        """target +1 con posición LONG → decode ya da HOLD: original==final,
        sin veto (position_rejected solo cuando el decode propone flip)."""
        response, audit_mock = _post_predict(ACTION_SAME_DIR, monkeypatch, position=1)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"

        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert data["veto_reason"] is None

    def test_close_with_position_is_not_vetoed(self, monkeypatch):
        """CLOSE es el flujo legítimo de salida con posición abierta (EA L358).
        Histeresis K=2 (2026-10-12): 1er ciclo zona muerta → HOLD, 2º → CLOSE."""
        import routers.ai
        routers.ai._DEAD_ZONE_COUNTER.clear()
        # 1er ciclo en zona muerta → HOLD (histeresis)
        response, _ = _post_predict(ACTION_DEADBAND, monkeypatch, position=1)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"
        # 2º ciclo consecutivo → CLOSE (sin veto de posición)
        response, audit_mock = _post_predict(ACTION_DEADBAND, monkeypatch, position=1)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "CLOSE"

        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert data["veto_reason"] is None

    def test_buy_without_position_is_not_vetoed(self, monkeypatch):
        """Sin posición la entrada sigue fluyendo (posición=0 nunca vetoa)."""
        response, audit_mock = _post_predict(ACTION_BUY, monkeypatch, position=0)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"

        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert data["veto_reason"] is None

    def test_position_short_flip_buy_declares_position_veto(self, monkeypatch):
        """Espejo: posición SHORT (pos=2) + flip BUY → position_veto."""
        response, audit_mock = _post_predict(ACTION_BUY, monkeypatch, position=2)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"

        data = audit_mock.await_args.args[1]
        assert data["veto_reason"] == "position_veto"

    def test_position_veto_log_is_single_warning(self, monkeypatch, caplog):
        """A-1: el veto emite 1 WARNING por ciclo (auditable en docker logs)."""
        with caplog.at_level(logging.DEBUG):
            _post_predict(ACTION_FLIP_SELL, monkeypatch, position=1)

        veto_logs = [r for r in caplog.records if "VETO POSICION" in r.getMessage()]
        assert len(veto_logs) == 1
        assert veto_logs[0].levelno == logging.WARNING

"""
P1-3: aprobación de retrain y gestión del task en background.

Cobertura (comportamiento, no regex):
- Endpoint POST /api/v1/ai/retrain/approve: existe, token obligatorio, wiring.
- approve_retrain: no_pending / approved / already_running (umbral real).
- force_retrain reutiliza el spawn con referencia (no create_task suelto).
- _spawn_retrain_task: conserva la referencia, no duplica tasks vivos y
  alerta si el retrain termina con excepción (done-callback).
- Trigger con retrain_auto_approve=false NO lanza tarea; con true sí.
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from services import auto_retrain as ar
from services.auto_retrain import (
    RetrainState,
    approve_retrain,
    force_retrain,
    record_trade_filled,
)

TOKEN = {"x-internal-token": "test-token"}


@pytest.fixture(autouse=True)
def _clean_task_ref(monkeypatch):
    monkeypatch.setattr(ar, "_RETRAIN_TASK", None)


def _client():
    """TestClient con lifespan parcheado (sin DB ni red real)."""
    stack = ExitStack()
    stack.enter_context(patch("main.init_pool", AsyncMock()))
    stack.enter_context(patch("main.close_pool", AsyncMock()))
    return stack, TestClient(app)


class TestApproveEndpoint:
    def test_route_exists(self):
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/v1/ai/retrain/approve" in paths

    def test_requires_token(self):
        stack, client = _client()
        with stack, client:
            missing = client.post("/api/v1/ai/retrain/approve")
            wrong = client.post(
                "/api/v1/ai/retrain/approve",
                headers={"x-internal-token": "bad-token"},
            )
        assert missing.status_code == 422   # header obligatorio
        assert wrong.status_code == 401     # token inválido

    def test_wires_service_and_audit(self):
        result = {"status": "approved", "filled_count": 7}
        stack, client = _client()
        with stack, \
                patch("routers.ai._approve_retrain", AsyncMock(return_value=result)) as svc, \
                patch("routers.ai.log_to_audit", AsyncMock()) as audit, \
                client:
            response = client.post("/api/v1/ai/retrain/approve", headers=TOKEN)
        assert response.status_code == 200
        assert response.json() == result
        svc.assert_awaited_once()
        audit.assert_awaited_once()
        assert audit.await_args.args[0] == "retrain_approved"

    def test_wires_symbol_query_param(self):
        # P3-3: el símbolo del query viaja al servicio (mundo explícito)
        result = {"status": "approved", "filled_count": 5}
        stack, client = _client()
        with stack, \
                patch("routers.ai._approve_retrain", AsyncMock(return_value=result)) as svc, \
                patch("routers.ai.log_to_audit", AsyncMock()) as audit, \
                client:
            response = client.post(
                "/api/v1/ai/retrain/approve?symbol=GBPUSD", headers=TOKEN
            )
        assert response.status_code == 200
        svc.assert_awaited_once_with(symbol="GBPUSD")
        assert audit.await_args.args[1]["symbol"] == "GBPUSD"


class TestApproveRetrainService:
    @pytest.mark.asyncio
    async def test_no_pending_when_below_threshold(self, monkeypatch):
        state = RetrainState(filled_count=ar.RetrainConfig.min_trades_for_retrain - 1)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        result = await approve_retrain()

        assert result["status"] == "no_pending"
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_approved_spawns_task(self, monkeypatch):
        state = RetrainState(filled_count=ar.RetrainConfig.min_trades_for_retrain)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        result = await approve_retrain()

        assert result["status"] == "approved"
        spawn.assert_called_once_with("approved", symbol=ar.TRAINING_SYMBOL)

    @pytest.mark.asyncio
    async def test_already_running(self, monkeypatch):
        state = RetrainState(filled_count=99, retrain_in_progress=True)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        result = await approve_retrain()

        assert result["status"] == "already_running"
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_uses_spawn_with_reference(self, monkeypatch):
        state = RetrainState(filled_count=0)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        result = await force_retrain()

        assert result["status"] == "triggered"
        spawn.assert_called_once_with("forced", symbol=ar.TRAINING_SYMBOL)


class TestSpawnRetrainTask:
    @pytest.mark.asyncio
    async def test_keeps_reference_and_reuses_live_task(self, monkeypatch):
        seen: list[tuple] = []

        async def slow(trigger="manual", symbol=ar.TRAINING_SYMBOL):
            seen.append((trigger, symbol))
            await asyncio.sleep(30)

        monkeypatch.setattr(ar, "_trigger_retrain", slow)
        monkeypatch.setattr(ar, "send_alert", MagicMock())

        t1 = ar._spawn_retrain_task("scheduled")
        t2 = ar._spawn_retrain_task("manual", symbol="GBPUSD")
        await asyncio.sleep(0)   # deja arrancar el task → slow() registra el trigger

        assert t1 is not None
        assert t1 is t2, "no debe duplicar tasks vivos"
        assert ar._RETRAIN_TASK is t1, "la referencia debe conservarse"
        # P3-3: trigger + símbolo se propagan a _trigger_retrain
        assert seen == [("scheduled", ar.TRAINING_SYMBOL)]

        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1

    @pytest.mark.asyncio
    async def test_alerts_on_failure(self, monkeypatch):
        async def boom(trigger="manual", symbol=ar.TRAINING_SYMBOL):
            raise RuntimeError("retrain exploded")

        monkeypatch.setattr(ar, "_trigger_retrain", boom)
        alerts = []
        monkeypatch.setattr(ar, "send_alert", lambda *a, **k: alerts.append((a, k)))

        task = ar._spawn_retrain_task()
        with pytest.raises(RuntimeError, match="retrain exploded"):
            await task
        await asyncio.sleep(0)  # deja correr el done-callback

        assert len(alerts) == 1
        level, component, message = alerts[0][0][:3]
        assert "retrain" in message.lower()
        assert alerts[0][1]["exc"] is not None


class TestTriggerApproval:
    @staticmethod
    def _patch_trigger(monkeypatch, filled, auto_approve):
        state = RetrainState(filled_count=filled)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        monkeypatch.setattr(ar, "_save_retrain_state", AsyncMock())
        monkeypatch.setattr(ar, "_persist_trade_outcome", AsyncMock(return_value=1))
        monkeypatch.setattr("services.trade_journal.record_trade_closed", AsyncMock())
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)
        monkeypatch.setattr(ar.settings, "retrain_auto_approve", auto_approve)
        return state, spawn

    @staticmethod
    async def _close_one_trade(deal_ticket: int):
        await record_trade_filled(
            symbol="EURUSD",
            entry_time=1725450000,
            exit_time=1725453600,
            pnl=10.0,
            pnl_pct=0.01,
            direction="LONG",
            sl_hit=False,
            tp_hit=True,
            exit_reason="tp",
            deal_ticket=deal_ticket,
        )

    @pytest.mark.asyncio
    async def test_threshold_reached_requires_approval(self, monkeypatch):
        min_trades = ar.RetrainConfig.min_trades_for_retrain
        state, spawn = self._patch_trigger(monkeypatch, min_trades - 1, False)

        await self._close_one_trade(deal_ticket=1001)

        assert state.filled_count == min_trades  # el cierre sí contó
        spawn.assert_not_called()                # pero sin aprobación no se lanza

    @pytest.mark.asyncio
    async def test_threshold_with_auto_approve_spawns(self, monkeypatch):
        min_trades = ar.RetrainConfig.min_trades_for_retrain
        state, spawn = self._patch_trigger(monkeypatch, min_trades - 1, True)

        await self._close_one_trade(deal_ticket=1002)

        assert state.filled_count == min_trades
        spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_below_threshold_no_spawn(self, monkeypatch):
        state, spawn = self._patch_trigger(
            monkeypatch, ar.RetrainConfig.min_trades_for_retrain - 2, True
        )

        await self._close_one_trade(deal_ticket=1003)

        spawn.assert_not_called()

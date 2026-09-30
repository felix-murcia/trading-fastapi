"""
O-1: ciclo de vida de orders.status → 'filled' en los webhooks de /trade/filled.

`check_duplicate` solo debe matchear órdenes VIVAS ('pending'/'placed'). Antes
`orders.status` nunca transicionaba: toda orden ejecutada permanecía 'pending'
para siempre y las entradas legítimas morían como `duplicate_order` (6 falsos
positivos contra órdenes no vivas en la ventana auditada).

Cubrimos:
- mark_order_filled: acotado a la fila viva más reciente (symbol+type, sin
  ventana temporal por incompatibilidad de relojes broker/UTC, excluye filas
  SMC por cycle_id), idempotente, traza en audit_log con event `order_filled`.
- /trade/filled: la rama de APERTURA (ignorada para trade_outcomes) y la de
  CIERRE (catch-all si la apertura se pierde) transicionan la orden.
- Mapeo de dominio LONG/SHORT (EA) → BUY/SELL (orders.type).
- Orden: si record_trade_filled falla, NO se transiciona (primero persistir
  el trade, después derivar el estado de la orden).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from services.order_manager import mark_order_filled

BASE_PAYLOAD = {
    "symbol": "EURUSD",
    "entry_time": "1725450000",
    "exit_time": "1725453600",
    "pnl": 12.5,
    "pnl_pct": 0.0125,
    "direction": "LONG",
    "sl_hit": False,
    "tp_hit": True,
    "exit_reason": "tp",
}


@pytest.fixture
def order_pool(monkeypatch):
    """Pool de services.order_manager: fetchrow (UPDATE) + execute (audit)."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    monkeypatch.setattr("services.order_manager.get_pool", lambda: pool)
    return pool


class TestMarkOrderFilledSQL:
    """El UPDATE solo puede casar una fila viva, la más reciente, sin SMC."""

    @pytest.mark.asyncio
    async def test_targets_newest_live_row_only(self, order_pool):
        order_pool.fetchrow.return_value = {"id": 9, "cycle_id": "EURUSD_1790702400000"}

        result = await mark_order_filled("EURUSD", "SELL")

        assert result == 9
        call = order_pool.fetchrow.call_args
        sql = call.args[0]
        assert call.args[1:] == ("EURUSD", "SELL")
        # Solo vivas: pending/placed salen del guard al transicionar
        assert "status IN ('pending','placed')" in sql
        # La más reciente (sin ventana temporal: entry_time es reloj broker)
        assert "ORDER BY created_at DESC LIMIT 1" in sql
        assert "INTERVAL" not in sql
        # Filas SMC excluidas: pipeline distinto con su propio ciclo de vida
        assert "NOT LIKE 'smc\\_%'" in sql
        assert "RETURNING id, cycle_id" in sql
        assert "UPDATE orders SET status='filled'" in sql

    @pytest.mark.asyncio
    async def test_transition_writes_order_filled_audit_event(self, order_pool):
        order_pool.fetchrow.return_value = {"id": 4, "cycle_id": "EURUSD_1790701100000"}

        await mark_order_filled("EURUSD", "BUY")

        args = order_pool.execute.call_args.args
        assert "INSERT INTO audit_log" in args[0]
        assert args[1] == "EURUSD_1790701100000"
        assert args[2] == "order_filled"

    @pytest.mark.asyncio
    async def test_no_live_row_returns_none_without_audit(self, order_pool):
        """Idempotencia: segundo reporte (cierre tras apertura) no matchea."""
        order_pool.fetchrow.return_value = None

        result = await mark_order_filled("EURUSD", "BUY")

        assert result is None
        order_pool.execute.assert_not_called()


def _post_filled(**overrides):
    """POST /trade/filled con las dos capas BD parcheadas (récord + transición)."""
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.load_inference_artifacts"), \
         patch("routers.ai.record_trade_filled", AsyncMock()) as record_mock, \
         patch("routers.ai.mark_order_filled", AsyncMock()) as fill_mock:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/trade/filled",
                json={**BASE_PAYLOAD, **overrides},
                headers={"x-internal-token": "test-token", "x-cycle-id": "o1-lifecycle"},
            )
    return response, record_mock, fill_mock


class TestCloseReportTransitions:
    """Cierre = catch-all: transiciona la orden si la apertura no llegó."""

    def test_close_long_transitions_to_buy(self):
        response, record_mock, fill_mock = _post_filled()
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "recorded"}
        record_mock.assert_awaited_once()
        fill_mock.assert_awaited_once_with("EURUSD", "BUY")

    def test_close_short_transitions_to_sell(self):
        _, _, fill_mock = _post_filled(
            direction="SHORT", pnl=-8.0, pnl_pct=-0.008,
            sl_hit=True, tp_hit=False, exit_reason="sl",
        )
        fill_mock.assert_awaited_once_with("EURUSD", "SELL")

    def test_no_transition_if_record_fails(self):
        """Orden: primero se persiste el trade; si eso falla, no se deriva nada."""
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts"), \
             patch("routers.ai.record_trade_filled", AsyncMock(side_effect=RuntimeError("DB caída"))), \
             patch("routers.ai.mark_order_filled", AsyncMock()) as fill_mock:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/ai/trade/filled",
                    json=BASE_PAYLOAD,
                    headers={"x-internal-token": "test-token", "x-cycle-id": "o1-record-fail"},
                )
        assert response.status_code == 500
        fill_mock.assert_not_awaited()


class TestOpeningReportTransitions:
    """Apertura: sigue sin persistirse en trade_outcomes, pero SÍ transiciona."""

    def test_opening_long_still_ignored_for_outcomes_but_fills_order(self):
        response, record_mock, fill_mock = _post_filled(
            entry_time="1725450000", exit_time="1725450000",
            pnl=0.0, pnl_pct=0.0,
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ignored_open_deal"}
        record_mock.assert_not_awaited()
        fill_mock.assert_awaited_once_with("EURUSD", "BUY")

    def test_opening_short_maps_to_sell(self):
        _, record_mock, fill_mock = _post_filled(
            entry_time="1725450000", exit_time="1725450000",
            pnl=0.0, pnl_pct=0.0, direction="SHORT",
        )
        record_mock.assert_not_awaited()
        fill_mock.assert_awaited_once_with("EURUSD", "SELL")

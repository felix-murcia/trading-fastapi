"""
O-1: ciclo de vida de orders.status → 'filled' en los webhooks de /trade/filled.

`check_duplicate` solo debe matchear órdenes VIVAS ('pending'/'placed'). Antes
`orders.status` nunca transicionaba: toda orden ejecutada permanecía 'pending'
para siempre y las entradas legítimas morían como `duplicate_order` (6 falsos
positivos contra órdenes no vivas en la ventana auditada).

Cubrimos:
- mark_order_filled: exige order_db_id exacto; sin él no muta ninguna orden.
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
    async def test_missing_order_id_does_not_guess_by_symbol_and_type(self, order_pool):
        result = await mark_order_filled("EURUSD", "SELL")

        assert result is None
        order_pool.fetchrow.assert_not_awaited()
        order_pool.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_order_id_is_used_when_provided(self, order_pool):
        order_pool.fetchrow.return_value = {"id": 9, "cycle_id": "EURUSD_1790702400000"}

        result = await mark_order_filled(
            "EURUSD", "SELL", order_db_id=9, mt5_order_id=88, position_id=77
        )

        assert result == 9
        sql, *params = order_pool.fetchrow.await_args.args
        assert "WHERE id=$3" in sql
        assert "mt5_position_id=COALESCE" in sql
        assert params == ["EURUSD", "SELL", 9, "88", 77]
        assert "ORDER BY created_at DESC LIMIT 1" not in sql

    @pytest.mark.asyncio
    async def test_transition_writes_order_filled_audit_event(self, order_pool):
        order_pool.fetchrow.return_value = {"id": 4, "cycle_id": "EURUSD_1790701100000"}

        await mark_order_filled("EURUSD", "BUY", order_db_id=4)

        args = order_pool.execute.call_args.args
        assert "INSERT INTO audit_log" in args[0]
        assert args[1] == "EURUSD_1790701100000"
        assert args[2] == "order_filled"

    @pytest.mark.asyncio
    async def test_no_live_row_returns_none_without_audit(self, order_pool):
        """Idempotencia: segundo reporte (cierre tras apertura) no matchea."""
        order_pool.fetchrow.return_value = None

        result = await mark_order_filled("EURUSD", "BUY", order_db_id=4)

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
        response, record_mock, fill_mock = _post_filled(
            order_db_id=9, opening_order_ticket=88, position_id=77
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "recorded"}
        record_mock.assert_awaited_once()
        record_mock.assert_awaited_once()
        assert record_mock.await_args.kwargs["order_db_id"] == 9
        fill_mock.assert_awaited_once_with(
            "EURUSD", "BUY", order_db_id=9, mt5_order_id=88, position_id=77
        )

    def test_close_short_transitions_to_sell(self):
        _, _, fill_mock = _post_filled(
            direction="SHORT", pnl=-8.0, pnl_pct=-0.008,
            sl_hit=True, tp_hit=False, exit_reason="sl",
            order_db_id=12,
        )
        fill_mock.assert_awaited_once_with(
            "EURUSD", "SELL", order_db_id=12, mt5_order_id=None, position_id=None
        )

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

    def test_conflicting_deal_order_link_returns_409_without_transition(self):
        from services.auto_retrain import TradeOutcomeLinkConflict

        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts"), \
             patch(
                 "routers.ai.record_trade_filled",
                 AsyncMock(side_effect=TradeOutcomeLinkConflict("different order")),
             ), \
             patch("routers.ai.mark_order_filled", AsyncMock()) as fill_mock:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/ai/trade/filled",
                    json={**BASE_PAYLOAD, "order_db_id": 9, "deal_ticket": 77},
                    headers={"x-internal-token": "test-token", "x-cycle-id": "o1-conflict"},
                )

        assert response.status_code == 409
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
        fill_mock.assert_awaited_once_with(
            "EURUSD", "BUY", order_db_id=None, mt5_order_id=None, position_id=None
        )

    def test_opening_short_maps_to_sell(self):
        _, record_mock, fill_mock = _post_filled(
            entry_time="1725450000", exit_time="1725450000",
            pnl=0.0, pnl_pct=0.0, direction="SHORT", order_db_id=15,
        )
        record_mock.assert_not_awaited()
        fill_mock.assert_awaited_once_with(
            "EURUSD", "SELL", order_db_id=15, mt5_order_id=None, position_id=None
        )

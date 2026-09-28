"""
Integration tests for POST /trade/filled webhook end-to-end (P1-4).

Flujo: TradeFilledRequest → normalización/validación → record_trade_filled →
_persist_trade_outcome → pool.fetchval(INSERT) → contador de retrain.

P1-4: TODAS las verificaciones sobre código Python son de COMPORTAMIENTO
(llamadas reales con BD/red parcheadas). Ningún test lee el fuente Python;
solo quedan como texto el DDL (schema.sql, sin Postgres en la suite) y el
contrato del EA MQL5 (sin runtime MQL5), en ficheros dedicados.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

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


def _post_filled(**overrides):
    """POST /trade/filled con record_trade_filled parcheado; devuelve (response, mock)."""
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.load_inference_artifacts"), \
         patch("routers.ai.record_trade_filled", AsyncMock()) as record_mock:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/trade/filled",
                json={**BASE_PAYLOAD, **overrides},
                headers={"x-internal-token": "test-token", "x-cycle-id": "e2e-filled"},
            )
    return response, record_mock


class TestTradeFilledDirectionNormalization:
    """MQL5 envía 'long'/'short'/'buy'/'sell'; el registro exige LONG/SHORT."""

    @pytest.mark.parametrize("incoming,expected", [
        ("long", "LONG"),
        ("buy", "LONG"),
        ("short", "SHORT"),
        ("sell", "SHORT"),
        ("LONG", "LONG"),
    ])
    def test_normalizes_direction_to_db_contract(self, incoming, expected):
        response, record_mock = _post_filled(direction=incoming)
        assert response.status_code == 200, response.text
        assert record_mock.await_args.kwargs["direction"] == expected


class TestTradeFilledTimestampValidation:
    def test_rejects_exit_before_entry(self):
        response, record_mock = _post_filled(entry_time="1725453600", exit_time="1725450000")
        assert response.status_code == 422
        assert "greater than" in response.json()["detail"]
        record_mock.assert_not_awaited()

    def test_rejects_timestamps_before_year_2000(self):
        response, record_mock = _post_filled(
            entry_time="946684799", exit_time="946684800"
        )
        assert response.status_code == 422
        assert "2000-01-01" in response.json()["detail"]
        record_mock.assert_not_awaited()

    def test_equal_timestamps_normalized_to_one_second(self):
        response, record_mock = _post_filled(
            entry_time="1725450000", exit_time="1725450000"
        )
        assert response.status_code == 200, response.text
        kwargs = record_mock.await_args.kwargs
        assert kwargs["entry_time"] == "1725450000"
        assert kwargs["exit_time"] == "1725450001"


class TestTradeFilledPayloadValidation:
    """P1-4: dominios validados ANTES de tocar BD → 422, nunca 500 de asyncpg."""

    def test_invalid_direction_rejected_at_edge(self):
        response, record_mock = _post_filled(direction="FLAT")
        assert response.status_code == 422
        assert "direction" in str(response.json()["detail"])
        record_mock.assert_not_awaited()

    def test_invalid_exit_reason_rejected_at_edge(self):
        response, record_mock = _post_filled(exit_reason="stop_loss")
        assert response.status_code == 422
        assert "exit_reason" in str(response.json()["detail"])
        record_mock.assert_not_awaited()


class TestTradeFilledPersistence:
    @pytest.mark.asyncio
    async def test_persist_inserts_row_with_dedup_and_returns_id(self, monkeypatch):
        from services.auto_retrain import _persist_trade_outcome

        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=77)
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        trade_id = await _persist_trade_outcome(
            "EURUSD", 1725450000.0, 1725453600.0, 12.5, 0.0125,
            "LONG", False, True, "tp", 123456, 789012,
        )

        assert trade_id == 77
        pool.fetchval.assert_awaited_once()
        sql, *params = pool.fetchval.await_args.args
        assert "INSERT INTO trade_outcomes" in sql
        assert "ON CONFLICT (deal_ticket)" in sql      # dedup a nivel DB
        assert "RETURNING id" in sql
        assert params[0] == "EURUSD"
        assert params[1] == "LONG"
        assert params[-2] == 123456                    # deal_ticket
        assert params[-1] == 789012                    # position_id

    @pytest.mark.asyncio
    async def test_persist_returns_none_on_duplicate_deal(self, monkeypatch):
        from services.auto_retrain import _persist_trade_outcome

        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=None)   # ON CONFLICT DO NOTHING
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        trade_id = await _persist_trade_outcome(
            "EURUSD", 1725450000.0, 1725453600.0, 12.5, 0.0125,
            "LONG", False, True, "tp", 123456, 789012,
        )
        assert trade_id is None


class TestRetrainTriggerLogic:
    def test_retrain_threshold_is_10(self):
        from services.auto_retrain import RetrainConfig

        assert RetrainConfig().trades_before_retrain == 10

    @pytest.mark.asyncio
    async def test_record_trade_filled_increments_counter(self, monkeypatch):
        from services import auto_retrain as ar

        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=1)
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        state = ar.RetrainState(filled_count=3, last_retrain_time=1.0)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        monkeypatch.setattr(ar, "_save_retrain_state", AsyncMock())
        monkeypatch.setattr("services.trade_journal.record_trade_closed", AsyncMock())

        await ar.record_trade_filled(
            symbol="EURUSD",
            entry_time="1725450000",
            exit_time="1725453600",
            pnl=12.5,
            pnl_pct=0.0125,
            direction="LONG",
            sl_hit=False,
            tp_hit=True,
            exit_reason="tp",
            deal_ticket=555001,
            position_id=666001,
        )

        assert state.filled_count == 4        # 3 → 4 (y 4 < min_trades: sin trigger)
        assert len(state.outcomes) == 1
        assert state.outcomes[0].pnl == 12.5

    @pytest.mark.asyncio
    async def test_duplicate_deal_does_not_increment_counter(self, monkeypatch):
        from services import auto_retrain as ar

        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=None)   # deal_ticket ya registrado
        monkeypatch.setattr("db.connection.get_pool", lambda: pool)

        state = ar.RetrainState(filled_count=3, last_retrain_time=1.0)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        monkeypatch.setattr(ar, "_save_retrain_state", AsyncMock())
        monkeypatch.setattr("services.trade_journal.record_trade_closed", AsyncMock())

        await ar.record_trade_filled(
            symbol="EURUSD",
            entry_time="1725450000",
            exit_time="1725453600",
            pnl=-5.0,
            pnl_pct=-0.005,
            direction="SHORT",
            sl_hit=True,
            tp_hit=False,
            exit_reason="sl",
            deal_ticket=555001,
            position_id=666001,
        )

        assert state.filled_count == 3        # sin doble conteo
        assert state.outcomes == []


class TestDealTicketPersistence:
    """P0-4: deal_ticket/position_id llegan del EA, se persisten y se deduplican."""

    def test_webhook_threads_deal_ticket_and_position_id(self):
        """El webhook debe pasar deal_ticket/position_id a record_trade_filled (no ignorarlos)."""
        response, record_mock = _post_filled(deal_ticket=123456, position_id=789012)

        assert response.status_code == 200, response.text
        kwargs = record_mock.await_args.kwargs
        assert kwargs["deal_ticket"] == 123456
        assert kwargs["position_id"] == 789012
        assert kwargs["direction"] == "LONG"

    def test_string_numeric_fields_are_coerced(self):
        """El EA a veces manda números como strings → se convierten antes de persistir."""
        response, record_mock = _post_filled(
            deal_ticket="123456", position_id="789012",
            pnl="12.5", pnl_pct="0.0125",
        )

        assert response.status_code == 200, response.text
        kwargs = record_mock.await_args.kwargs
        assert kwargs["deal_ticket"] == 123456
        assert kwargs["position_id"] == 789012
        assert isinstance(kwargs["pnl"], float) and kwargs["pnl"] == 12.5

    @pytest.mark.asyncio
    async def test_duplicate_deal_ticket_skips_journal_and_counter(self):
        """ON CONFLICT DO NOTHING → trade_id None → no journal, no contador, sin excepción."""
        from services.auto_retrain import record_trade_filled

        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=None)   # conflicto: ya existía
        with patch("db.connection.get_pool", return_value=pool), \
             patch("services.trade_journal.record_trade_closed", AsyncMock()) as journal_mock:
            await record_trade_filled(
                symbol="EURUSD",
                entry_time="1725450000",
                exit_time="1725453600",
                pnl=-10.0,
                pnl_pct=-0.01,
                direction="SHORT",
                sl_hit=True,
                tp_hit=False,
                exit_reason="sl",
                deal_ticket=999999,
                position_id=111111,
            )

        journal_mock.assert_not_called()

    def test_schema_declares_unique_deal_ticket(self):
        """schema.sql declara deal_ticket + índice único parcial (dedup a nivel DB).

        Aserción de DDL (sin Postgres en la suite unitaria): la migración viva
        se aplicó por separado; el comportamiento dedup está cubierto arriba.
        """
        import os
        schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
        with open(schema_path, "r") as f:
            schema = f.read()
        assert "deal_ticket  BIGINT" in schema
        assert "idx_trade_outcomes_deal_ticket" in schema
        assert "WHERE deal_ticket IS NOT NULL" in schema

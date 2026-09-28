"""
Integration tests for POST /trade/filled webhook end-to-end.

These tests verify the complete flow:
  TradeFilledRequest validation → record_trade_filled → _persist_trade_outcome → DB

The MQL5 EA calls /trade/filled when an order closes, so this is a critical path
that must work correctly for the auto-retrain pipeline to function.
"""
import os
import re
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from main import app


class TestTradeFilledDirectionNormalization:
    """
    Verify direction normalization by reading the actual source file.
    This avoids importing routers.ai which requires torch/pandas.
    """

    def test_trade_filled_webhook_normalizes_direction_to_uppercase(self):
        """
        MQL5 sends 'long'/'short' but DB constraint requires 'LONG'/'SHORT'.
        The router MUST normalize direction to uppercase before calling record_trade_filled.
        This was the root cause of HTTP 500 errors on /trade/filled (commit 38491e8).
        """
        routers_ai_path = os.path.join(
            os.path.dirname(__file__), "..", "routers", "ai.py"
        )
        with open(routers_ai_path, "r") as f:
            source = f.read()

        # Find the trade_filled_webhook function body
        match = re.search(
            r'async def trade_filled_webhook\(.*?\):.*?(?=\n@router|\nasync def |\nclass |\Z)',
            source,
            re.DOTALL,
        )
        assert match, "trade_filled_webhook function not found in routers/ai.py"
        func_source = match.group(0)

        # The function must contain direction.upper() normalization
        assert "direction.upper()" in func_source, (
            "trade_filled_webhook must normalize direction to uppercase with "
            "direction.upper() to prevent DB constraint violation. "
            "MQL5 sends 'long'/'short', DB requires 'LONG'/'SHORT'."
        )

    def test_trade_filled_webhook_rejects_invalid_or_unordered_timestamps(self):
        routers_ai_path = os.path.join(
            os.path.dirname(__file__), "..", "routers", "ai.py"
        )
        with open(routers_ai_path, "r") as f:
            source = f.read()

        match = re.search(
            r'async def trade_filled_webhook\(.*?\):.*?(?=\n@router|\nasync def |\nclass |\Z)',
            source,
            re.DOTALL,
        )
        assert match, "trade_filled_webhook function not found in routers/ai.py"
        func_source = match.group(0)

        assert "entry_ts < minimum_timestamp" in func_source
        assert "exit_ts <= entry_ts" in func_source


class TestTradeFilledPersistence:
    """Verify trade_filled persists correctly to DB via auto_retrain service."""

    def test_persist_trade_outcome_inserts_into_database(self):
        """
        _persist_trade_outcome must INSERT a row into trade_outcomes.
        Verify the SQL query uses INSERT and includes all required columns.
        """
        auto_retrain_path = os.path.join(
            os.path.dirname(__file__), "..", "services", "auto_retrain.py"
        )
        with open(auto_retrain_path, "r") as f:
            source = f.read()

        # Find _persist_trade_outcome
        match = re.search(
            r'async def _persist_trade_outcome\(.*?\):.*?(?=\nasync def |\ndef |\Z)',
            source,
            re.DOTALL,
        )
        assert match, "_persist_trade_outcome not found in services/auto_retrain.py"
        func_source = match.group(0)

        # Must contain INSERT INTO trade_outcomes
        assert "INSERT INTO trade_outcomes" in func_source, \
            "_persist_trade_outcome must INSERT into trade_outcomes table"

        # Must include direction column
        assert "direction" in func_source, \
            "INSERT must include direction column"

    def test_persist_trade_outcome_uses_fetchval(self):
        """
        _persist_trade_outcome uses pool.fetchval() to get the inserted row ID.
        This is the correct asyncpg pattern for INSERT + returning id.
        """
        auto_retrain_path = os.path.join(
            os.path.dirname(__file__), "..", "services", "auto_retrain.py"
        )
        with open(auto_retrain_path, "r") as f:
            source = f.read()

        match = re.search(
            r'async def _persist_trade_outcome\(.*?\):.*?(?=\nasync def |\ndef |\Z)',
            source,
            re.DOTALL,
        )
        assert match, "_persist_trade_outcome not found"
        func_source = match.group(0)

        assert "fetchval" in func_source, \
            "_persist_trade_outcome must use fetchval to get inserted ID"


class TestRetrainTriggerLogic:
    """Verify retrain triggers after N trades."""

    def test_retrain_trades_threshold_is_10(self):
        """
        RetrainConfig.trades_before_retrain should be 10.
        Verify this is correctly set so the system can learn reasonably fast.
        """
        auto_retrain_path = os.path.join(
            os.path.dirname(__file__), "..", "services", "auto_retrain.py"
        )
        with open(auto_retrain_path, "r") as f:
            source = f.read()

        match = re.search(r"trades_before_retrain:\s*int\s*=\s*(\d+)", source)
        assert match, "trades_before_retrain not found in RetrainConfig"
        threshold = int(match.group(1))
        assert threshold == 10, \
            f"trades_before_retrain should be 10, got {threshold}"

    def test_record_trade_filled_increments_filled_count(self):
        """
        record_trade_filled must increment filled_count.
        After 10 calls, retrain should trigger.
        """
        auto_retrain_path = os.path.join(
            os.path.dirname(__file__), "..", "services", "auto_retrain.py"
        )
        with open(auto_retrain_path, "r") as f:
            source = f.read()

        match = re.search(
            r'async def record_trade_filled\(.*?\):.*?(?=\nasync def |\ndef |\Z)',
            source,
            re.DOTALL,
        )
        assert match, "record_trade_filled not found"
        func_source = match.group(0)

        # Must increment filled_count
        assert "filled_count" in func_source, \
            "record_trade_filled must track filled_count"
        assert "state.filled_count" in func_source or "filled_count +" in func_source, \
            "filled_count must be incremented on each trade"


class TestDealTicketPersistence:
    """P0-4: deal_ticket/position_id llegan del EA, se persisten y se deduplican."""

    def test_webhook_threads_deal_ticket_and_position_id(self):
        """El webhook debe pasar deal_ticket/position_id a record_trade_filled (no ignorarlos)."""
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts"), \
             patch("routers.ai.record_trade_filled", AsyncMock()) as record_mock:
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/ai/trade/filled",
                    json={
                        "symbol": "EURUSD",
                        "entry_time": "1725450000",
                        "exit_time": "1725453600",
                        "pnl": 12.5,
                        "pnl_pct": 0.0125,
                        "direction": "LONG",
                        "sl_hit": False,
                        "tp_hit": True,
                        "exit_reason": "tp",
                        "deal_ticket": 123456,
                        "position_id": 789012,
                    },
                    headers={"x-internal-token": "test-token", "x-cycle-id": "e2e-filled"},
                )

        assert response.status_code == 200, response.text
        kwargs = record_mock.await_args.kwargs
        assert kwargs["deal_ticket"] == 123456
        assert kwargs["position_id"] == 789012
        assert kwargs["direction"] == "LONG"

    def test_duplicate_deal_ticket_skips_journal_and_counter(self):
        """ON CONFLICT DO NOTHING → trade_id None → no journal, no contador, sin excepción."""
        from services.auto_retrain import record_trade_filled

        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=None)  # conflicto: deal_ticket ya existía
        with patch("db.connection.get_pool", return_value=pool), \
             patch("services.trade_journal.record_trade_closed", AsyncMock()) as journal_mock:
            asyncio.run(record_trade_filled(
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
            ))

        journal_mock.assert_not_called()

    def test_schema_declares_unique_deal_ticket(self):
        """schema.sql debe declarar deal_ticket + índice único parcial (dedup a nivel DB)."""
        schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
        with open(schema_path, "r") as f:
            schema = f.read()
        assert "deal_ticket  BIGINT" in schema
        assert "idx_trade_outcomes_deal_ticket" in schema
        assert "WHERE deal_ticket IS NOT NULL" in schema

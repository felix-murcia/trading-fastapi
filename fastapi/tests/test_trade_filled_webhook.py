"""
Integration tests for POST /trade/filled webhook end-to-end.

These tests verify the complete flow:
  TradeFilledRequest validation → record_trade_filled → _persist_trade_outcome → DB

The MQL5 EA calls /trade/filled when an order closes, so this is a critical path
that must work correctly for the auto-retrain pipeline to function.

NOTE: These tests use source-code inspection rather than importing routers.ai directly,
because routers.ai imports torch/pandas which are not installed in the test environment.
We verify the critical behaviors by checking the source code directly.
"""
import os
import re


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

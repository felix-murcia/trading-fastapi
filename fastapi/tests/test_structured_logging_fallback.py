"""
Tests for structured_logging fallback behavior.

Covers:
- log_to_audit when DB pool is not initialized (should not crash)
- log_to_audit when DB pool fails (should log warning, not crash)
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import logging

from services.structured_logging import log_to_audit


class TestLogToAuditFallback:
    """Tests for log_to_audit fallback when DB is unavailable."""

    @pytest.mark.asyncio
    async def test_no_pool_logs_warning_and_does_not_crash(self, caplog):
        """Should log warning and not raise when pool is not initialized."""
        with patch("db.connection.get_pool") as mock_get_pool:
            mock_get_pool.side_effect = RuntimeError("DB pool not initialized")

            with caplog.at_level(logging.WARNING):
                await log_to_audit("test_event", {"key": "value"})

        assert "No se pudo guardar audit_log" in caplog.text
        assert "DB pool not initialized" in caplog.text

    @pytest.mark.asyncio
    async def test_pool_execute_failure_logs_warning(self, caplog):
        """Should log warning when pool.execute fails."""
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(side_effect=OSError("DB connection lost"))

        with caplog.at_level(logging.WARNING):
            await log_to_audit("test_event", {"key": "value"}, pool=mock_pool)

        assert "No se pudo guardar audit_log" in caplog.text
        assert "DB connection lost" in caplog.text

    @pytest.mark.asyncio
    async def test_successful_audit_log(self):
        """Should successfully insert into audit_log when pool works."""
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        await log_to_audit("test_event", {"key": "value"}, pool=mock_pool)

        mock_pool.execute.assert_called_once()
        call_args = mock_pool.execute.call_args[0]
        assert "INSERT INTO audit_log" in call_args[0]
        assert call_args[1] == "trace_no-trace"  # cycle_id with no trace
        assert call_args[2] == "test_event"

    @pytest.mark.asyncio
    async def test_mt5_time_never_overrides_created_at(self):
        """T-1: con mt5_time (reloj broker, UTC+3) el INSERT es único y SIN
        to_timestamp — `created_at` lo pone el DEFAULT de la columna (now() UTC).
        Antes, el epoch del broker se escribía como created_at → filas "futuras"
        y timeline invertida. El epoch sigue disponible dentro del payload."""
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        await log_to_audit(
            "ai_predict_cycle",
            {"symbol": "EURUSD", "mt5_time": 1790783400.0},
            pool=mock_pool,
            cycle_id="EURUSD_1790783400000",
        )

        mock_pool.execute.assert_called_once()
        call_args = mock_pool.execute.call_args[0]
        sql = call_args[0]
        # Único INSERT, sin rama de reloj broker: solo 3 placeholders
        assert "to_timestamp" not in sql
        assert "created_at" not in sql
        assert sql.count("$") == 3
        assert call_args[1] == "EURUSD_1790783400000"
        # El epoch del broker viaja en data/enriched, no en el timeline
        assert '"mt5_time": 1790783400.0' in call_args[3]

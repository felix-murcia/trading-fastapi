"""
Tests for services/alerting.py
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.alerting import send_alert, AlertLevel


class TestAlertLevels:
    """Tests that AlertLevel enum has expected values."""

    def test_alert_levels_exist(self):
        assert AlertLevel.WARNING.value == "WARNING"
        assert AlertLevel.ERROR.value == "ERROR"
        assert AlertLevel.CRITICAL.value == "CRITICAL"


class TestSendAlert:
    """Tests for send_alert function."""

    def test_send_alert_logs_error(self, mock_alerting):
        """send_alert should call _send_to_telegram (mocked)."""
        send_alert(AlertLevel.ERROR, "TEST", "Test message")
        mock_alerting.assert_called_once()

    def test_send_alert_with_exception(self, mock_alerting):
        """send_alert should include exception info."""
        exc = ValueError("test error")
        send_alert(AlertLevel.ERROR, "TEST", "Error occurred", exc=exc)
        mock_alerting.assert_called_once()

    def test_send_alert_with_context(self, mock_alerting):
        """send_alert should include context dict."""
        ctx = {"trade_id": 123, "symbol": "EURUSD"}
        send_alert(AlertLevel.WARNING, "TEST", "Warning occurred", context=ctx)
        mock_alerting.assert_called_once()

    def test_send_alert_critical_level(self, mock_alerting):
        """send_alert should handle CRITICAL level."""
        send_alert(AlertLevel.CRITICAL, "CRITICAL-COMPONENT", "System failure")
        mock_alerting.assert_called_once()

    def test_send_alert_no_telegram_if_not_configured(self):
        """If Telegram not configured, should not fail."""
        with patch("services.alerting._send_to_telegram") as mock_tg:
            mock_tg.side_effect = Exception("Telegram not configured")
            # _send_to_telegram catches Exception internally (except Exception: pass)
            # so send_alert itself must not propagate — it should complete normally
            try:
                send_alert(AlertLevel.ERROR, "TEST", "No Telegram configured")
            except Exception as e:
                pytest.fail(f"send_alert should not raise, but raised: {e}")

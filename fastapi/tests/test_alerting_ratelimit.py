"""
B-1 (plan 2026-09-30): rate-limit de alertas.

La misma clase de alerta (nivel + componente + huella con cifras colapsadas)
no se re-emite dentro de 600 s — ni log ni Telegram — y el recuento de
repeticiones suprimidas se anexa a la siguiente emisión permitida. CRITICAL
nunca se limita. Mensajes que sólo difieren en marcas de tiempo/cifras
comparten huella (ruido típico de errores de red).
"""
import pytest
from unittest.mock import patch

import services.alerting as alerting
from services.alerting import send_alert, AlertLevel


@pytest.fixture(autouse=True)
def _fresh_ratelimit_state(monkeypatch):
    """Estado de rate-limit limpio + reloj controlable por test."""
    alerting._alert_last_emit.clear()
    alerting._alert_suppressed.clear()
    clock = {"now": 1000.0}
    monkeypatch.setattr(alerting, "monotonic", lambda: clock["now"])
    yield clock
    alerting._alert_last_emit.clear()
    alerting._alert_suppressed.clear()


class TestRateLimit:
    """Supresión de repeticiones dentro de la ventana."""

    def test_repeat_within_window_emits_once(self, mock_alerting):
        with patch.object(alerting.logger, "error") as log_err:
            for _ in range(3):
                send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        mock_alerting.assert_called_once()
        log_err.assert_called_once()

    def test_suppression_leaves_no_error_log(self, mock_alerting):
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        with patch.object(alerting.logger, "error") as log_err:
            send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        log_err.assert_not_called()
        mock_alerting.assert_called_once()   # sólo la primera llegó a Telegram

    def test_different_component_not_suppressed(self, mock_alerting):
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        send_alert(AlertLevel.ERROR, "PIPELINE-HEALTH", "Backend caído")
        assert mock_alerting.call_count == 2

    def test_different_level_not_suppressed(self, mock_alerting):
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        send_alert(AlertLevel.WARNING, "MCP", "Backend caído")
        assert mock_alerting.call_count == 2

    def test_window_expiry_reemits(self, mock_alerting, _fresh_ratelimit_state):
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        _fresh_ratelimit_state["now"] += 600.0
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        assert mock_alerting.call_count == 2

    def test_timestamped_variants_share_shape(self, mock_alerting):
        """17:02:43 vs 17:09:10 → misma clase: una sola alerta en la ventana."""
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído 17:02:43 en 172.16.0.9:8000")
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído 17:09:10 en 172.16.0.9:8000")
        mock_alerting.assert_called_once()


class TestSuppressionIsVisible:
    """La supresión no puede ser invisible: recuento anexado a la re-emisión."""

    def test_next_emission_reports_suppressed_count(self, mock_alerting, _fresh_ratelimit_state):
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        _fresh_ratelimit_state["now"] += 5.0
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")   # suprimida 1
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")   # suprimida 2
        _fresh_ratelimit_state["now"] += 600.0
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        assert mock_alerting.call_count == 2
        reemitted_message = mock_alerting.call_args.args[1]
        assert "+2 repetidas suprimidas en 600s" in reemitted_message

    def test_expired_idle_class_is_pruned(self, _fresh_ratelimit_state):
        send_alert(AlertLevel.ERROR, "MCP", "Backend caído")
        assert ("ERROR", "MCP", "Backend caído") in alerting._alert_last_emit
        _fresh_ratelimit_state["now"] += 600.0
        send_alert(AlertLevel.ERROR, "OTHER", "Otra clase")
        assert ("ERROR", "MCP", "Backend caído") not in alerting._alert_last_emit


class TestCriticalNeverLimited:
    """CRITICAL = posible pérdida de datos: siempre se emite."""

    def test_critical_bypasses_rate_limit(self, mock_alerting):
        for _ in range(3):
            send_alert(AlertLevel.CRITICAL, "DB", "trade_outcomes sin persistir")
        assert mock_alerting.call_count == 3

    def test_critical_does_not_consume_other_levels_window(self, mock_alerting):
        send_alert(AlertLevel.CRITICAL, "DB", " Pérdida de datos")
        send_alert(AlertLevel.ERROR, "DB", " Pérdida de datos")
        assert mock_alerting.call_count == 2

"""Test de RealNotifier: captura mensajes sin tocar la red."""

import pytest

from src.domain.ports import Severity
from src.infrastructure.adapters.real_notifier import RealNotifier


def test_notify_capture_guarda_mensaje_sin_red():
    """capture=True: notify guarda en self.sent, no llama a send_alert."""
    notifier = RealNotifier(capture=True)

    notifier.notify("retrain promoted: EURUSD (manual) -> model_123", Severity.INFO)
    notifier.notify("retrain failed: EURUSD -> ValueError", Severity.ERROR)

    assert len(notifier.sent) == 2
    assert notifier.sent[0] == {
        "message": "retrain promoted: EURUSD (manual) -> model_123",
        "severity": "info",
    }
    assert notifier.sent[1] == {
        "message": "retrain failed: EURUSD -> ValueError",
        "severity": "error",
    }


def test_notify_default_severity_es_info():
    """severity por defecto: INFO."""
    notifier = RealNotifier(capture=True)
    notifier.notify("algo pasó")
    assert notifier.sent[0]["severity"] == Severity.INFO


def test_notify_sin_capture_llama_send_alert(monkeypatch):
    """capture=False: notify llama a services.alerting.send_alert."""
    from services import alerting

    captured = {}

    def _fake_send_alert(level, component, message, **kwargs):
        captured["level"] = level
        captured["component"] = component
        captured["message"] = message

    monkeypatch.setattr(alerting, "send_alert", _fake_send_alert)

    notifier = RealNotifier(capture=False)
    notifier.notify("mensaje de prueba", Severity.WARNING)

    assert captured["component"] == "auto_retrain"
    assert captured["message"] == "mensaje de prueba"
    assert captured["level"].name == "WARNING"

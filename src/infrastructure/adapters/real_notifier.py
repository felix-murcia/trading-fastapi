"""RealNotifier: adaptador real del puerto Notifier.

Envía alertas vía ``services.alerting.send_alert`` (Telegram/Slack según
configuración). En tests se usa con ``capture=True`` para no tocar la red:
los mensajes se guardan en ``self.sent`` y se pueden inspeccionar.

Implementa Notifier por typing estructural:
  - notify(message: str, severity: str = Severity.INFO) -> None
"""

from src.domain.ports import Notifier, Severity


class RealNotifier(Notifier):
    def __init__(self, capture: bool = False):
        self.capture = capture
        self.sent: list[dict[str, str]] = []

    def notify(self, message: str, severity: str = Severity.INFO) -> None:
        entry = {"message": message, "severity": severity}
        if self.capture:
            self.sent.append(entry)
            return

        # Producción: envío real vía send_alert.
        from services.alerting import AlertLevel, send_alert

        level_map = {
            Severity.INFO: AlertLevel.WARNING,   # no hay INFO en AlertLevel
            Severity.WARNING: AlertLevel.WARNING,
            Severity.ERROR: AlertLevel.ERROR,
        }
        level = level_map.get(severity, AlertLevel.WARNING)
        send_alert(level, "auto_retrain", message)

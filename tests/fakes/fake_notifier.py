"""Fake Notifier en memoria (satisface Notifier por typing estructural).

Captura notificaciones para inspección en tests. Para tests de caracterización de RunAutoRetrain.
"""


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    def notify(self, message: str, severity: str = "info") -> None:
        self.notifications.append({"message": message, "severity": severity})

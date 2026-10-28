"""Fake AuditLogger en memoria (sin infraestructura, sin IO).

Almacena cada evento en `events` para inspección en tests.
"""


class FakeAuditLogger:
    def __init__(self):
        self.events = []

    def log(self, event: str, details: dict) -> None:
        self.events.append({"event": event, "details": details})

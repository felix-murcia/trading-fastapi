"""Fake BrokerGateway en memoria (sin red, sin EA, sin MT5 real).

Solo captura la orden en una lista para verificación en tests.
Ninguna ruta al broker real: no imports de httpx/requests, no capas de red,
no HTTP.
"""

from src.domain.ports import BrokerGateway


class FakeBrokerGateway(BrokerGateway):
    """Fake de BrokerGateway para tests de caracterización y unidad.

    Almacena cada orden ejecutada en `executed_orders` para inspección.
    No tiene ruta al broker real: es puramente en memoria.
    """

    def __init__(self):
        self.executed_orders = []
        self.should_fail = False
        self.fail_reason = "broker_unavailable"

    def execute(self, order: dict) -> dict:
        """Ejecuta una orden (en memoria). Devuelve un ID sintético."""
        if self.should_fail:
            raise RuntimeError(self.fail_reason)
        ticket = len(self.executed_orders) + 900000
        record = {
            "ticket": ticket,
            "symbol": order.get("symbol"),
            "type": order.get("type"),
            "volume": order.get("volume"),
            "price": order.get("price"),
            "sl": order.get("sl"),
            "tp": order.get("tp"),
            "comment": order.get("comment", ""),
        }
        self.executed_orders.append(record)
        return {"ticket": ticket, "order": record}

    def get_price(self, symbol: str) -> float:
        """Devuelve un precio sintético (sin red)."""
        return 1.1000


class FakeAuditLogger:
    """Fake de AuditLogger en memoria.

    Almacena cada evento en `events` para inspección en tests.
    """

    def __init__(self):
        self.events = []

    def log(self, event: str, details: dict) -> None:
        self.events.append({"event": event, "details": details})


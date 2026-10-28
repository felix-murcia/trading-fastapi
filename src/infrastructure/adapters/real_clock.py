"""RealClock: adaptador real del puerto Clock.

Usa datetime.utcnow() (reloj real del sistema). No toca infraestructura
externa — solo la biblioteca estándar.
"""

from datetime import datetime

from src.domain.ports import Clock


class RealClock(Clock):
    """Reloj real: devuelve datetime.utcnow()."""

    def now(self) -> datetime:
        return datetime.utcnow()

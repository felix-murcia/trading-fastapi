"""T-07 (2026-10-15, orden de Félix «aprobado t07»): breaker del feed H4/D1.

El incidente MCP 14/10 (14:05→15:45 UTC) demostró que el bug vive en el
binding MetaTrader5: la PRIMERA petición H4/D1 al servidor wedgado vuelve
a tumbar su sesión (ventanas vivas de 1-2 min muertas por el 1er H4
posterior). Sin breaker, cada ciclo /predict reintentaba H4 y re-mataba
el servidor — el MCP no podía recuperarse solo.

Semántica half-open:
  - cerrado (default): fetch H4/D1 en todos los ciclos (contexto de
    Qwen, no crítico);
  - PRIMER FALLO de fetch H4/D1 → `h4d_breaker_trip`: abierto durante
    `H4D_BREAKER_COOLDOWN_MINUTES` (config, default 30) — los ciclos del
    cooldown NO fetchan H4/D1 (la línea del prompt se renderiza
    «Unavailable» con esquema constante, anti-ancla H2 intacta) y el MCP
    se queda vivo para los críticos equity/H1;
  - al expirar el cooldown el siguiente ciclo sondea de nuevo: éxito →
    cerrado; fallo → re-abre otro cooldown (contador `openings`).

Estado en memoria (módulo, patrón qwen_client/_QWEN_STATE): un único
MCP/terminal compartido → un único breaker; sin locks (event loop
single-thread, mismo patrón que el estado de Qwen).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from config import settings


@dataclass
class FeedBreakerState:
    """Estado del breaker H4/D1 (telemetría + ventana de cooldown)."""

    open_until: float = 0.0            # epoch; ahora < open_until → abierto
    openings: int = 0                  # aperturas acumuladas (telemetría)
    last_opened_at: float | None = None
    last_failure: str | None = None    # «TF: Excepción: detalle» del último fallo


_FEED_BREAKER = FeedBreakerState()


def get_feed_breaker() -> FeedBreakerState:
    return _FEED_BREAKER


def h4d_breaker_is_open() -> bool:
    """True → los ciclos /predict NO fetchan H4/D1 (cooldown en curso)."""
    return time.time() < _FEED_BREAKER.open_until


def h4d_breaker_trip(reason: str) -> None:
    """Abre el breaker otro cooldown. Cada fallo (incluido el de la sonda
    half-open) extiende/reabre la ventana con el cooldown configurado."""
    now = time.time()
    _FEED_BREAKER.open_until = now + settings.h4d_breaker_cooldown_minutes * 60.0
    _FEED_BREAKER.openings += 1
    _FEED_BREAKER.last_opened_at = now
    _FEED_BREAKER.last_failure = reason


def h4d_breaker_reset() -> None:
    """Restaura el estado inicial COMPLETO (cierre + telemetría limpia);
    usado por los tests como fixture autouse por test."""
    _FEED_BREAKER.open_until = 0.0
    _FEED_BREAKER.openings = 0
    _FEED_BREAKER.last_opened_at = None
    _FEED_BREAKER.last_failure = None

"""Caso de uso: RecordEADecision.

Registra la decisión del EA reportada vía /ea/decision webhook.
Audita en AuditLogger.

COMPONE puertos de dominio: AuditLogger, Clock.
NO toca infraestructura (no asyncpg, no httpx, no MetaTrader5).

Paridad con legacy/ea_decision_webhook (routers/ai.py:2310):
  - Recibe los campos de EADecisionRequest
  - mt5_time fallback a time.time() si falta
  - Audita evento "ea_decision" con todos los campos
  - Retorna {"status": "recorded"}

NOTA: el legacy NO es idempotente (no verifica cycle_id duplicado).
Este caso de uso tampoco lo es: cada llamada audita un evento nuevo.
Si se requiere idempotencia, extender con un puerto de deduplicación.

TODO(7c-1): el legacy usa time.time() como fallback de mt5_time. El caso de
uso inyecta Clock para determinismo en tests; el router pasa el clock real.
"""

from dataclasses import dataclass
from typing import Optional

from src.domain.ports import AuditLogger, Clock


@dataclass(frozen=True)
class RecordEADecisionInput:
    cycle_id: str
    symbol: str
    received_decision: str
    action: str
    order_ticket: Optional[str] = None
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    reason: Optional[str] = None
    latency_ms: Optional[float] = None
    raw_response: Optional[str] = None
    mt5_time: Optional[float] = None  # epoch desde MT5; si falta, se usa clock


@dataclass(frozen=True)
class RecordEADecisionOutput:
    audited: bool
    error: Optional[str]


class RecordEADecision:
    def __init__(
        self,
        audit: AuditLogger,
        clock: Clock,
    ):
        self._audit = audit
        self._clock = clock

    def execute(self, inp: RecordEADecisionInput) -> RecordEADecisionOutput:
        # Fallback de mt5_time: si falta, usar clock.now()
        mt5_ts = inp.mt5_time if inp.mt5_time is not None else self._clock.now()

        # Auditar evento (paridad con legacy: log_to_audit puede lanzar;
        # se captura para no romper el ciclo del EA)
        try:
            self._audit.log(
                "ea_decision",
                {
                    "cycle_id": inp.cycle_id,
                    "symbol": inp.symbol,
                    "received_decision": inp.received_decision,
                    "action": inp.action,
                    "order_ticket": inp.order_ticket,
                    "volume": inp.volume,
                    "sl": inp.sl,
                    "tp": inp.tp,
                    "reason": inp.reason,
                    "latency_ms": inp.latency_ms,
                    "raw_response": inp.raw_response,
                    "mt5_time": mt5_ts,
                },
            )
            audited = True
            error = None
        except Exception as exc:
            # Fallo de auditoría: silenciar para no romper el EA (paridad legacy)
            audited = False
            error = f"audit_error: {exc}"

        return RecordEADecisionOutput(
            audited=audited,
            error=error,
        )

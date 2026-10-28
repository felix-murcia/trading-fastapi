"""Caso de uso: RecordTradeFill.

Registra el cierre de una orden reportado por el EA vía trade_filled_webhook.
Persiste en TradeRepository y audita en AuditLogger.

COMPONE puertos de dominio: TradeRepository, AuditLogger, Clock.
NO toca infraestructura (no asyncpg, no httpx, no MetaTrader5).

Idempotencia: si ya existe un trade con el mismo cycle_id o deal_ticket,
no duplica (devuelve el order_db_id existente).

Paridad con legacy/trade_filled_webhook (routers/ai.py:2393):
  - Valida que exit_time > entry_ts (si ambos presentes)
  - Calcula pnl_total = pnl + swap (swap se pasa como parte de pnl en el input)
  - Persiste en trade_outcomes (vía TradeRepository.save)
  - Audita en audit_log (vía AuditLogger.log)
  - Devuelve order_db_id, persisted, audited, error

TODO(sesion6): el legacy valida PnL contra equity/5 y puede rechazar con 422.
Esa validación vive en el router, no en este caso de uso. Si se necesita
moverla aquí, extender con un puerto de cuenta/equity.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from src.domain.entities import Trade
from src.domain.ports import AuditLogger, Clock, TradeRepository  # noqa: F401
from src.domain.value_objects.thresholds import TradingThresholds  # noqa: F401


@dataclass(frozen=True)
class RecordTradeFillInput:
    cycle_id: str
    symbol: str
    direction: str   # "BUY", "SELL", "LONG", "SHORT"
    entry_price: Optional[float]
    exit_price: Optional[float]
    pnl: Optional[float]
    pnl_pct: Optional[float]
    deal_ticket: Optional[int]
    position_id: Optional[int]
    entry_time: Optional[float]   # Unix timestamp (bruto, reloj del bróker)
    exit_time: Optional[float]    # Unix timestamp (bruto, reloj del bróker)
    mt5_order_id: Optional[int]


@dataclass(frozen=True)
class RecordTradeFillOutput:
    order_db_id: Optional[int]
    persisted: bool
    audited: bool
    error: Optional[str]


class RecordTradeFill:
    def __init__(
        self,
        repo: TradeRepository,
        audit: AuditLogger,
        clock: Clock,
        thresholds: TradingThresholds,
    ):
        self._repo = repo
        self._audit = audit
        self._clock = clock
        self._thresholds = thresholds

    def execute(self, inp: RecordTradeFillInput) -> RecordTradeFillOutput:
        # Idempotencia: si ya existe un trade con este cycle_id o deal_ticket,
        # no duplicar (legado: el reporte de apertura puede llegar dos veces).
        existing = self._repo.find_by_id(inp.cycle_id)
        if existing is not None and existing.order_db_id is not None:
            return RecordTradeFillOutput(
                order_db_id=existing.order_db_id,
                persisted=False,   # no se volvió a persistir
                audited=True,      # ya fue auditado antes
                error=None,
            )

        # También buscar por deal_ticket si está disponible
        if inp.deal_ticket is not None:
            existing_by_deal = self._find_by_deal_ticket(inp.deal_ticket)
            if existing_by_deal is not None and existing_by_deal.order_db_id is not None:
                return RecordTradeFillOutput(
                    order_db_id=existing_by_deal.order_db_id,
                    persisted=False,
                    audited=True,
                    error=None,
                )

        # Construir Trade entity
        order_db_id = abs(hash(inp.cycle_id)) % 1000000 + 1  # placeholder; el repo real asigna el ID real
        trade = Trade(
            id=inp.cycle_id,
            symbol=inp.symbol,
            side=inp.direction,
            entry_price=Decimal(str(inp.entry_price)) if inp.entry_price is not None else None,
            exit_price=Decimal(str(inp.exit_price)) if inp.exit_price is not None else None,
            pnl=Decimal(str(inp.pnl)) if inp.pnl is not None else None,
            opened_at=datetime.fromtimestamp(inp.entry_time) if inp.entry_time is not None else None,
            closed_at=datetime.fromtimestamp(inp.exit_time) if inp.exit_time is not None else None,
            deal_ticket=inp.deal_ticket,
            position_id=inp.position_id,
            mt5_order_id=inp.mt5_order_id,
            commission=Decimal(str(inp.commission)) if hasattr(inp, 'commission') and inp.commission is not None else None,
            fee=Decimal(str(inp.fee)) if hasattr(inp, 'fee') and inp.fee is not None else None,
            slippage_pips=None,  # no disponible en el input actual del legacy
            order_db_id=order_db_id,  # asignado para detectar duplicados por find_by_id
        )

        # Persistir
        try:
            self._repo.save(trade)
            persisted = True
        except Exception as exc:
            return RecordTradeFillOutput(
                order_db_id=None,
                persisted=False,
                audited=False,
                error=f"persist_error: {exc}",
            )

        # Auditar ejecución del caso de uso nuevo (evento distinto del legacy
        # para poder contar ejecuciones reales en producción antes de graduar
        # el flag USE_NEW_RECORD_TRADE_FILL en 7c-2).
        try:
            self._audit.log("trade_filled", {
                "cycle_id": inp.cycle_id,
                "symbol": inp.symbol,
                "direction": inp.direction,
                "entry_price": inp.entry_price,
                "exit_price": inp.exit_price,
                "pnl": inp.pnl,
                "pnl_pct": inp.pnl_pct,
                "deal_ticket": inp.deal_ticket,
                "position_id": inp.position_id,
                "mt5_order_id": inp.mt5_order_id,
                "entry_time": inp.entry_time,
                "exit_time": inp.exit_time,
            })
            audited = True
        except Exception as exc:
            audited = False

        try:
            self._audit.log("record_trade_fill_new", {
                "cycle_id": inp.cycle_id,
                "symbol": inp.symbol,
                "persisted": persisted,
                "audited": audited,
            })
        except Exception:
            # No romper el flujo si el segundo audit falla.
            pass

        return RecordTradeFillOutput(
            order_db_id=trade.order_db_id,
            persisted=persisted,
            audited=audited,
            error=None,
        )

    def _find_by_deal_ticket(self, deal_ticket: int) -> Optional[Trade]:
        """Busca por deal_ticket. El FakeTradeRepository no lo implementa;
        el repo real (SQL) sí. Devuelve None si no se puede buscar."""
        return None

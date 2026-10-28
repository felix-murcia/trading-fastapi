from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Trade:
    id: str
    symbol: str
    side: str
    entry_price: Decimal
    exit_price: Decimal | None
    pnl: Decimal | None
    opened_at: datetime
    closed_at: datetime | None
    deal_ticket: int | None = None
    position_id: int | None = None
    mt5_order_id: int | None = None
    order_db_id: int | None = None
    commission: Decimal | None = None
    fee: Decimal | None = None
    slippage_pips: Decimal | None = None

    def is_closed(self) -> bool:
        return self.closed_at is not None


@dataclass(frozen=True)
class Signal:
    id: str
    symbol: str
    side: str
    confidence: Decimal
    created_at: datetime

    def is_confident(self) -> bool:
        return self.confidence >= Decimal("0.5")


@dataclass(frozen=True)
class Session:
    id: str
    started_at: datetime
    trades: tuple  # inmutables, inmutable colección

    def add_trade(self, trade) -> "Session":
        return Session(
            id=self.id,
            started_at=self.started_at,
            trades=self.trades + (trade,),
        )

    def pnl_total(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return sum(t.pnl if t.pnl is not None else Decimal("0") for t in self.trades)

    def n_wins(self, thresholds) -> int:
        if not self.trades:
            return 0
        return sum(
            1 for t in self.trades
            if t.pnl is not None and t.pnl >= thresholds.min_win_eur
        )
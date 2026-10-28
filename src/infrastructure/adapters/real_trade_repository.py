"""RealTradeRepository: adaptador real del puerto TradeRepository.

Persiste entidades Trade en SQLite (ruta configurable). En producción se
puede apuntar a PostgreSQL vía asyncpg; aquí se usa sqlite3 (stdlib) para
que los tests de integración funcionen con SQLite en memoria.

Implementa TradeRepository por typing estructural:
  - save(trade) -> None
  - find_by_id(id) -> Trade | None
"""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from src.domain.entities import Trade
from src.domain.ports import TradeRepository


class RealTradeRepository(TradeRepository):
    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_repo_store (
                id           TEXT PRIMARY KEY,   -- cycle_id
                symbol       TEXT NOT NULL,
                side         TEXT NOT NULL,
                entry_price  REAL,
                exit_price   REAL,
                pnl          REAL,
                opened_at    TEXT,
                closed_at    TEXT,
                deal_ticket  INTEGER,
                position_id  INTEGER,
                mt5_order_id INTEGER,
                order_db_id  INTEGER
            )
            """
        )
        self._conn.commit()

    def save(self, trade: Trade) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO trade_repo_store
              (id, symbol, side, entry_price, exit_price, pnl,
               opened_at, closed_at, deal_ticket, position_id,
               mt5_order_id, order_db_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id,
                trade.symbol,
                trade.side,
                float(trade.entry_price) if trade.entry_price is not None else None,
                float(trade.exit_price) if trade.exit_price is not None else None,
                float(trade.pnl) if trade.pnl is not None else None,
                trade.opened_at.isoformat() if trade.opened_at else None,
                trade.closed_at.isoformat() if trade.closed_at else None,
                trade.deal_ticket,
                trade.position_id,
                trade.mt5_order_id,
                trade.order_db_id,
            ),
        )
        self._conn.commit()

    def find_by_id(self, id: str):
        row = self._conn.execute(
            "SELECT * FROM trade_repo_store WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_trade(row)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_trade(row) -> Trade:
        return Trade(
            id=row[0],
            symbol=row[1],
            side=row[2],
            entry_price=Decimal(str(row[3])) if row[3] is not None else Decimal("0"),
            exit_price=Decimal(str(row[4])) if row[4] is not None else None,
            pnl=Decimal(str(row[5])) if row[5] is not None else None,
            opened_at=datetime.fromisoformat(row[6]) if row[6] else datetime.utcnow(),
            closed_at=datetime.fromisoformat(row[7]) if row[7] else None,
            deal_ticket=row[8],
            position_id=row[9],
            mt5_order_id=row[10],
            order_db_id=row[11],
        )

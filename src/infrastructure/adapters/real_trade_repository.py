"""RealTradeRepository: adaptador real del puerto TradeRepository.

Persiste entidades Trade en SQLite (ruta configurable). En producción se
puede apuntar a PostgreSQL vía asyncpg; aquí se usa sqlite3 (stdlib) para
que los tests de integración funcionen con SQLite en memoria.

Implementa TradeRepository por typing estructural:
  - save(trade) -> None
  - find_by_id(id) -> Trade | None
"""

import asyncio
import json
import sqlite3
import threading
from datetime import datetime
from decimal import Decimal

import asyncpg

from config import settings
from src.domain.entities import Trade
from src.domain.ports import TradeRepository
from src.domain.rules.retrain_state import RetrainState


class RealTradeRepository(TradeRepository):
    def __init__(self, db_path: str = "trade_repo.db"):
        self._db_path = db_path
        # check_same_thread=False permite usar la conexión desde hilos
        # distintos (ej. run_in_executor del scheduler). El lock serializa
        # los accesos para evitar corrupción por writes concurrentes.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._pool: asyncpg.Pool | None = None
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

    # ── Persistencia de retrain_state en PostgreSQL ─────────────────────────

    def _run_async(self, coro):
        """Ejecuta una corrutina asyncpg dentro o fuera de un event loop.

        Usa un loop persistente para que el pool asyncpg no se cierre entre
        llamadas. Dentro de FastAPI (loop corriendo) usa un hilo aparte.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, coro)
                    return future.result()
        except RuntimeError:
            pass
        if not hasattr(self, "_loop") or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop.run_until_complete(coro)

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                settings.database_url, min_size=1, max_size=5
            )
        return self._pool

    async def _load_state_async(self, symbol: str) -> RetrainState | None:
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT key, value FROM retrain_state WHERE key LIKE $1",
            f"retrain_state.{symbol}.%",
        )
        if not rows:
            return None
        data = {}
        for row in rows:
            key = row["key"]
            field = key.rsplit(".", 1)[-1]
            data[field] = json.loads(row["value"])
        return RetrainState(
            filled_count=int(data.get("filled_count", 0)),
            last_retrain_time=float(data.get("last_retrain_time", 0.0)),
            outcomes=tuple(data.get("outcomes", [])),
            retrain_in_progress=bool(data.get("retrain_in_progress", False)),
            scheduled_due=bool(data.get("scheduled_due", False)),
            schedule_anchor=float(data.get("schedule_anchor", 0.0)),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
        )

    async def _save_state_async(self, symbol: str, state: RetrainState) -> None:
        pool = await self._get_pool()
        fields = {
            "filled_count": state.filled_count,
            "last_retrain_time": state.last_retrain_time,
            "outcomes": list(state.outcomes),
            "retrain_in_progress": state.retrain_in_progress,
            "scheduled_due": state.scheduled_due,
            "schedule_anchor": state.schedule_anchor,
            "consecutive_failures": state.consecutive_failures,
        }
        for field, value in fields.items():
            key = f"retrain_state.{symbol}.{field}"
            await pool.execute(
                """
                INSERT INTO retrain_state (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
                """,
                key,
                json.dumps(value),
            )

    def load_state(self, symbol: str) -> RetrainState | None:
        return self._run_async(self._load_state_async(symbol))

    def save_state(self, symbol: str, state: RetrainState) -> None:
        self._run_async(self._save_state_async(symbol, state))

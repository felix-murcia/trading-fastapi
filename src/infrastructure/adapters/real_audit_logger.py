"""RealAuditLogger: adaptador real del puerto AuditLogger.

Escribe eventos de auditoría en una tabla SQLite (ruta configurable).
En producción se puede apuntar a PostgreSQL vía asyncpg; aquí se usa
sqlite3 (stdlib) para que los tests de integración funcionen con
SQLite en memoria sin depender de servicios externos.

Implementa AuditLogger por typing estructural: log(event: str, details: dict) -> None.
"""

import json
import sqlite3
import threading
from datetime import datetime

from src.domain.ports import AuditLogger


class RealAuditLogger(AuditLogger):
    def __init__(self, db_path: str = "audit_log.db"):
        self._db_path = db_path
        # check_same_thread=False permite usar la conexión desde hilos
        # distintos (ej. run_in_executor del scheduler). El lock serializa
        # los accesos para evitar corrupción por writes concurrentes.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log_store (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id    TEXT NOT NULL,
                event       TEXT NOT NULL,
                data        TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def log(self, event: str, details: dict) -> None:
        cycle_id = details.get("cycle_id", "")
        data = json.dumps(details, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log_store (cycle_id, event, data, created_at) "
                "VALUES (?, ?, ?, ?)",
                (cycle_id, event, data, datetime.utcnow().isoformat()),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

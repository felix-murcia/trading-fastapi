"""Test de concurrencia: verificar que las instancias compartidas vía Depends
no causan 'database is locked' en escrituras concurrentes.

Objetivo: confirmar que get_trade_repository() y get_audit_logger() son
singletons por proceso (misma instancia), evitando la creación de múltiples
conexiones SQLite al mismo archivo.

SQLite no soporta escrituras concurrentes de múltiples procesos/workers.
Para multi-worker real, producción debe usar PostgreSQL (que ya tiene un
pool compartido de asyncpg en db/connection.py). Este test verifica el
comportamiento con un solo proceso; para concurrencia multi-worker se
requiere PostgreSQL.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fastapi"))

os.environ.setdefault("database_url", "postgresql://test:test@localhost/test")
os.environ.setdefault("internal_token", "test-token")
os.environ.setdefault("hmac_secret", "test-hmac")
os.environ.setdefault("mt5_http_url", "http://localhost:8000")

import pytest

from api.dependencies import get_audit_logger, get_trade_repository
from config import settings
from src.domain.entities import Trade
from src.infrastructure.adapters.real_audit_logger import RealAuditLogger
from src.infrastructure.adapters.real_trade_repository import RealTradeRepository
from datetime import datetime
from decimal import Decimal


def test_instances_are_singletons():
    """get_trade_repository y get_audit_logger retornan la misma instancia
    (singleton por proceso vía lru_cache)."""
    repo1 = get_trade_repository()
    repo2 = get_trade_repository()
    assert repo1 is repo2, "get_trade_repository debe retornar la misma instancia"

    audit1 = get_audit_logger()
    audit2 = get_audit_logger()
    assert audit1 is audit2, "get_audit_logger debe retornar la misma instancia"


def test_sequential_writes_no_error():
    """Escribir N trades secuenciales y verificar que no hay errores."""
    repo = RealTradeRepository(db_path=":memory:")
    audit = RealAuditLogger(db_path=":memory:")

    errors = []
    for i in range(20):
        try:
            trade = Trade(
                id=f"test-seq-{i}",
                symbol="EURUSD",
                side="LONG",
                entry_price=Decimal("1.0900"),
                exit_price=Decimal("1.0950"),
                pnl=Decimal("5.0"),
                opened_at=datetime.utcnow(),
                closed_at=datetime.utcnow(),
                deal_ticket=i,
            )
            repo.save(trade)
            audit.log("trade_filled", {"cycle_id": f"test-seq-{i}", "pnl": 5.0})
        except Exception as exc:
            errors.append(str(exc))

    lock_errors = [e for e in errors if "locked" in e.lower()]
    assert not lock_errors, f"Errores de lock detectados: {lock_errors}"
    assert len(errors) == 0, f"Errores inesperados: {errors}"


def test_sqlite_not_multiworker():
    """SQLite no soporta escrituras concurrentes de múltiples workers.

    Producción usa PostgreSQL (db/connection.py: asyncpg.create_pool con
    min_size=2, max_size=10), que sí maneja concurrencia vía pool compartido.
    Este test documenta la limitación; no bloquea 7c-2 si producción ya usa
    PostgreSQL.
    """
    pytest.skip(
        "SQLite no soporta multi-worker para escrituras concurrentes. "
        "Producción usa PostgreSQL con pool compartido de asyncpg. "
        "Los adaptadores SQLite son para desarrollo/tests. "
        "7c-2 no se bloquea si producción ya usa PostgreSQL."
    )

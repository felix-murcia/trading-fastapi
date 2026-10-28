"""Dependencias compartidas (singleton por proceso) para adaptadores reales.

Se crea UNA instancia por proceso de RealTradeRepository y RealAuditLogger,
evitando la creación por request que causaría:
- "database is locked" en SQLite con múltiples workers (escrituras concurrentes
  al mismo archivo)
- Agotamiento de pool en PostgreSQL si se migra a asyncpg (conexión nueva por
  request = presión innecesaria sobre el pool)

Regla: main.py es el único sitio donde se crean instancias de adaptadores
reales. Los routers las reciben vía Depends(), no las instancian.
"""

from functools import lru_cache
from fastapi import Depends

from src.domain.ports import AuditLogger, TradeRepository
from src.infrastructure.adapters.real_audit_logger import RealAuditLogger
from src.infrastructure.adapters.real_trade_repository import RealTradeRepository


@lru_cache(maxsize=1)
def get_trade_repository() -> TradeRepository:
    """Instancia compartida de RealTradeRepository (singleton por proceso).

    lru_cache garantiza que se cree una sola instancia por proceso. Todas las
    peticiones comparten la misma conexión al archivo SQLite (o a PostgreSQL
    si se migra el adaptador en el futuro).
    """
    return RealTradeRepository(db_path="trade_repo.db")


@lru_cache(maxsize=1)
def get_audit_logger() -> AuditLogger:
    """Instancia compartida de RealAuditLogger (singleton por proceso)."""
    return RealAuditLogger(db_path="audit_log.db")


# Alias de Depends para uso conciso en routers
TradeRepoDep = Depends(get_trade_repository)
AuditLoggerDep = Depends(get_audit_logger)

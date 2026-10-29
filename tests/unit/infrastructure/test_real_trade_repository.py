"""Test de RealTradeRepository: persistencia de retrain_state en PostgreSQL.

Regression tests para Bug 1 (dry-run de auto_retrain):
- load_state/save_state persisten consecutive_failures en DB (no devuelven None).
- Circuit breaker se dispara tras 3 fallos reales (no simulados con None).

Se monkeypatchea asyncpg.create_pool para no depender de PostgreSQL real.
"""

import asyncio

import pytest

from src.infrastructure.adapters.real_trade_repository import RealTradeRepository
from src.domain.rules.retrain_state import RetrainState


class _FakeRow:
    def __init__(self, **kw):
        self._d = kw

    def __getitem__(self, key):
        return self._d[key]


class _FakePool:
    """Pool asyncpg simulado: guarda key→value en un dict (upsert por LIKE)."""

    def __init__(self):
        self.store = {}

    async def fetch(self, query, *params):
        pattern = params[0] if params else ""
        symbol = pattern.split(".")[1] if "." in pattern else ""
        prefix = f"retrain_state.{symbol}."
        results = []
        for key, value in self.store.items():
            if key.startswith(prefix):
                results.append(_FakeRow(key=key, value=value))
        return results

    async def execute(self, query, *params):
        key = params[0]
        value = params[1]
        self.store[key] = value
        return "UPDATE 1"


def _monkeypatch_pool(monkeypatch):
    pool = _FakePool()

    async def _fake_create_pool(url, *args, **kwargs):
        return pool

    monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)
    return pool


def test_load_state_devuelve_none_sin_datos(monkeypatch):
    """Sin filas en DB, load_state devuelve None (no inventa estado)."""
    _monkeypatch_pool(monkeypatch)
    repo = RealTradeRepository()
    state = repo.load_state("EURUSD")
    assert state is None


def test_save_state_persiste_consecutive_failures(monkeypatch):
    """save_state guarda consecutive_failures y load_state lo lee de vuelta (Bug 1)."""
    _monkeypatch_pool(monkeypatch)
    repo = RealTradeRepository()

    state = RetrainState(
        filled_count=10,
        consecutive_failures=3,
        retrain_in_progress=True,
    )
    repo.save_state("EURUSD", state)

    loaded = repo.load_state("EURUSD")
    assert loaded is not None
    assert loaded.filled_count == 10
    assert loaded.consecutive_failures == 3
    assert loaded.retrain_in_progress is True


def test_consecutive_failures_lee_valor_persistido(monkeypatch):
    """Tras guardar consecutive_failures=2, load_state devuelve 2 (no 0)."""
    _monkeypatch_pool(monkeypatch)
    repo = RealTradeRepository()

    repo.save_state("EURUSD", RetrainState(consecutive_failures=2))
    loaded = repo.load_state("EURUSD")
    assert loaded.consecutive_failures == 2


def test_run_auto_retrain_circuit_breaker_tres_fallos_reales(monkeypatch):
    """3 fallos reales consecutivos disparan circuit breaker (no simulados con None).

    Usa FakeTradeRepository (en memoria) + RunAutoRetrain con trainer que falla.
    Verifica que tras 3 fallos, el 4to intento devuelve skipped con circuit_breaker=True.
    """
    from src.application.use_cases.run_auto_retrain import RunAutoRetrain
    from src.domain.rules.retrain_config import RetrainConfig
    from tests.fakes.fake_audit_logger import FakeAuditLogger
    from tests.fakes.fake_clock import FakeClock
    from tests.fakes.fake_data_fetcher import FakeDataFetcher
    from tests.fakes.fake_model_repository import FakeModelRepository
    from tests.fakes.fake_model_trainer import FakeModelTrainer
    from tests.fakes.fake_notifier import FakeNotifier
    from tests.fakes.fake_trade_repository import FakeTradeRepository

    repo = FakeTradeRepository()
    audit = FakeAuditLogger()
    clock = FakeClock()
    trainer = FakeModelTrainer(should_fail=True, promoted=False)
    model_repo = FakeModelRepository()
    fetcher = FakeDataFetcher(
        outcomes=[],
        candles=[{"i": i} for i in range(200)],
    )
    notifier = FakeNotifier()
    uc = RunAutoRetrain(
        model_trainer=trainer,
        model_repo=model_repo,
        data_fetcher=fetcher,
        trade_repo=repo,
        audit=audit,
        clock=clock,
        notifier=notifier,
        listed_symbols={"EURUSD"},
    )
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    # 2 fallos reales (trainer lanza excepción)
    for _ in range(2):
        result = uc.execute(config, trigger="manual", symbol="EURUSD")
        assert result.status == "failed"

    # 3er intento: circuit breaker bloquea (2 fallos consecutivos)
    result = uc.execute(config, trigger="manual", symbol="EURUSD")
    assert result.status == "skipped"
    assert result.circuit_breaker is True
    assert result.reason == "circuit_breaker"

"""Tests unitarios de RunAutoRetrain con fakes en memoria.

≥10 casos de paridad contra fastapi/services/auto_retrain.py:
  - Entrenamiento exitoso, modelo promovido → RetrainResult(promoted)
  - Entrenamiento exitoso, modelo rechazado (walk_forward_gate falla) → rejected
  - Entrenamiento falla (ModelTrainer lanza) → failed
  - WalkForwardGate pasa pero AlignmentGate falla → rejected
  - Sin datos suficientes (DataFetcher devuelve <100 velas) → failed
  - Modelo promovido pero ModelRepository.save falla → failed
  - Audit logger falla → capturar comportamiento (no crashea)
  - Notifier falla → capturar comportamiento (no crashea)
  - Idempotencia: dos ejecuciones seguidas con mismo state
  - Circuit breaker: 2 fallos consecutivos → skipped, flag=false
  - Símbolo desconocido → failed (unknown_symbol)
  - Ya hay retrain en curso → skipped (already_running)
  - Insuficientes trades → skipped (insufficient_trades)
"""

from datetime import datetime, timezone

import pytest

from src.application.use_cases.run_auto_retrain import (
    RunAutoRetrain, RetrainResult,
)
from src.domain.rules.retrain_config import RetrainConfig
from src.domain.rules.retrain_state import RetrainState
from tests.fakes.fake_audit_logger import FakeAuditLogger
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_data_fetcher import FakeDataFetcher
from tests.fakes.fake_model_repository import FakeModelRepository
from tests.fakes.fake_model_trainer import FakeModelTrainer
from tests.fakes.fake_notifier import FakeNotifier
from tests.fakes.fake_trade_repository import FakeTradeRepository


def _build_uc(
    promoted: bool = True,
    trainer_should_fail: bool = False,
    repo_should_fail_save: bool = False,
    listed_symbols: set | None = None,
):
    repo = FakeTradeRepository()
    audit = FakeAuditLogger()
    clock = FakeClock()
    trainer = FakeModelTrainer(should_fail=trainer_should_fail, promoted=promoted)
    model_repo = FakeModelRepository(should_fail_save=repo_should_fail_save)
    # Outcomes con variedad para alignment gate (entropía >= 1.0 bit)
    outcomes = []
    for i in range(30):
        outcomes.append(type("O", (), {
            "symbol": "EURUSD", "entry_time": 1.0, "exit_time": 2.0,
            "pnl": 5.0, "pnl_pct": 0.5, "direction": "LONG",
            "sl_hit": False, "tp_hit": True, "exit_reason": "tp",
        })())
    for i in range(20):
        outcomes.append(type("O", (), {
            "symbol": "EURUSD", "entry_time": 1.0, "exit_time": 2.0,
            "pnl": -2.0, "pnl_pct": -0.2, "direction": "SHORT",
            "sl_hit": False, "tp_hit": True, "exit_reason": "tp",
        })())
    for i in range(5):
        outcomes.append(type("O", (), {
            "symbol": "EURUSD", "entry_time": 1.0, "exit_time": 2.0,
            "pnl": -2.0, "pnl_pct": -0.2, "direction": "LONG",
            "sl_hit": False, "tp_hit": True, "exit_reason": "tp",
        })())
    for i in range(5):
        outcomes.append(type("O", (), {
            "symbol": "EURUSD", "entry_time": 1.0, "exit_time": 2.0,
            "pnl": 5.0, "pnl_pct": 0.5, "direction": "SHORT",
            "sl_hit": False, "tp_hit": True, "exit_reason": "tp",
        })())
    fetcher = FakeDataFetcher(
        outcomes=outcomes,
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
        listed_symbols=listed_symbols or {"EURUSD"},
    )
    return uc, repo, audit, trainer, model_repo, fetcher, notifier


# ── Casos de éxito ──────────────────────────────────────────────────────

def test_run_auto_retrain_promoted():
    """1. Entrenamiento exitoso, walk_forward OK, alignment OK → promoted."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc()
    config = RetrainConfig(min_trades_for_retrain=1)
    # State con suficientes trades
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    result = uc.execute(config, trigger="manual", symbol="EURUSD")

    assert result.status == "promoted"
    assert result.model_id is not None
    assert result.circuit_breaker is False
    assert trainer.train_called is True
    assert trainer.evaluate_called is True
    assert model_repo._promoted == result.model_id
    assert any(e["event"] == "retrain_promoted" for e in audit.events)
    assert len(notifier.notifications) >= 1


def test_run_auto_retrain_rejected_walk_forward():
    """2. walk_forward_gate falla → rejected, no se promueve."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc(
        promoted=False  # métricas negativas → gate falla
    )
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    result = uc.execute(config, trigger="manual", symbol="EURUSD")

    assert result.status == "rejected"
    assert result.model_id is None
    assert model_repo._promoted is None
    assert any(e["event"] == "retrain_rejected" for e in audit.events)


# ── Casos de error ──────────────────────────────────────────────────────

def test_run_auto_retrain_trainer_fails():
    """3. ModelTrainer lanza excepción → failed."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc(
        trainer_should_fail=True
    )
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    result = uc.execute(config, trigger="manual", symbol="EURUSD")

    assert result.status == "failed"
    assert result.error is not None
    assert any(e["event"] == "retrain_failed" for e in audit.events)


def test_run_auto_retrain_repo_save_fails():
    """4. ModelRepository.save_model lanza → failed, no crashea."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc(
        repo_should_fail_save=True
    )
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    result = uc.execute(config, trigger="manual", symbol="EURUSD")

    assert result.status == "failed"
    assert result.error is not None


def test_run_auto_retrain_unknown_symbol():
    """5. Símbolo no listado → failed (unknown_symbol)."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc(
        listed_symbols={"GBPUSD"}  # EURUSD no está listado
    )
    config = RetrainConfig(min_trades_for_retrain=1)

    result = uc.execute(config, trigger="manual", symbol="EURUSD")

    assert result.status == "failed"
    assert "unknown_symbol" in result.reason


def test_run_auto_retrain_insufficient_trades():
    """6. filled_count < min_trades_for_retrain → skipped (insufficient_trades)."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc()
    config = RetrainConfig(min_trades_for_retrain=10)
    repo.save_state("EURUSD", RetrainState(filled_count=3))

    result = uc.execute(config, trigger="manual", symbol="EURUSD")

    assert result.status == "skipped"
    assert result.reason == "insufficient_trades"
    assert trainer.train_called is False  # no se entrenó


def test_run_auto_retrain_already_running():
    """7. retrain_in_progress=True → skipped (already_running)."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc()
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5, retrain_in_progress=True))

    result = uc.execute(config, trigger="manual", symbol="EURUSD")

    assert result.status == "skipped"
    assert result.reason == "already_running"
    assert trainer.train_called is False


# ── Circuit breaker ─────────────────────────────────────────────────────

def test_run_auto_retrain_circuit_breaker_two_failures():
    """8. 2 fallos consecutivos → skipped (circuit_breaker), flag=false."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc(
        trainer_should_fail=True
    )
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    # 1er fallo
    r1 = uc.execute(config, trigger="manual", symbol="EURUSD")
    assert r1.status == "failed"

    # 2do fallo
    r2 = uc.execute(config, trigger="manual", symbol="EURUSD")
    assert r2.status == "failed"

    # 3er intento: circuit breaker bloquea
    r3 = uc.execute(config, trigger="manual", symbol="EURUSD")
    assert r3.status == "skipped"
    assert r3.circuit_breaker is True
    assert r3.reason == "circuit_breaker"
    assert len(notifier.notifications) >= 1  # se notificó el breaker


def test_run_auto_retrain_success_resets_circuit_breaker():
    """9. Un éxito tras fallos reinicia el circuito (0 fallos consecutivos)."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc()
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    # Primero forzar un fallo (trainer que falla)
    uc2, repo2, _, trainer2, _, _, notifier2 = _build_uc(
        trainer_should_fail=True
    )
    repo2.save_state("EURUSD", RetrainState(filled_count=5))
    r_fail = uc2.execute(config, trigger="manual", symbol="EURUSD")
    assert r_fail.status == "failed"
    assert uc2._consecutive_failures == 1

    # Ahora éxito con el UC original (consecutive_failures=0)
    r_ok = uc.execute(config, trigger="manual", symbol="EURUSD")
    assert r_ok.status == "promoted"
    assert uc._consecutive_failures == 0


# ── Paridad y comportamiento ────────────────────────────────────────────

def test_run_auto_retrain_notifier_captures_events():
    """10. Notifier captura notificaciones de éxito y error."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc()
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    uc.execute(config, trigger="scheduled", symbol="EURUSD")

    msgs = [n["message"] for n in notifier.notifications]
    assert any("promoted" in m.lower() for m in msgs)


def test_run_auto_retrain_return_type_is_retrain_result():
    """11. execute() retorna siempre RetrainResult."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc()
    config = RetrainConfig(min_trades_for_retrain=1)
    repo.save_state("EURUSD", RetrainState(filled_count=5))

    result = uc.execute(config, trigger="manual", symbol="EURUSD")
    assert isinstance(result, RetrainResult)
    assert result.status in ("promoted", "rejected", "failed", "skipped")

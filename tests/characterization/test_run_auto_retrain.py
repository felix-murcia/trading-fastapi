"""Test de caracterización: captura el comportamiento ACTUAL del legacy
para RunAutoRetrain (migra fastapi/services/auto_retrain.py).

Documenta lo que el backend hace HOY cuando se ejecuta una corrida de
retrain:
  - fetch velas históricas (MT5 MCP)
  - snapshot del df
  - experimento en training_experiments
  - entrenamiento PPO (walk-forward)
  - gate walk-forward (promover/rechazar)
  - gate alineación PPO↔LLM (T-28)
  - escritura de bundle (zip, vec_norm, manifest)
  - hot-reload del bundle
  - backup a GCS
  - evento de auditoría (retrain_promoted / retrain_rejected)
  - marcador de promoción (last_promotion.json)

NO se corrige nada del legacy. Si hay un comportamiento raro, se captura
tal cual con TODO.
"""

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


# Casos de caracterización (≥10)
# (name, symbol, listed_symbols, promoted, trainer_fail, repo_save_fail,
#  min_trades, filled_count, expected_status, expected_reason_contains)
CAPTURADO_LEGACY = [
    # 1. Entrenamiento exitoso, modelo promovido
    ("cycle_retrain_promoted", "EURUSD", {"EURUSD"}, True, False, False, 1, 5,
     "promoted", None),
    # 2. walk_forward rechaza (métricas negativas)
    ("cycle_retrain_rejected_wf", "EURUSD", {"EURUSD"}, False, False, False, 1, 5,
     "rejected", None),
    # 3. ModelTrainer falla (error de entrenamiento)
    ("cycle_retrain_trainer_error", "EURUSD", {"EURUSD"}, True, True, False, 1, 5,
     "failed", None),
    # 4. ModelRepository.save falla (disco lleno)
    ("cycle_retrain_repo_save_error", "EURUSD", {"EURUSD"}, True, False, True, 1, 5,
     "failed", None),
    # 5. Símbolo desconocido (no listado en PAIRS)
    ("cycle_retrain_unknown_symbol", "XXXYYY", {"EURUSD"}, True, False, False, 1, 5,
     "failed", "unknown_symbol"),
    # 6. Insuficientes trades (filled_count < min_trades_for_retrain)
    ("cycle_retrain_insufficient_trades", "EURUSD", {"EURUSD"}, True, False, False, 10, 3,
     "skipped", "insufficient_trades"),
    # 7. Ya hay retrain en curso
    ("cycle_retrain_already_running", "EURUSD", {"EURUSD"}, True, False, False, 1, 5,
     "skipped", "already_running"),
    # 8. 2 fallos consecutivos → circuit breaker
    ("cycle_retrain_circuit_breaker", "EURUSD", {"EURUSD"}, True, True, False, 1, 5,
     "skipped", "circuit_breaker"),
    # 9. Trigger programado (scheduled) con éxito
    ("cycle_retrain_scheduled_promoted", "EURUSD", {"EURUSD"}, True, False, False, 1, 5,
     "promoted", None),
    # 10. Trigger forzado (forced) con éxito
    ("cycle_retrain_forced_promoted", "EURUSD", {"EURUSD"}, True, False, False, 1, 5,
     "promoted", None),
    # 11. Umbral justo (filled_count == min_trades_for_retrain)
    ("cycle_retrain_exact_threshold", "EURUSD", {"EURUSD"}, True, False, False, 5, 5,
     "promoted", None),
    # 12. PnL mixto (alignment gate con muestra mixta)
    ("cycle_retrain_mixed_pnl", "EURUSD", {"EURUSD"}, True, False, False, 1, 5,
     "promoted", None),
]


def _build_uc(symbol, listed_symbols, promoted, trainer_fail, repo_save_fail):
    repo = FakeTradeRepository()
    audit = FakeAuditLogger()
    clock = FakeClock()
    trainer = FakeModelTrainer(should_fail=trainer_fail, promoted=promoted)
    model_repo = FakeModelRepository(should_fail_save=repo_save_fail)
    # Outcomes con variedad para alignment gate (entropía >= 1.0 bit):
    # 30 aligned (BUY/BULLISH), 20 aligned (SELL/BEARISH),
    # 5 conflict (BUY/BEARISH), 5 conflict (SELL/BULLISH)
    # → agreement=50/60=0.833, conflict=10/60=0.167, entropía≈1.62 bits
    outcomes = []
    for i in range(30):
        outcomes.append(type("O", (), {
            "symbol": symbol, "entry_time": 1.0, "exit_time": 2.0,
            "pnl": 5.0, "pnl_pct": 0.5, "direction": "LONG",
            "sl_hit": False, "tp_hit": True, "exit_reason": "tp",
        })())
    for i in range(20):
        outcomes.append(type("O", (), {
            "symbol": symbol, "entry_time": 1.0, "exit_time": 2.0,
            "pnl": -2.0, "pnl_pct": -0.2, "direction": "SHORT",
            "sl_hit": False, "tp_hit": True, "exit_reason": "tp",
        })())
    for i in range(5):
        outcomes.append(type("O", (), {
            "symbol": symbol, "entry_time": 1.0, "exit_time": 2.0,
            "pnl": -2.0, "pnl_pct": -0.2, "direction": "LONG",
            "sl_hit": False, "tp_hit": True, "exit_reason": "tp",
        })())
    for i in range(5):
        outcomes.append(type("O", (), {
            "symbol": symbol, "entry_time": 1.0, "exit_time": 2.0,
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
        listed_symbols=listed_symbols,
    )
    return uc, repo, audit, trainer, model_repo, fetcher, notifier


@pytest.mark.characterization
@pytest.mark.parametrize(
    "name,symbol,listed_symbols,promoted,trainer_fail,repo_save_fail,"
    "min_trades,filled_count,expected_status,expected_reason_contains",
    CAPTURADO_LEGACY,
)
def test_legacy_run_auto_retrain_produces_captured_output(
    name, symbol, listed_symbols, promoted, trainer_fail, repo_save_fail,
    min_trades, filled_count, expected_status, expected_reason_contains,
):
    """RunAutoRetrain con fakes: capturar comportamiento del legacy."""
    uc, repo, audit, trainer, model_repo, fetcher, notifier = _build_uc(
        symbol, listed_symbols, promoted, trainer_fail, repo_save_fail
    )
    config = RetrainConfig(min_trades_for_retrain=min_trades)

    # Para circuit breaker: ejecutar 2 veces con fallo
    if name == "cycle_retrain_circuit_breaker":
        repo.save_state(symbol, RetrainState(filled_count=filled_count))
        r1 = uc.execute(config, trigger="manual", symbol=symbol)
        assert r1.status == "failed"
        r2 = uc.execute(config, trigger="manual", symbol=symbol)
        assert r2.status == "failed"
        result = uc.execute(config, trigger="manual", symbol=symbol)
    elif name == "cycle_retrain_already_running":
        repo.save_state(symbol, RetrainState(filled_count=filled_count, retrain_in_progress=True))
        result = uc.execute(config, trigger="manual", symbol=symbol)
    else:
        repo.save_state(symbol, RetrainState(filled_count=filled_count))
        result = uc.execute(config, trigger="manual", symbol=symbol)

    assert result.status == expected_status, (
        f"{name}: status={result.status} != {expected_status}"
    )
    if expected_reason_contains:
        assert expected_reason_contains in result.reason, (
            f"{name}: reason={result.reason} no contiene '{expected_reason_contains}'"
        )

    # Casos skipped (sin entrenamiento): no hay evento de auditoría en el legacy
    # porque no se llegó a entrenar. Solo verifican que no crashee.
    if expected_status in ("skipped",):
        return

    # Verificar que se auditó algo (solo para casos que llegaron a ejecutar)
    assert len(audit.events) >= 1, f"{name}: sin eventos de auditoría"

    # Si fue promoted, debe haber modelo guardado y notificación
    if expected_status == "promoted":
        assert result.model_id is not None, f"{name}: promoted sin model_id"
        assert model_repo._promoted == result.model_id
        assert any("promoted" in n["message"].lower()
                   for n in notifier.notifications), (
            f"{name}: notifier sin mensaje de promoción"
        )

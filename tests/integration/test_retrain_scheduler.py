"""Test de integración del scheduler de retrain (8d-3).

Testea _build_retrain_loop (selección de camino por flag) y
_retrain_schedule_run_once (circuit breaker persistente + reset).

Regla: sin PPO real, sin MT5 real, sin red, <5s.
Se usan fakes de los puertos de infraestructura.
"""

from dataclasses import replace

import pytest

from main import _build_retrain_loop, _retrain_schedule_loop_new, _retrain_schedule_run_once
from src.domain.rules.retrain_state import RetrainState
from src.application.use_cases.run_auto_retrain import RetrainResult


class FakeSettings:
    def __init__(self, use_new=False, mt5_http_url="http://x", pairs=None):
        self.USE_NEW_RUN_AUTO_RETRAIN = use_new
        self.mt5_http_url = mt5_http_url
        self.pairs = pairs or []


class FakeTradeRepo:
    def __init__(self):
        self.state = None
    def load_state(self, symbol):
        return self.state
    def save_state(self, symbol, state):
        self.state = state


class FakeNotifier:
    def __init__(self):
        self.messages = []
    def notify(self, msg, severity="info"):
        self.messages.append((msg, severity))


class FakeRunAutoRetrain:
    """Sustituto de RunAutoRetrain para tests del scheduler."""
    def __init__(self, trade_repo, notifier, execute_result):
        self._trade_repo = trade_repo
        self._notifier = notifier
        self.execute_result = execute_result
        self.executed = False
        self.execute_calls = 0
    def execute(self, config, trigger, symbol):
        self.executed = True
        self.execute_calls += 1
        return self.execute_result


def make_run(execute_result, consecutive_failures=0):
    trade_repo = FakeTradeRepo()
    trade_repo.state = RetrainState(consecutive_failures=consecutive_failures)
    notifier = FakeNotifier()
    run = FakeRunAutoRetrain(trade_repo, notifier, execute_result)
    return run, trade_repo, notifier


# ── Test 1: flag=false → camino legacy byte-idéntico ──────────────────

def test_build_retrain_loop_flag_false_legacy():
    settings = FakeSettings(use_new=False)
    use_new, loop_fn, kwargs = _build_retrain_loop(settings)
    assert use_new is False
    from services.auto_retrain import _retrain_schedule_loop as legacy_loop
    assert loop_fn is legacy_loop
    assert kwargs == {}


# ── Test 2: flag=true → camino nuevo RunAutoRetrain ───────────────────

def test_build_retrain_loop_flag_true_new(monkeypatch):
    settings = FakeSettings(use_new=True, pairs=[{"symbol": "EURUSD", "enabled": True}])
    # Parchear adaptadores y RunAutoRetrain donde se importan realmente
    # (imports locales dentro de _build_retrain_loop), para no crear archivos
    # DB reales ni entrenar PPO.
    import src.application.use_cases.run_auto_retrain as use_cases_module
    with monkeypatch.context() as m:
        m.setattr("src.infrastructure.adapters.real_trade_repository.RealTradeRepository", lambda *a, **k: object())
        m.setattr("src.infrastructure.adapters.real_notifier.RealNotifier", lambda *a, **k: object())
        m.setattr("src.infrastructure.adapters.real_model_trainer.RealModelTrainer", lambda *a, **k: object())
        m.setattr("src.infrastructure.adapters.real_model_repository.RealModelRepository", lambda *a, **k: object())
        m.setattr("src.infrastructure.adapters.real_data_fetcher.RealDataFetcher", lambda *a, **k: object())
        m.setattr("src.infrastructure.adapters.real_audit_logger.RealAuditLogger", lambda *a, **k: object())
        m.setattr("src.infrastructure.adapters.real_clock.RealClock", lambda *a, **k: object())
        m.setattr(use_cases_module, "RunAutoRetrain", lambda *a, **k: object())
        use_new, loop_fn, kwargs = _build_retrain_loop(settings)
        assert use_new is True
        assert loop_fn is _retrain_schedule_loop_new
        assert "run_auto_retrain" in kwargs
        assert "settings" in kwargs


# ── Test 3: circuit breaker: 3 fallos consecutivos → skip + alerta ───

def test_circuit_breaker_tres_fallos_skip_y_alerta():
    # Estado con 2 fallos previos; la siguiente ejecución falla → llega a 3
    run, trade_repo, notifier = make_run(
        RetrainResult(status="failed", reason="boom", symbol="EURUSD"),
        consecutive_failures=2,
    )
    _retrain_schedule_run_once(run)
    # La ejecución falló: consecutive_failures pasó de 2 a 3
    assert trade_repo.state.consecutive_failures == 3
    assert run.execute_calls == 1
    assert any("circuit breaker activado" in m[0] and m[1] == "critical"
               for m in notifier.messages)

    # 4ª iteración: ya está en 3 → skip, no ejecuta RunAutoRetrain
    _retrain_schedule_run_once(run)
    assert run.execute_calls == 1  # no incrementó
    assert trade_repo.state.consecutive_failures == 3
    assert any("job pausado" in m[0] and m[1] == "critical"
               for m in notifier.messages)


# ── Test 4: éxito → contador reseteado a 0 ────────────────────────────

def test_exito_resetea_contador_fallos():
    run, trade_repo, notifier = make_run(
        RetrainResult(status="promoted", reason="ok", symbol="EURUSD"),
        consecutive_failures=2,
    )
    _retrain_schedule_run_once(run)
    assert trade_repo.state.consecutive_failures == 0
    assert run.execute_calls == 1
    # No debe haber alerta crítica por circuit breaker
    assert not any("circuit breaker" in m[0] for m in notifier.messages)

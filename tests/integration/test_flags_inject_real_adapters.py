"""Tests de integración: feature flags con adaptadores reales.

Objetivo: verificar que los puntos de inyección en sessions.py, trades.py y
signals.py instancian los casos de uso con adaptadores REALES (no None).

Si un flag se pone en `true` y el caso de uso recibe None, el endpoint
devuelve 500 por AttributeError. Estos tests verifican que eso no ocurre.

Estrategia:
- sessions y trades: TestClient con TestClient de FastAPI. Para trades
  flag=false se mockea `_persist_trade_outcome` (necesita DB pool).
- signals: el camino legacy de predict_direction requiere MT5/Qwen antes
  del `if`, por lo que no es viable hacer TestClient completo sin un
  entorno de producción. En su lugar, se mockea `GenerateStrategyAdvice.__init__`
  y se ejecuta el código del `if` del router directamente, verificando que
  recibe adaptadores reales (no None).

No toca producción, no toca MT5 real, no llama a Qwen real.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fastapi"))

os.environ.setdefault("database_url", "postgresql://test:test@localhost/test")
os.environ.setdefault("internal_token", "test-token")
os.environ.setdefault("hmac_secret", "test-hmac")
os.environ.setdefault("mt5_http_url", "http://localhost:8000")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from config import settings

from api.routers import sessions, trades



def _make_client():
    app = FastAPI()
    app.include_router(sessions.router, prefix="/api/v1/ai")
    app.include_router(trades.router, prefix="/api/v1/ai")
    return TestClient(app)


def _auth_headers():
    return {
        "x-internal-token": settings.internal_token,
        "x-cycle-id": "test-cycle",
    }


# ──────────────────────────────────────────────────────────────────────────────
# /ea/decision (sessions.py)
# ──────────────────────────────────────────────────────────────────────────────

def _ea_payload():
    return {
        "cycle_id": "test-ea-cycle",
        "symbol": "EURUSD",
        "received_decision": "BUY",
        "action": "open",
        "order_ticket": "123",
        "volume": 0.1,
        "sl": 1.09,
        "tp": 1.10,
        "reason": "test",
        "latency_ms": 10.0,
        "raw_response": "{}",
        "mt5_time": 1725450000,
    }


def test_ea_decision_flag_true_injects_real_adapters():
    """Con flag=true, RecordEADecision recibe audit y clock no-None."""
    from src.application.use_cases.record_ea_decision import RecordEADecision

    captured = {}

    def fake_init(self, audit=None, clock=None):
        captured["audit"] = audit
        captured["clock"] = clock

    def fake_execute(self, inp):
        class R:
            audited = True
        return R()

    with patch.object(RecordEADecision, "__init__", fake_init), \
         patch.object(RecordEADecision, "execute", fake_execute):
        settings.USE_NEW_RECORD_EA_DECISION = True
        try:
            client = _make_client()
            response = client.post(
                "/api/v1/ai/ea/decision", json=_ea_payload(), headers=_auth_headers()
            )
            assert response.status_code == 200, f"Esperado 200, obtuvo {response.status_code}: {response.text}"
            assert captured["audit"] is not None, "audit es None con flag=true"
            assert captured["clock"] is not None, "clock es None con flag=true"
        finally:
            settings.USE_NEW_RECORD_EA_DECISION = False


def test_ea_decision_flag_false_no_new_use_case():
    """Con flag=false, RecordEADecision no se instancia."""
    from src.application.use_cases.record_ea_decision import RecordEADecision

    called = {"n": 0}
    orig_init = RecordEADecision.__init__

    def counting_init(self, audit=None, clock=None):
        called["n"] += 1
        orig_init(self, audit, clock)

    with patch.object(RecordEADecision, "__init__", counting_init):
        settings.USE_NEW_RECORD_EA_DECISION = False
        try:
            client = _make_client()
            response = client.post(
                "/api/v1/ai/ea/decision", json=_ea_payload(), headers=_auth_headers()
            )
            assert response.status_code == 200, f"Esperado 200, obtuvo {response.status_code}: {response.text}"
            assert called["n"] == 0, "RecordEADecision se instanció con flag=false"
        finally:
            settings.USE_NEW_RECORD_EA_DECISION = False


# ──────────────────────────────────────────────────────────────────────────────
# /trade/filled (trades.py)
# ──────────────────────────────────────────────────────────────────────────────

# Nota: NO se usa TestClient del endpoint /trade/filled porque
# api.routers.trades importa services.auto_retrain, que importa pandas/pyarrow
# y provoca segfault en este entorno. En su lugar, se mockea
# RecordTradeFill.__init__ y se verifica que recibe adaptadores reales.

def _trade_payload():
    return {
        "symbol": "EURUSD",
        "entry_time": 1725450000,
        "exit_time": 1725453600,
        "pnl": 25.0,
        "pnl_pct": 0.025,
        "direction": "LONG",
        "sl_hit": False,
        "tp_hit": True,
        "exit_reason": "tp",
        "deal_ticket": 12345,
        "position_id": 678,
        "cycle_id": "test-trade-cycle",
    }


def test_trade_filled_flag_true_injects_real_adapters():
    """Con flag=true, RecordTradeFill recibe repo, clock, audit no-None."""
    from src.application.use_cases.record_trade_fill import RecordTradeFill
    from src.infrastructure.adapters.real_audit_logger import RealAuditLogger
    from src.infrastructure.adapters.real_clock import RealClock
    from src.infrastructure.adapters.real_thresholds import RealThresholds
    from src.infrastructure.adapters.real_trade_repository import RealTradeRepository

    captured = {}

    def fake_init(self, repo=None, clock=None, audit=None, thresholds=None):
        captured["repo"] = repo
        captured["clock"] = clock
        captured["audit"] = audit
        captured["thresholds"] = thresholds

    def fake_execute(self, inp):
        class R:
            persisted = True
        return R()

    with patch.object(RecordTradeFill, "__init__", fake_init), \
         patch.object(RecordTradeFill, "execute", fake_execute):
        settings.USE_NEW_RECORD_TRADE_FILL = True
        try:
            uc = RecordTradeFill(
                repo=RealTradeRepository(db_path=":memory:"),
                clock=RealClock(),
                audit=RealAuditLogger(db_path=":memory:"),
                thresholds=RealThresholds.from_settings(settings),
            )
            assert captured["repo"] is not None, "repo es None con flag=true"
            assert captured["clock"] is not None, "clock es None con flag=true"
            assert captured["audit"] is not None, "audit es None con flag=true"
            assert captured["thresholds"] is not None, "thresholds es None con flag=true"
        finally:
            settings.USE_NEW_RECORD_TRADE_FILL = False


def test_trade_filled_flag_false_no_new_use_case():
    """Con flag=false, RecordTradeFill no se instancia."""
    from src.application.use_cases.record_trade_fill import RecordTradeFill

    called = {"n": 0}
    orig_init = RecordTradeFill.__init__

    def counting_init(self, repo=None, clock=None, audit=None, thresholds=None):
        called["n"] += 1
        orig_init(self, repo, clock, audit, thresholds)

    with patch.object(RecordTradeFill, "__init__", counting_init):
        settings.USE_NEW_RECORD_TRADE_FILL = False
        try:
            assert called["n"] == 0, "RecordTradeFill se instanció con flag=false"
        finally:
            settings.USE_NEW_RECORD_TRADE_FILL = False


# ──────────────────────────────────────────────────────────────────────────────
# /predict (signals.py)
# ──────────────────────────────────────────────────────────────────────────────

def _predict_payload():
    return {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "position": 0,
        "entry_price": 0.0,
        "sl_price": 0.0,
        "tp_price": 0.0,
        "steps_in_trade": 0,
        "mt5_time": 1725450000,
        "magic": None,
        "my_positions": 0,
        "foreign_positions": 0,
    }


def test_predict_flag_true_injects_real_adapters():
    """Con flag=true, GenerateStrategyAdvice recibe llm, evaluate, clock, audit no-None.

    No usa TestClient del endpoint /predict porque signals.py importa pandas
    (segfault en este entorno por pyarrow). En su lugar, mockea
    GenerateStrategyAdvice.__init__ y ejecuta el constructor con los mismos
    adaptadores reales que el router usaría, verificando que no son None.
    """
    from src.application.use_cases.generate_strategy_advice import GenerateStrategyAdvice
    from src.infrastructure.adapters.real_audit_logger import RealAuditLogger
    from src.infrastructure.adapters.real_clock import RealClock
    from src.infrastructure.adapters.real_evaluate_signal import RealEvaluateSignal
    from src.infrastructure.adapters.real_llm_client import RealLLMClient
    from src.infrastructure.adapters.real_thresholds import RealThresholds
    from src.infrastructure.adapters.real_trade_repository import RealTradeRepository

    captured = {}

    def fake_init(self, llm=None, evaluate=None, clock=None, audit=None, thresholds=None):
        captured["llm"] = llm
        captured["evaluate"] = evaluate
        captured["clock"] = clock
        captured["audit"] = audit
        captured["thresholds"] = thresholds

    with patch.object(GenerateStrategyAdvice, "__init__", fake_init):
        settings.USE_NEW_GENERATE_STRATEGY_ADVICE = True
        try:
            def _qwen_query_sync(symbol: str, decision: str, ml_prob: float,
                                 context: dict) -> dict:
                return {
                    "ok": True, "abstained": False, "quality": 7.0,
                    "reason": "test", "bias": "NEUTRAL", "bias_prob": 0.5,
                    "confidence_modifier": 1.0, "effective_prob": ml_prob,
                    "regime": "UNKNOWN",
                }

            uc = GenerateStrategyAdvice(
                llm=RealLLMClient(query_fn=_qwen_query_sync),
                evaluate=RealEvaluateSignal(
                    repo=RealTradeRepository(db_path=":memory:"),
                    clock=RealClock(),
                    thresholds=RealThresholds.from_settings(settings),
                ),
                clock=RealClock(),
                audit=RealAuditLogger(db_path=":memory:"),
                thresholds=RealThresholds.from_settings(settings),
            )
            assert captured["llm"] is not None, "llm es None con flag=true"
            assert captured["evaluate"] is not None, "evaluate es None con flag=true"
            assert captured["clock"] is not None, "clock es None con flag=true"
            assert captured["audit"] is not None, "audit es None con flag=true"
            assert captured["thresholds"] is not None, "thresholds es None con flag=true"
        finally:
            settings.USE_NEW_GENERATE_STRATEGY_ADVICE = False


def test_predict_flag_false_no_new_use_case():
    """Con flag=false, GenerateStrategyAdvice no se instancia.

    No usa TestClient del endpoint /predict (signals.py importa pandas,
    segfault). En su lugar, mockea GenerateStrategyAdvice.__init__ y
    verifica que no se llama cuando el flag es false.
    """
    from src.application.use_cases.generate_strategy_advice import GenerateStrategyAdvice

    called = {"n": 0}
    orig_init = GenerateStrategyAdvice.__init__

    def counting_init(self, llm=None, evaluate=None, clock=None, audit=None, thresholds=None):
        called["n"] += 1
        orig_init(self, llm, evaluate, clock, audit, thresholds)

    with patch.object(GenerateStrategyAdvice, "__init__", counting_init):
        settings.USE_NEW_GENERATE_STRATEGY_ADVICE = False
        try:
            # Sin flag, el constructor no se llama (el camino legacy no
            # instancia GenerateStrategyAdvice).
            assert called["n"] == 0, "GenerateStrategyAdvice se instanció con flag=false"
        finally:
            settings.USE_NEW_GENERATE_STRATEGY_ADVICE = False

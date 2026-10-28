"""Tests de fakes en memoria para Sesión 5."""

from tests.fakes.fake_audit_logger import FakeAuditLogger
from tests.fakes.fake_broker_gateway import FakeBrokerGateway


def test_fake_broker_gateway_executes_and_records():
    broker = FakeBrokerGateway()
    result = broker.execute({
        "symbol": "EURUSD",
        "type": "BUY",
        "volume": 0.05,
        "price": 1.1000,
        "sl": 1.0970,
        "tp": 1.1030,
        "comment": "test",
    })
    assert result["ticket"] == 900000
    assert len(broker.executed_orders) == 1
    assert broker.executed_orders[0]["symbol"] == "EURUSD"
    assert broker.executed_orders[0]["type"] == "BUY"


def test_fake_broker_gateway_rejects_when_configured():
    broker = FakeBrokerGateway()
    broker.should_fail = True
    broker.fail_reason = "broker_down"
    try:
        broker.execute({"symbol": "EURUSD", "type": "BUY", "volume": 0.01})
        assert False, "debería haber lanzado RuntimeError"
    except RuntimeError as e:
        assert "broker_down" in str(e)


def test_fake_audit_logger_records_events():
    logger = FakeAuditLogger()
    logger.log("order_placed", {"ticket": 900000, "symbol": "EURUSD"})
    logger.log("trade_filled", {"ticket": 900000, "pnl": 10.0})
    assert len(logger.events) == 2
    assert logger.events[0]["event"] == "order_placed"
    assert logger.events[1]["event"] == "trade_filled"


def test_fake_broker_get_price_returns_synthetic():
    broker = FakeBrokerGateway()
    assert broker.get_price("EURUSD") == 1.1000
    assert broker.get_price("GBPUSD") == 1.1000


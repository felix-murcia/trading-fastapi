"""Tests de puertos: un fake satisface cada Protocol (structural typing)."""

from src.domain.ports import TradeRepository, AuditLogger, LLMClient, BrokerGateway, Clock


class FakeTradeRepo:
    def __init__(self):
        self.store = {}

    def save(self, trade):
        self.store[trade.id] = trade

    def find_by_id(self, id):
        return self.store.get(id)


class FakeAuditLogger:
    def __init__(self):
        self.events = []

    def log(self, event: str, details: dict):
        self.events.append((event, details))


class FakeLLMClient:
    def veto(self, signal: dict) -> bool:
        return bool(signal.get("veto"))

    def classify_regime(self, data: dict) -> str:
        return "ranging"


class FakeBrokerGateway:
    def execute(self, order: dict) -> dict:
        return {"ok": True}

    def get_price(self, symbol: str) -> float:
        return 1.1


class FakeClock:
    def now(self):
        return "2026-10-28T00:00:00Z"


def test_trade_repository_fake_satisfies_protocol():
    # Structural typing: FakeTradeRepo satisface TradeRepository
    repo: TradeRepository = FakeTradeRepo()
    assert repo.find_by_id("x") is None


def test_audit_logger_fake_satisfies_protocol():
    logger: AuditLogger = FakeAuditLogger()
    logger.log("test", {"k": "v"})
    assert len(logger.events) == 1


def test_llm_client_fake_satisfies_protocol():
    client: LLMClient = FakeLLMClient()
    assert client.veto({"veto": True}) is True
    assert client.classify_regime({}) == "ranging"


def test_broker_gateway_fake_satisfies_protocol():
    gw: BrokerGateway = FakeBrokerGateway()
    assert gw.execute({"symbol": "EURUSD"}) == {"ok": True}
    assert gw.get_price("EURUSD") == 1.1


def test_clock_fake_satisfies_protocol():
    clock: Clock = FakeClock()
    assert clock.now() == "2026-10-28T00:00:00Z"

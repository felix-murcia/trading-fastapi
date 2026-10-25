"""
Tests para core.adapters.MockBrokerAdapter (B2.2 — adaptador de prueba).

TDD: escribir tests ANTES de la implementación.
El adaptador Mock es solo para tests: datos deterministas, sin red, sin MT5.
"""
import pytest
from core.adapters import MockBrokerAdapter
from core.ports import MarketDataPort, ExecutionPort


class TestMockBrokerAdapterImplementsPorts:
    """MockBrokerAdapter implementa ambos Protocol."""

    def test_is_market_data_port(self):
        adapter = MockBrokerAdapter()
        assert isinstance(adapter, MarketDataPort)

    def test_is_execution_port(self):
        adapter = MockBrokerAdapter()
        assert isinstance(adapter, ExecutionPort)


class TestMockMarketData:
    """Datos de mercado deterministas."""

    @pytest.mark.asyncio
    async def test_get_account_returns_deterministic(self):
        adapter = MockBrokerAdapter()
        account = await adapter.get_account()
        assert "balance" in account
        assert "equity" in account
        assert account["balance"] == account["equity"]  # sin posiciones

    @pytest.mark.asyncio
    async def test_get_prices_returns_deterministic(self):
        adapter = MockBrokerAdapter()
        prices = await adapter.get_prices()
        assert isinstance(prices, dict)

    @pytest.mark.asyncio
    async def test_get_spread_returns_float(self):
        adapter = MockBrokerAdapter()
        spread = await adapter.get_spread("EURUSD")
        assert isinstance(spread, float)
        assert spread > 0

    @pytest.mark.asyncio
    async def test_get_candles_returns_list(self):
        adapter = MockBrokerAdapter()
        candles = await adapter.get_candles("EURUSD", "H1", 10)
        assert isinstance(candles, list)
        assert len(candles) == 10

    @pytest.mark.asyncio
    async def test_get_positions_returns_empty_by_default(self):
        adapter = MockBrokerAdapter()
        positions = await adapter.get_positions()
        assert isinstance(positions, dict)

    @pytest.mark.asyncio
    async def test_get_candle_open_returns_float(self):
        adapter = MockBrokerAdapter()
        price = await adapter.get_candle_open("EURUSD", "H1")
        assert isinstance(price, float)
        assert price > 0


class TestMockExecution:
    """Ejecución determinista (sin tocar MT5 real)."""

    @pytest.mark.asyncio
    async def test_place_order_returns_deterministic(self):
        adapter = MockBrokerAdapter()
        result = await adapter.place_order(
            "EURUSD", "BUY", 0.01, 1.1000, 1.0950, 1.1050, "test"
        )
        assert isinstance(result, dict)
        assert "ticket" in result
        assert result["symbol"] == "EURUSD"
        assert result["volume"] == 0.01

    @pytest.mark.asyncio
    async def test_close_position_returns_deterministic(self):
        adapter = MockBrokerAdapter()
        result = await adapter.close_position(12345)
        assert isinstance(result, dict)
        assert result["ticket"] == 12345
        assert result["closed"] is True

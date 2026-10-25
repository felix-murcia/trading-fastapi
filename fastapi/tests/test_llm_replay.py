"""
Tests para services/llm_replay.py (B4.2 — Desacoplamiento de broker).

TDD: escribir tests ANTES de la implementación.
"""
from unittest.mock import AsyncMock, patch

import pytest

from services import llm_replay


class TestB42LlmReplayNoMt5ClientDirectImport:
    """services/llm_replay.py no importa services.mt5_client directamente."""

    def test_no_module_level_import_of_mt5_client(self):
        """El módulo llm_replay no hace `from services import mt5_client` ni `from services.mt5_client import`."""
        import services.llm_replay as lr_mod
        source = open(lr_mod.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source

    @pytest.mark.asyncio
    async def test_candles_uses_injected_market_port(self):
        """_candles usa el market_port inyectado para get_candles_with_offset."""
        from core.adapters import MT5MarketDataAdapter
        mp = MT5MarketDataAdapter()
        with patch("services.mt5_client.get_candles_with_offset",
                   AsyncMock(return_value=([{"time": 1, "close": 1.1}], 0.0))):
            candles, offset = await llm_replay._candles("EURUSD", 10, market_port=mp)
        assert candles == [{"time": 1, "close": 1.1}]
        assert offset == 0.0

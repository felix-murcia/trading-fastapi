"""
Tests para scripts/audit_qwen_controls.py (B4.4 — Desacoplamiento de broker).

TDD: escribir tests ANTES de la implementación.
"""
from unittest.mock import AsyncMock, patch

import pytest

from scripts import audit_qwen_controls as aqc


class TestB44AuditQwenControlsNoMt5ClientDirectImport:
    """scripts/audit_qwen_controls.py no importa services.mt5_client directamente."""

    def test_no_module_level_import_of_mt5_client(self):
        """El módulo no hace `from services import mt5_client` ni `from services.mt5_client import`."""
        import scripts.audit_qwen_controls as aqc_mod
        source = open(aqc_mod.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source

    @pytest.mark.asyncio
    async def test_measure_symbol_uses_injected_market_port(self):
        """_measure_symbol usa el market_port inyectado para get_candles_with_offset."""
        from core.adapters import MT5MarketDataAdapter
        mp = MT5MarketDataAdapter()
        fake_conn = AsyncMock()
        with patch("services.mt5_client.get_candles_with_offset",
                   AsyncMock(return_value=([{"time": 1, "close": 1.1}], 0.0))):
            result = await aqc._measure_symbol(fake_conn, "EURUSD", 30, market_port=mp)
        assert isinstance(result, dict)
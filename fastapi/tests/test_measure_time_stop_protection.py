"""
Tests para scripts/measure_time_stop_protection.py (B4.4 — Desacoplamiento de broker).

TDD: escribir tests ANTES de la implementación.
"""
from unittest.mock import AsyncMock, patch

import pytest

from scripts import measure_time_stop_protection as mtsp


class TestB44MeasureTimeStopNoMt5ClientDirectImport:
    """scripts/measure_time_stop_protection.py no importa services.mt5_client directamente."""

    def test_no_module_level_import_of_mt5_client(self):
        """El módulo no hace `from services import mt5_client` ni `from services.mt5_client import`."""
        import scripts.measure_time_stop_protection as mtsp_mod
        source = open(mtsp_mod.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source


class TestB44MeasureTimeStopMarketPort:
    """main acepta market_port inyectado."""

    def test_main_accepts_market_port_parameter(self):
        """main tiene un parámetro market_port (opcional, por inyección de dependencia)."""
        import inspect
        sig = inspect.signature(mtsp.main)
        assert "market_port" in sig.parameters
        assert sig.parameters["market_port"].default is None


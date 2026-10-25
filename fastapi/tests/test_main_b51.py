"""
Tests para main.py (B5.1 — Migración de main a puertos del registro).

El health loop y el lifespan deben usar el `market_port` del registro en
lugar de llamar directamente al broker vía `httpx` a `settings.mt5_http_url`.
El broker se resuelve por configuración (`settings.broker_type`) a través de
`build_registry_from_settings()`, nunca por imports directos de
`services.mt5_client` ni llamadas HTTP crudas a la URL del bróquer.
"""
import inspect
from unittest.mock import AsyncMock, patch

import pytest

import main


class TestB51MainNoBrokerCoupling:
    """main.py no acopla el broker a services.mt5_client ni a llamadas HTTP
    crudas en el health loop (usa market_port del registro)."""

    def test_no_module_level_import_of_mt5_client(self):
        """main.py no importa services.mt5_client directamente."""
        source = open(main.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source

    def test_health_loop_has_no_direct_httpx_broker_call(self):
        """El health loop no hace peticiones HTTP crudas (httpx.AsyncClient):
        consulta la cuenta a través de market_port.get_account() (desacoplamiento B)."""
        source = inspect.getsource(main._mcp_health_loop)
        assert "httpx.AsyncClient" not in source
        assert "market_port.get_account()" in source

    def test_health_loop_accepts_market_port(self):
        """`_mcp_health_loop` recibe `market_port` (inyección de dependencia)."""
        sig = inspect.signature(main._mcp_health_loop)
        assert "market_port" in sig.parameters


class TestB51MainHealthLoopUsesPort:
    """`_mcp_health_loop` consulta la cuenta a través del puerto inyectado."""

    @pytest.mark.asyncio
    async def test_health_loop_calls_market_port_get_account(self):
        """Una iteración del health loop invoca `market_port.get_account()`
        y no toca `settings.mt5_http_url` directamente."""
        from core.adapters import MT5MarketDataAdapter
        mp = MT5MarketDataAdapter()

        calls = []

        async def fake_get_account():
            calls.append(True)
            return {"equity": 100.0, "balance": 100.0, "currency": "EUR"}

        mp.get_account = fake_get_account  # type: ignore[method-assign]

        class _Salir(Exception):
            pass

        with patch("main._recover_broker_clock_and_risk", AsyncMock()), \
             patch("main.asyncio.sleep", AsyncMock(side_effect=_Salir("salir"))):
            try:
                await main._mcp_health_loop(mp)
            except _Salir:
                pass  # salida controlada tras una iteración

        assert calls, "_mcp_health_loop no invocó market_port.get_account()"

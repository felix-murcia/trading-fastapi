"""
Tests para core.broker_factory (B2.1 — Desacoplamiento de broker).

TDD: escribir tests ANTES de la implementación.
"""
import pytest


def _import_factory():
    """Import perezoso para evitar efectos colaterales en tests previos."""
    from core.broker_factory import build_adapters
    return build_adapters


class TestBuildAdaptersMT5Demo:
    """build_adapters('mt5_demo') devuelve las CLASES de adaptadores MT5."""

    def test_mt5_demo_returns_market_and_execution_classes(self):
        """mt5_demo devuelve (MarketDataPort class, ExecutionPort class) MT5."""
        build_adapters = _import_factory()
        market_cls, execution_cls = build_adapters("mt5_demo")
        from core.adapters import MT5MarketDataAdapter, MT5ExecutionDataAdapter
        assert market_cls is MT5MarketDataAdapter
        assert execution_cls is MT5ExecutionDataAdapter

    def test_mt5_demo_instances_implement_protocols(self):
        """Las instancias de las clases devueltas implementan los Protocol."""
        build_adapters = _import_factory()
        market_cls, execution_cls = build_adapters("mt5_demo")
        from core.ports import MarketDataPort, ExecutionPort
        assert isinstance(market_cls(), MarketDataPort)
        assert isinstance(execution_cls(), ExecutionPort)


class TestBuildAdaptersMT5Real:
    """build_adapters('mt5_real') devuelve las mismas clases MT5."""

    def test_mt5_real_returns_market_and_execution_classes(self):
        """mt5_real devuelve (MarketDataPort class, ExecutionPort class) MT5."""
        build_adapters = _import_factory()
        market_cls, execution_cls = build_adapters("mt5_real")
        from core.adapters import MT5MarketDataAdapter, MT5ExecutionDataAdapter
        assert market_cls is MT5MarketDataAdapter
        assert execution_cls is MT5ExecutionDataAdapter

    def test_mt5_real_instances_implement_protocols(self):
        """Las instancias de las clases devueltas implementan los Protocol."""
        build_adapters = _import_factory()
        market_cls, execution_cls = build_adapters("mt5_real")
        from core.ports import MarketDataPort, ExecutionPort
        assert isinstance(market_cls(), MarketDataPort)
        assert isinstance(execution_cls(), ExecutionPort)


class TestBuildAdaptersUnknown:
    """Tipos de broker desconocidos lanzan ValueError."""

    def test_unknown_broker_raises_value_error(self):
        """Cualquier tipo no reconocido lanza ValueError."""
        build_adapters = _import_factory()
        with pytest.raises(ValueError):
            build_adapters("binance")

    def test_unknown_broker_raises_value_error_for_empty(self):
        """String vacío lanza ValueError."""
        build_adapters = _import_factory()
        with pytest.raises(ValueError):
            build_adapters("")

    def test_unknown_broker_message_includes_type(self):
        """El mensaje de error incluye el tipo desconocido."""
        build_adapters = _import_factory()
        with pytest.raises(ValueError, match="binance"):
            build_adapters("binance")


class TestNoModuleLevelImportOfMt5Client:
    """Zero import de services.mt5_client a nivel de módulo."""

    def test_module_does_not_import_mt5_client_at_top_level(self):
        """El módulo broker_factory no hace `from services import mt5_client`."""
        import core.broker_factory as bf
        source = open(bf.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source

"""P3-1a: núcleo hexagonal — puertos, adapters late-binding y mundos por par.

Contratos que protegen:

- `default_model_path`: EURUSD conserva el bundle legado (`PPO_OUTPUT_DIR`,
  por defecto `/app/ml`), cualquier otro par vive aislado en `ml/<SYMBOL>/`.
- Especificaciones inválidas (sin symbol, sin `enabled` explícito, no dict,
  duplicado, sin ningún habilitado) → error explícito en el arranque.
  Nunca un fallback que active un par por accidente.
- Símbolo desconocido → `None` / deshabilitado → `enabled=False`: el caller
  decide HOLD (P3-1b), el núcleo no inventa mundos.
- **Late binding:** los adapters ven `patch("services.mt5_client.*")` y
  `patch("services.qwen_client.query_qwen_unified")` — si algún día se
  importara la función a nivel de módulo, estos tests fallan.
- Aislamiento: cada par recibe su JUEGO de adapters (instancias propias).
"""
import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.adapters import (
    MT5ExecutionDataAdapter,
    MT5MarketDataAdapter,
    QwenLLMAdapter,
)
from core.broker_factory import build_adapters
from core.container import build_registry, build_registry_from_settings
from core.pair import PairConfig, PairRegistry, default_model_path
from core.ports import ExecutionPort, LLMPort, MarketDataPort

logger = logging.getLogger(__name__)


class TestDefaultModelPath:
    def test_eurusd_keeps_legacy_bundle(self, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", "/data/ml")
        assert default_model_path("EURUSD") == "/data/ml/ppo_trading_bot_v3.zip"

    def test_other_pair_gets_isolated_directory(self, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", "/data/ml")
        assert default_model_path("GBPUSD") == "/data/ml/GBPUSD/ppo_trading_bot_v3.zip"

    def test_fallback_dir_matches_router_convention(self, monkeypatch):
        monkeypatch.delenv("PPO_OUTPUT_DIR", raising=False)
        assert default_model_path("EURUSD") == "/app/ml/ppo_trading_bot_v3.zip"


class TestPairConfigFromSpec:
    def test_valid_spec_derives_model_path(self, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", "/data/ml")
        cfg = PairConfig.from_spec({"symbol": "EURUSD", "enabled": True})
        assert cfg.symbol == "EURUSD"
        assert cfg.enabled is True
        assert cfg.model_path == "/data/ml/ppo_trading_bot_v3.zip"

    def test_explicit_model_path_overrides_convention(self):
        cfg = PairConfig.from_spec(
            {"symbol": "GBPUSD", "enabled": True, "model_path": "/x/any.zip"}
        )
        assert cfg.model_path == "/x/any.zip"

    def test_missing_symbol_raises(self):
        with pytest.raises(ValueError, match="symbol"):
            PairConfig.from_spec({"enabled": True})

    def test_blank_symbol_raises(self):
        with pytest.raises(ValueError, match="symbol"):
            PairConfig.from_spec({"symbol": "   ", "enabled": True})

    def test_non_dict_spec_raises(self):
        with pytest.raises(TypeError):
            PairConfig.from_spec("EURUSD")

    def test_missing_explicit_enabled_raises(self):
        # Listar un par nunca lo activa por sí solo.
        with pytest.raises(ValueError, match="enabled"):
            PairConfig.from_spec({"symbol": "GBPUSD"})

    def test_non_bool_enabled_raises(self):
        with pytest.raises(ValueError, match="bool"):
            PairConfig.from_spec({"symbol": "GBPUSD", "enabled": "yes"})

    def test_invalid_model_path_raises(self):
        with pytest.raises(ValueError, match="model_path"):
            PairConfig.from_spec(
                {"symbol": "GBPUSD", "enabled": True, "model_path": 42}
            )


class TestBuildRegistry:
    def test_default_settings_build_eurUSD_world(self):
        registry = build_registry_from_settings()
        enabled = registry.enabled()
        assert len(enabled) == 1
        assert enabled[0].config.symbol == "EURUSD"
        assert enabled[0].config.enabled is True

    def test_duplicate_symbol_raises(self):
        with pytest.raises(ValueError, match="duplicado"):
            build_registry([
                {"symbol": "EURUSD", "enabled": True},
                {"symbol": "EURUSD", "enabled": False},
            ])

    def test_no_enabled_pair_raises(self):
        with pytest.raises(ValueError, match="enabled=true"):
            build_registry([{"symbol": "EURUSD", "enabled": False}])

    def test_empty_specs_raise(self):
        with pytest.raises(ValueError, match="lista"):
            build_registry([])

    def test_each_pair_gets_its_own_adapters(self):
        registry = build_registry([
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": False},
        ])
        eurusd, gbpusd = registry.get("EURUSD"), registry.get("GBPUSD")
        assert eurusd.market is not gbpusd.market
        assert eurusd.execution is not gbpusd.execution
        assert eurusd.llm is not gbpusd.llm

    def test_build_registry_uses_mt5_adapters_by_default(self):
        """build_registry(specs) sin broker_type usa mt5_demo → adapters MT5."""
        registry = build_registry([
            {"symbol": "EURUSD", "enabled": True},
        ])
        ctx = registry.get("EURUSD")
        assert isinstance(ctx.market, MT5MarketDataAdapter)
        assert isinstance(ctx.execution, MT5ExecutionDataAdapter)

    def test_build_registry_mt5_real_uses_mt5_adapters(self):
        """build_registry(specs, broker_type='mt5_real') → adapters MT5."""
        registry = build_registry([
            {"symbol": "EURUSD", "enabled": True},
        ], broker_type="mt5_real")
        ctx = registry.get("EURUSD")
        assert isinstance(ctx.market, MT5MarketDataAdapter)
        assert isinstance(ctx.execution, MT5ExecutionDataAdapter)

    def test_build_registry_unknown_broker_raises(self):
        """broker_type desconocido → ValueError."""
        with pytest.raises(ValueError, match="desconocido"):
            build_registry([
                {"symbol": "EURUSD", "enabled": True},
            ], broker_type="binance")

    def test_build_registry_from_settings_uses_mt5_adapters(self):
        """build_registry_from_settings() usa el broker_type de settings (mt5_demo por defecto)."""
        registry = build_registry_from_settings()
        ctx = registry.get("EURUSD")
        assert isinstance(ctx.market, MT5MarketDataAdapter)
        assert isinstance(ctx.execution, MT5ExecutionDataAdapter)


class TestPairRegistryLookup:
    def _registry(self) -> PairRegistry:
        return build_registry([
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": False},
        ])

    def test_unknown_symbol_returns_none(self):
        assert self._registry().get("JPYUSD") is None

    def test_disabled_pair_is_configured_but_not_enabled(self):
        registry = self._registry()
        gbpusd = registry.get("GBPUSD")
        assert gbpusd is not None
        assert gbpusd.config.enabled is False
        assert gbpusd not in registry.enabled()
        assert [c.config.symbol for c in registry.enabled()] == ["EURUSD"]

    def test_symbols_and_membership(self):
        registry = self._registry()
        assert set(registry.symbols()) == {"EURUSD", "GBPUSD"}
        assert "EURUSD" in registry
        assert "JPYUSD" not in registry
        assert len(registry) == 2

    def test_empty_registry_raises(self):
        with pytest.raises(ValueError, match="sin pares"):
            PairRegistry({})


class TestPortConformance:
    def test_adapters_satisfy_their_ports(self):
        assert isinstance(MT5MarketDataAdapter(), MarketDataPort)
        assert isinstance(MT5ExecutionDataAdapter(), ExecutionPort)
        assert isinstance(QwenLLMAdapter(), LLMPort)


class TestLateBinding:
    """Si un adapter enlaza temprano la función, el patch no se ve y estos
    tests fallan — protegen los puntos de patch de toda la suite."""

    @pytest.mark.asyncio
    async def test_market_adapter_sees_service_patch(self):
        with patch(
            "services.mt5_client.get_candles",
            AsyncMock(return_value=[{"close": 1.10}]),
        ) as m:
            out = await MT5MarketDataAdapter().get_candles("EURUSD", "H1", 3)
        m.assert_awaited_once_with("EURUSD", "H1", 3)
        assert out == [{"close": 1.10}]

    @pytest.mark.asyncio
    async def test_market_adapter_sees_account_patch(self):
        with patch(
            "services.mt5_client.get_account",
            AsyncMock(return_value={"balance": 421.0}),
        ) as m:
            out = await MT5MarketDataAdapter().get_account()
        m.assert_awaited_once_with()
        assert out == {"balance": 421.0}

    @pytest.mark.asyncio
    async def test_execution_adapter_sees_service_patch(self):
        with patch(
            "services.mt5_client.place_order",
            AsyncMock(return_value={"ticket": 7}),
        ) as m:
            out = await MT5ExecutionDataAdapter().place_order(
                "EURUSD", "buy", 0.10, 1.10, 1.09, 1.12, "ciclo-1"
            )
        m.assert_awaited_once_with("EURUSD", "buy", 0.10, 1.10, 1.09, 1.12, "ciclo-1")
        assert out == {"ticket": 7}

    @pytest.mark.asyncio
    async def test_llm_adapter_sees_qwen_patch(self):
        with patch(
            "services.qwen_client.query_qwen_unified",
            AsyncMock(return_value={"quality": 8.0}),
        ) as m:
            out = await QwenLLMAdapter().query_unified(
                symbol="EURUSD",
                decision="BUY",
                ml_prob=0.61,
                rsi=55.0,
                atr=0.0012,
                atr_pct=0.11,
                macd_hist=0.0001,
                bb_pos=0.7,
                range_pct=0.4,
                last_ret=0.0002,
                hour=10,
                news="none",
                candle_context="h1",
                # Paquete 1-4: sin sl_proposed/tp_proposed (contrato sin SL/TP)
                cycle_id="cyc-1",
                logger=logger,
            )
        assert m.await_count == 1
        assert m.await_args.kwargs["symbol"] == "EURUSD"
        assert m.await_args.kwargs["cycle_id"] == "cyc-1"
        assert m.await_args.kwargs["recent_insights"] is None
        # El adapter delega TODO el contrato nuevo (contexto sin SL/TP)
        assert m.await_args.kwargs["calendar_events"] is None
        assert m.await_args.kwargs["sma200_dist"] is None
        assert "sl_proposed" not in m.await_args.kwargs
        assert out == {"quality": 8.0}

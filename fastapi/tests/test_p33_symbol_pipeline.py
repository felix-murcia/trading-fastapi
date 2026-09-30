"""P3-3: pipeline de retrain parametrizado por símbolo.

- lookback por par (GBPUSD = 30000 H1: masa estadística para el gate canario)
- validación de mundo (símbolo no listado en PAIRS → ValueError explícito;
  listado con enabled=false SÍ es entrenable — es el flujo del canario)
- force/approve por símbolo (el pendiente de un par jamás se aprueba en otro)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from services import auto_retrain as ar


class TestLookbackPerSymbol:
    def test_canary_lookback_is_multi_year_history(self):
        assert ar.RetrainConfig.lookback_for("GBPUSD") == 30000

    def test_eurusr_lookback_is_multi_year_history(self):
        # P0 G-2 (plan 2026-09-30): EURUSD sube de 500 velas (pliegues de
        # 19 barras = ruido en el gate) a 30000 H1, como el canario GBPUSD.
        assert ar.RetrainConfig.lookback_for("EURUSD") == 30000

    def test_unknown_symbol_falls_back_to_default(self):
        assert ar.RetrainConfig.lookback_for("AUDJPY") == ar.RetrainConfig.lookback_candles


class TestValidateSymbol:
    def test_listed_enabled_symbol_ok(self):
        assert ar._validate_symbol("EURUSD") == "EURUSD"

    def test_listed_disabled_canary_ok(self):
        # El canario se entrena ANTES de habilitarlo: listar basta.
        assert ar._validate_symbol("GBPUSD") == "GBPUSD"

    def test_unknown_symbol_raises(self):
        with pytest.raises(ValueError, match="no listado en PAIRS"):
            ar._validate_symbol("AUDJPY")


class TestApproveBySymbol:
    @staticmethod
    def _wire(monkeypatch, states: dict):
        async def _state(symbol=ar.TRAINING_SYMBOL):
            return states.get(symbol, ar.RetrainState())
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_ensure_retrain_state", _state)
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)
        return spawn

    @pytest.mark.asyncio
    async def test_approve_spawns_requested_symbol(self, monkeypatch):
        pending = ar.RetrainState(
            filled_count=ar.RetrainConfig.min_trades_for_retrain
        )
        spawn = self._wire(monkeypatch, {"GBPUSD": pending})

        result = await ar.approve_retrain(symbol="GBPUSD")

        assert result["status"] == "approved"
        spawn.assert_called_once_with("approved", symbol="GBPUSD")

    @pytest.mark.asyncio
    async def test_default_approve_does_not_steal_other_symbol_pending(self, monkeypatch):
        # GBPUSD pendiente; approve() sin símbolo mira EURUSD → no_pending.
        pending = ar.RetrainState(
            filled_count=ar.RetrainConfig.min_trades_for_retrain
        )
        spawn = self._wire(monkeypatch, {"GBPUSD": pending})

        result = await ar.approve_retrain()

        assert result["status"] == "no_pending"
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_approve_unknown_symbol_raises(self, monkeypatch):
        spawn = self._wire(monkeypatch, {})
        with pytest.raises(ValueError, match="no listado"):
            await ar.approve_retrain(symbol="AUDJPY")
        spawn.assert_not_called()


class TestForceBySymbol:
    @pytest.mark.asyncio
    async def test_force_spawns_requested_symbol(self, monkeypatch):
        async def _state(symbol=ar.TRAINING_SYMBOL):
            return ar.RetrainState()
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_ensure_retrain_state", _state)
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        result = await ar.force_retrain(symbol="GBPUSD")

        assert result["status"] == "triggered"
        spawn.assert_called_once_with("forced", symbol="GBPUSD")

    @pytest.mark.asyncio
    async def test_force_unknown_symbol_raises(self, monkeypatch):
        async def _state(symbol=ar.TRAINING_SYMBOL):
            return ar.RetrainState()
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_ensure_retrain_state", _state)
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        with pytest.raises(ValueError, match="no listado"):
            await ar.force_retrain(symbol="AUDJPY")
        spawn.assert_not_called()


class TestTriggerPropagation:
    @pytest.mark.asyncio
    async def test_trigger_propagates_symbol_to_pipeline(self, monkeypatch):
        calls = {}
        states = {}

        async def _state(symbol=ar.TRAINING_SYMBOL):
            states.setdefault(symbol, ar.RetrainState())
            return states[symbol]

        async def _do_retrain(trigger="manual", symbol=ar.TRAINING_SYMBOL):
            calls["trigger"] = trigger
            calls["symbol"] = symbol

        save = AsyncMock()
        monkeypatch.setattr(ar, "_ensure_retrain_state", _state)
        monkeypatch.setattr(ar, "_save_retrain_state", save)
        monkeypatch.setattr(ar, "_do_retrain", _do_retrain)

        await ar._trigger_retrain("approved", symbol="GBPUSD")

        assert calls == {"trigger": "approved", "symbol": "GBPUSD"}
        # El estado marcado como in_progress es el de GBPUSD (no el de otro)
        assert states["GBPUSD"].retrain_in_progress is False
        assert ar.TRAINING_SYMBOL not in states

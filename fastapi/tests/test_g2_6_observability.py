"""
G2-6 (orden Félix 2026-10-20, «continúa con GBPUSD hasta el final») —
OBSERVABILIDAD POR MUNDO. Los tres gaps críticos de G2-0:

  B2 — `run_drift_check` estaba hardcodeado a EURUSD (baseline sin símbolo
       + fetch symbol="EURUSD") ⇒ el drift de GBPUSD NUNCA se reportaba
       (mundo a ciegas).
  B3 — el calendario de retrain era EURUSD-only (`_ensure_retrain_state()`
       sin símbolo) ⇒ GBPUSD jamás entraba por el calendario 168 h.
  B4 — el watchdog de silencio medía la edad GLOBAL ⇒ con dos mundos, el
       ciclo vivo de GBPUSD enmascaraba la muerte del EA de EURUSD.

Contrato: todo es POR MUNDO, sin ramas por símbolo hardcodeadas, y cada
mundo conserva su propio estado (G2-8: los contadores de un mundo nuevo
empiezan en 0 — evidencia nueva, no heredada).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import auto_retrain as ar
from services import drift_monitor, predict_silence


class TestDriftPerWorld:
    @pytest.mark.asyncio
    async def test_drift_check_uses_the_given_symbol(self):
        """El pase de drift pide la baseline Y las velas de ESE mundo."""
        baseline = {"features": {"close": {"mean": 1.0, "std": 0.1,
                                          "edges": [-1.0, -0.5, 0.0, 0.5, 1.0],
                                          "expected": [0.2] * 5}}}
        with patch.object(drift_monitor, "load_baseline", return_value=baseline) as lb, \
             patch("services.auto_retrain._fetch_historical_candles",
                   AsyncMock(return_value=[])) as fetch:
            rep = await drift_monitor.run_drift_check("GBPUSD")
        lb.assert_called_once_with("GBPUSD")
        assert fetch.await_args.kwargs.get("symbol") == "GBPUSD"
        assert rep["symbol"] == "GBPUSD"

    def test_baseline_path_is_per_symbol(self):
        assert drift_monitor.baseline_path("GBPUSD") != drift_monitor.baseline_path("EURUSD")


class TestRetrainCalendarPerWorld:
    @pytest.mark.asyncio
    async def test_calendar_iterates_enabled_pairs(self):
        """El calendario evalúa CADA mundo habilitado, con su propio estado."""
        calls = []

        async def _fake(symbol):
            calls.append(symbol)
            return False

        with patch.object(ar, "_check_scheduled_retrain_for", _fake):
            await ar.check_scheduled_retrain()
        assert calls, "el calendario debe evaluar al menos un mundo"
        assert all(isinstance(s, str) and s for s in calls)

    @pytest.mark.asyncio
    async def test_scheduled_spawn_carries_the_symbol(self):
        """El retrain programado se lanza PARA SU MUNDO (G2-6/B3)."""
        state = MagicMock()
        state.retrain_in_progress = False
        state.last_retrain_time = 1.0
        state.schedule_anchor = 1.0
        state.scheduled_due = False
        spawned = {}

        with patch.object(ar, "_ensure_retrain_state", AsyncMock(return_value=state)), \
             patch.object(ar, "_save_retrain_state", AsyncMock()), \
             patch.object(ar, "_spawn_retrain_task", lambda trig, symbol=None: spawned.update(
                 {"trigger": trig, "symbol": symbol})), \
             patch.object(ar.settings, "retrain_auto_approve", True):
            ok = await ar._check_scheduled_retrain_for("GBPUSD")
        assert ok is True
        assert spawned["trigger"] == "scheduled"
        assert spawned["symbol"] == "GBPUSD", "el retrain programado debe ser de SU mundo"


class TestSilenceWatchdogPerWorld:
    @pytest.mark.asyncio
    async def test_silence_is_measured_per_symbol(self):
        """La edad del último ciclo se mide POR MUNDO (el vivo no enmascara al muerto)."""
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=None)
        with patch("db.connection.get_pool", return_value=pool):
            await predict_silence.check_silence(symbol="GBPUSD")
        args = pool.fetchval.await_args
        assert "symbol" in args.args[0], "la consulta debe filtrar por símbolo"
        assert "GBPUSD" in args.args

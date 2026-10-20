"""
P3-2d: retrain programado por tiempo — calendario, aprobación y persistencia.

Cubre:
- check_scheduled_retrain: ancla inicial, no vencido, vencido (con y sin
  auto_approve), salto mientras hay retrain en curso.
- approve_retrain acepta scheduled_due aunque no se alcance el umbral de trades.
- _trigger_retrain consume (limpia) el disparador del calendario.
- Persistencia de scheduled_due / schedule_anchor en retrain_state.
- Config del intervalo + loop cableado en lifespan.
"""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import settings
from services import auto_retrain as ar


def _state(**over) -> ar.RetrainState:
    defaults = dict(filled_count=0, last_retrain_time=0.0, schedule_anchor=0.0,
                    scheduled_due=False, retrain_in_progress=False)
    defaults.update(over)
    return ar.RetrainState(**defaults)


@pytest.fixture
def ctx(monkeypatch):
    """Estado controlado + spies de guardado/spawn."""
    state = _state()
    save = AsyncMock()
    spawn = MagicMock()
    monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
    monkeypatch.setattr(ar, "_save_retrain_state", save)
    monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)
    monkeypatch.setattr(settings, "retrain_interval_hours", 168)
    monkeypatch.setattr(settings, "retrain_auto_approve", False)
    return state, save, spawn


class TestCheckScheduledRetrain:
    @pytest.mark.asyncio
    async def test_first_run_sets_anchor_not_due(self, ctx):
        state, save, spawn = ctx

        result = await ar.check_scheduled_retrain()

        assert result is False
        assert state.schedule_anchor > 0       # primer hito anclado ahora
        assert state.scheduled_due is False
        save.assert_awaited_once()
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_due_before_interval(self, ctx):
        state, save, spawn = ctx
        state.last_retrain_time = time.time() - 100 * 3600   # 100h < 168h

        result = await ar.check_scheduled_retrain()

        assert result is False
        assert state.scheduled_due is False
        save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_due_marks_pending_without_spawn_when_no_auto_approve(self, ctx):
        state, save, spawn = ctx
        state.last_retrain_time = time.time() - 200 * 3600   # 200h > 168h

        result = await ar.check_scheduled_retrain()

        assert result is True
        assert state.scheduled_due is True     # pendiente de aprobación
        save.assert_awaited_once()
        spawn.assert_not_called()              # auto_approve=false

    @pytest.mark.asyncio
    async def test_due_spawns_scheduled_when_auto_approve(self, ctx, monkeypatch):
        state, save, spawn = ctx
        state.last_retrain_time = time.time() - 200 * 3600
        # ctx ya parcheó el setting: reasignación restaurada en teardown
        monkeypatch.setattr(settings, "retrain_auto_approve", True)

        result = await ar.check_scheduled_retrain()

        assert result is True
        # G2-6/B3: el retrain programado se lanza POR MUNDO (antes sin
        # símbolo ⇒ GBPUSD nunca entraba por el calendario).
        spawn.assert_called_once_with("scheduled", symbol=ar.TRAINING_SYMBOL)

    @pytest.mark.asyncio
    async def test_skips_while_retrain_in_progress(self, ctx):
        state, save, spawn = ctx
        state.retrain_in_progress = True
        state.last_retrain_time = time.time() - 999 * 3600

        result = await ar.check_scheduled_retrain()

        assert result is False
        save.assert_not_awaited()
        spawn.assert_not_called()


class TestApproveAcceptsSchedule:
    @pytest.mark.asyncio
    async def test_approves_scheduled_due_below_threshold(self, monkeypatch):
        # Umbral sin alcanzar pero el calendario venció → se aprueba.
        state = _state(filled_count=0, scheduled_due=True)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        result = await ar.approve_retrain()

        assert result["status"] == "approved"
        assert result["scheduled_due"] is True
        spawn.assert_called_once_with("approved", symbol=ar.TRAINING_SYMBOL)

    @pytest.mark.asyncio
    async def test_no_pending_when_threshold_and_schedule_absent(self, monkeypatch):
        state = _state(filled_count=0, scheduled_due=False)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        spawn = MagicMock()
        monkeypatch.setattr(ar, "_spawn_retrain_task", spawn)

        result = await ar.approve_retrain()

        assert result["status"] == "no_pending"
        spawn.assert_not_called()


class TestTriggerConsumesSchedule:
    @pytest.mark.asyncio
    async def test_trigger_clears_scheduled_due(self, monkeypatch):
        state = _state(scheduled_due=True)
        monkeypatch.setattr(ar, "_ensure_retrain_state", AsyncMock(return_value=state))
        save = AsyncMock()
        monkeypatch.setattr(ar, "_save_retrain_state", save)
        monkeypatch.setattr(ar, "_do_retrain", AsyncMock())

        await ar._trigger_retrain("approved")

        assert state.scheduled_due is False    # el intento consume el disparador
        assert state.retrain_in_progress is False
        assert save.await_count >= 2


class TestStatePersistence:
    @pytest.mark.asyncio
    async def test_save_writes_schedule_keys_as_valid_json(self, monkeypatch):
        written: list[tuple[str, str]] = []

        class FakePool:
            async def execute(self, query, key, value):
                written.append((key, value))
                return "UPSERT 1"

        monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
        import json

        state = _state(scheduled_due=True, schedule_anchor=1725450000.5)
        await ar._save_retrain_state(state)

        keys = dict(written)
        # P3-1c: las claves viven en el namespace del par (EURUSD, legado)
        assert "retrain_state.EURUSD.scheduled_due" in keys
        assert "retrain_state.EURUSD.schedule_anchor" in keys
        assert json.loads(keys["retrain_state.EURUSD.scheduled_due"]) is True
        assert json.loads(keys["retrain_state.EURUSD.schedule_anchor"]) == 1725450000.5

    @pytest.mark.asyncio
    async def test_load_parses_schedule_keys(self, monkeypatch):
        class FakePool:
            async def fetch(self, query, params):
                return [
                    {"key": "retrain_state.EURUSD.filled_count", "value": 3},
                    {"key": "retrain_state.EURUSD.last_retrain_time", "value": 0},
                    {"key": "retrain_state.EURUSD.scheduled_due", "value": True},
                    {"key": "retrain_state.EURUSD.schedule_anchor", "value": 1725450000},
                ]

        monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
        state = await ar._load_retrain_state()

        assert state.scheduled_due is True
        assert state.schedule_anchor == 1725450000

    @pytest.mark.asyncio
    async def test_load_defaults_when_schedule_keys_absent(self, monkeypatch):
        class FakePool:
            async def fetch(self, query, params=None):
                # params=None: también la query de bootstrap (trade_outcomes)
                return []

        monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
        state = await ar._load_retrain_state()

        assert state.scheduled_due is False
        assert state.schedule_anchor == 0.0

    def test_parse_bool_tolerates_strings(self):
        assert ar._parse_bool(True) is True
        assert ar._parse_bool("true") is True
        assert ar._parse_bool("false") is False
        assert ar._parse_bool(None, default=True) is True
        assert ar._parse_bool(0) is False


class TestConfigAndWiring:
    def test_interval_declared_positive(self):
        assert settings.retrain_interval_hours > 0

    def test_loop_wired_in_lifespan(self):
        import os

        from services import drift_monitor as dm
        with open(os.path.join(os.path.dirname(dm.__file__), "..", "main.py")) as f:
            src = f.read()
        assert "_bg_tasks.add(asyncio.create_task(_retrain_schedule_loop()))" in src

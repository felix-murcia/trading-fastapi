"""
P2-4: riesgo persistente (DB + rollover diario) y exposición agregada.

Cobertura:
- Rollover: un baseline de AYER se sustituye al primer observe(equity) de hoy
  (el límite diario mide un día real, no "desde el último arranque").
- Restauración: un reinicio a mitad de día PRESERVA la pérdida ya sufrida y
  el peak (init_risk_state con fila de hoy → NO se siembra con el equity
  actual).
- Primera vez / fila vencida → baseline = equity actual y persistencia
  inmediata.
- Sin equity al arrancar (MCP caído) → sin escritura: el primer observe() del
  día siembra y el primer flush persiste.
- flush_risk_state: sin dirty NO toca la BD; con dirty hace upsert de la
  cuenta y del par; un fallo de escritura NO lanza (la protección en memoria
  sigue viva) y conserva dirty para reintentar.
- Racha por par con la convención >= 3 EUR (is_winning_close decide).
- Exposición AGREGADA: cupo del par con cupo total de cuenta alcanzado →
  bloqueo MAX_TOTAL_POSITIONS (settings.max_total_positions).
- schema.sql: ambas tablas en el DDL (fresh install completo).
"""
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.risk_state_store as store
from services import risk_guardian as rg

_SCHEMA = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
_TODAY = date.today()
_YESTERDAY = _TODAY - timedelta(days=1)


@pytest.fixture
def fresh(monkeypatch):
    """Cuenta + guardianes limpios (aislamiento entre tests)."""
    monkeypatch.setattr(rg, "_GUARDIANS", {})
    monkeypatch.setattr(rg, "_ACCOUNT", rg.AccountRiskState())
    return rg


def _pool(fetchrow=None, fetch=None):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow)
    pool.fetch = AsyncMock(return_value=fetch if fetch is not None else [])
    pool.execute = AsyncMock()
    return pool


class TestRollover:
    def test_stday_baseline_rolls_over_on_observe(self, fresh):
        account = fresh.get_account_state()
        account.restore(_YESTERDAY, daily_start_equity=1000.0, peak_equity=1200.0)

        account.observe(950.0)  # primer equity de HOY

        assert account.day == _TODAY
        # Día nuevo: baseline = equity actual (la caída de ayer ya no cuenta)
        assert account.daily_start_equity == 950.0
        assert account.peak_equity == 950.0
        assert account.dirty is True  # el rollover se persiste en el próximo flush

    def test_same_day_observe_only_moves_peak(self, fresh):
        account = fresh.get_account_state()
        account.restore(_TODAY, daily_start_equity=1000.0, peak_equity=1000.0)

        account.observe(980.0)  # misma fecha → NO reinicia el baseline
        assert account.daily_start_equity == 1000.0
        assert account.peak_equity == 1000.0  # sube solo si equity > peak

        account.observe(1075.0)
        assert account.daily_start_equity == 1000.0  # intacto
        assert account.peak_equity == 1075.0
        assert account.dirty is True

    def test_observe_equity_module_seeds_unseeded_account(self, fresh):
        # Sin fila ni baseline (day=None): observe siembra como día nuevo
        fresh.observe_equity(420.0)
        account = fresh.get_account_state()
        assert account.day == _TODAY
        assert account.daily_start_equity == 420.0
        assert account.dirty is True


class TestInitRiskState:
    @pytest.mark.asyncio
    async def test_same_day_row_restores_real_baseline_not_current_equity(self, fresh):
        """EL FIX: reinicio a mitad de día con pérdidas ya tomadas → el
        baseline NO se reinicia con el equity actual (420): se conserva el
        del arranque del día (400) y el peak (465)."""
        pool = _pool(fetchrow={
            "day": _TODAY, "daily_start_equity": 400.0, "peak_equity": 465.0,
        })
        with patch.object(store, "get_pool", return_value=pool):
            await store.init_risk_state(current_equity=420.0)

        account = fresh.get_account_state()
        assert account.daily_start_equity == 400.0
        assert account.peak_equity == 465.0
        assert account.dirty is False       # restaurado, no modificado
        pool.execute.assert_not_called()    # misma fila → sin escritura

    @pytest.mark.asyncio
    async def test_stale_row_rolls_over_and_persists(self, fresh):
        pool = _pool(fetchrow={
            "day": _YESTERDAY, "daily_start_equity": 500.0, "peak_equity": 510.0,
        })
        with patch.object(store, "get_pool", return_value=pool):
            await store.init_risk_state(current_equity=420.0)

        account = fresh.get_account_state()
        assert account.day == _TODAY
        assert account.daily_start_equity == 420.0
        assert account.peak_equity == 420.0
        assert account.dirty is False       # ya persistido por init
        pool.execute.assert_awaited_once()  # el día nuevo se escribe YA

    @pytest.mark.asyncio
    async def test_no_row_first_boot_starts_and_persists(self, fresh):
        pool = _pool(fetchrow=None)
        with patch.object(store, "get_pool", return_value=pool):
            await store.init_risk_state(current_equity=418.0)

        account = fresh.get_account_state()
        assert account.daily_start_equity == 418.0
        pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_without_equity_defers_seeding_to_first_observe(self, fresh):
        pool = _pool(fetchrow=None)
        with patch.object(store, "get_pool", return_value=pool):
            await store.init_risk_state(current_equity=None)

        account = fresh.get_account_state()
        assert account.day is None          # nada sembrado sin equity
        pool.execute.assert_not_called()    # y nada escrito

    @pytest.mark.asyncio
    async def test_pair_rows_restore_streak_and_alert_cooldown(self, fresh):
        guardian = fresh.get_risk_guardian("EURUSD")
        pool = _pool(
            fetchrow={"day": _TODAY, "daily_start_equity": 400.0, "peak_equity": 465.0},
            fetch=[{"symbol": "EURUSD", "consecutive_losses": 3, "last_alert_ts": 123.0}],
        )
        with patch.object(store, "get_pool", return_value=pool):
            await store.init_risk_state(current_equity=420.0)

        assert guardian.state.consecutive_losses == 3
        assert guardian.state.last_alert_ts == 123.0
        # Un par sin fila conserva sus defaults
        other = fresh.get_risk_guardian("GBPUSD")
        assert other.state.consecutive_losses == 0


class TestFlush:
    @pytest.mark.asyncio
    async def test_clean_state_never_touches_db(self, fresh):
        pool = _pool()
        with patch.object(store, "get_pool", return_value=pool):
            await store.flush_risk_state(symbol="EURUSD")
        pool.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_dirty_account_upserts_and_clears(self, fresh):
        account = fresh.get_account_state()
        account.start(430.0)
        pool = _pool()
        with patch.object(store, "get_pool", return_value=pool):
            await store.flush_risk_state(symbol="EURUSD")

        pool.execute.assert_awaited_once()
        args = pool.execute.call_args.args
        assert str(args[0]).strip().startswith("INSERT INTO risk_account_state")
        assert args[1] == account.day and args[2] == 430.0 and args[3] == 430.0
        assert account.dirty is False

    @pytest.mark.asyncio
    async def test_dirty_pair_upserts_streak(self, fresh):
        guardian = fresh.get_risk_guardian("EURUSD")
        guardian.record_trade_result(False)
        pool = _pool()
        with patch.object(store, "get_pool", return_value=pool):
            await store.flush_risk_state(symbol="EURUSD")

        sql, symbol, streak, alert_ts = pool.execute.call_args.args
        assert "risk_pair_state" in sql
        assert (symbol, streak) == ("EURUSD", 1)
        assert guardian.state.dirty is False

    @pytest.mark.asyncio
    async def test_write_failure_keeps_dirty_and_does_not_raise(self, fresh):
        account = fresh.get_account_state()
        account.start(430.0)
        pool = _pool()
        pool.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(store, "get_pool", return_value=pool):
            await store.flush_risk_state(symbol="EURUSD")  # jamás lanza

        assert account.dirty is True  # conservado → se reintenta en el próximo flush

    @pytest.mark.asyncio
    async def test_flush_without_pool_keeps_dirty_and_does_not_raise(self, fresh):
        # Suite sin BD: get_pool() → RuntimeError. La protección en memoria
        # sigue viva; el estado queda dirty para el próximo flush.
        account = fresh.get_account_state()
        account.start(430.0)
        with patch.object(store, "get_pool", side_effect=RuntimeError("DB pool not initialized")):
            await store.flush_risk_state(symbol="EURUSD")
        assert account.dirty is True


class TestStreakConvention:
    def test_loss_by_3eur_rule_marks_dirty_and_accumulates(self, fresh):
        from core.rules import is_winning_close

        g = fresh.get_risk_guardian("EURUSD")
        # convención: 1.5 EUR < 3 → pérdida aunque pnl_pct sea positivo
        assert is_winning_close(1.5) is False
        fresh.record_trade(is_winning_close(1.5), symbol="EURUSD")
        fresh.record_trade(is_winning_close(-4.2), symbol="EURUSD")
        assert g.state.consecutive_losses == 2
        assert g.state.dirty is True

        fresh.record_trade(is_winning_close(10.0), symbol="EURUSD")  # >= 3 → win
        assert g.state.consecutive_losses == 0

    def test_streak_is_per_pair(self, fresh):
        fresh.record_trade(False, symbol="EURUSD")
        assert fresh.get_risk_guardian("EURUSD").state.consecutive_losses == 1
        assert fresh.get_risk_guardian("GBPUSD").state.consecutive_losses == 0


class TestAggregateExposure:
    def test_pair_free_but_account_at_cap_blocks(self, fresh):
        g = fresh.get_risk_guardian("EURUSD")
        g.set_daily_start(1000.0)

        ok, reason = g.can_open_position(1000.0, open_positions=0, total_open_positions=5, total_risk_eur=0.0, same_dir_open=0)
        assert ok is False
        assert reason == "MAX_TOTAL_POSITIONS 5 reached"  # settings.max_total_positions=5

    def test_below_caps_allows(self, fresh):
        g = fresh.get_risk_guardian("EURUSD")
        g.set_daily_start(1000.0)
        ok, _ = g.can_open_position(1000.0, open_positions=4, total_open_positions=4, total_risk_eur=0.0, same_dir_open=0)
        assert ok is True

    def test_pair_cap_still_applies_first(self, fresh):
        g = fresh.get_risk_guardian("EURUSD")
        g.set_daily_start(1000.0)
        ok, reason = g.can_open_position(1000.0, open_positions=5, total_open_positions=5, total_risk_eur=0.0, same_dir_open=0)
        assert ok is False and "MAX_POSITIONS" in reason  # cupo del par antes que el agregado

    def test_check_risk_requires_total_explicitly(self, fresh):
        with pytest.raises(TypeError):
            fresh.check_risk(1000.0, 0, symbol="EURUSD")  # sin total → no pasa


class TestSchema:
    def test_schema_declares_both_tables(self):
        with open(_SCHEMA, encoding="utf-8") as f:
            ddl = f.read()
        assert "CREATE TABLE IF NOT EXISTS risk_account_state" in ddl
        assert "CREATE TABLE IF NOT EXISTS risk_pair_state" in ddl
        assert "CHECK (id = 1)" in ddl  # fila única de cuenta

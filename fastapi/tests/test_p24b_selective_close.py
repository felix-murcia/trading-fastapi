"""
P2-4b: cierre selectivo por ledger de propiedad — los mundos no se pisan.

- `position_ownership`: partición propias/ajenas por el ledger `orders`
  (cycle_id LIKE 'smc\\_%' + mt5_order_id = ticket), cierre por ticket
  individual con aislamiento de fallos y log de lo que NO se cierra.
- Endpoint `/close`: cierra y registra en auto-retrain SOLO las suyas; con
  solo posiciones ajenas → skip/no_owned_position. Antes cerraba TODO el
  símbolo (`DELETE /positions/symbol/{symbol}`) y registraba la posición
  del EA como si fuera de SMC.
- `news-check`: mismo criterio selectivo + ahora persiste sus cierres
  propios en trade_outcomes (antes: jamás los registraba).
- ExecutionPort: expone `close_position(ticket)` (el primitivo real);
  `close_positions_by_symbol` eliminado por zero llamantes (doctrina P3-1d).

Nota: el MCP no expone magic en /positions → el ledger `orders` es la única
fuente de verdad de propiedad (magic=0 además lo comparten los manuales).
"""
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.mt5_client as mt5_client
import services.position_ownership as po
from routers import smc
from core.adapters import MT5ExecutionDataAdapter
from core.ports import ExecutionPort

_EA = {"ticket": 222, "symbol": "EURUSD", "type": "buy", "volume": 0.1,
       "price": 1.10, "sl": 1.095, "tp": 1.12, "profit": 3.4, "state": ""}
_OWN = {"ticket": 111, "symbol": "EURUSD", "type": "buy", "volume": 0.1,
        "price": 1.099, "sl": 1.094, "tp": 1.115, "profit": 5.0, "state": ""}


def _ledger_pool(rows):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


def _audit_pool():
    pool = MagicMock()
    pool.execute = AsyncMock()
    return pool


# ─── position_ownership (unidad) ──────────────────────────────────────────────

class TestLedgerPartition:
    @pytest.mark.asyncio
    async def test_closes_only_ledger_tickets_and_reports_foreign(self):
        ledger = _ledger_pool([{"mt5_order_id": "111"}])
        with patch.object(po, "get_pool", return_value=ledger), \
             patch("services.mt5_client.close_position", AsyncMock()) as close:
            result = await po.close_positions_owned("EURUSD", [_OWN, _EA])

        close.assert_awaited_once_with(111)          # jamás el ticket del EA
        assert result == {"closed": [111], "foreign": [222], "failed": []}

    @pytest.mark.asyncio
    async def test_ledger_query_is_prefixed_and_status_scoped(self):
        ledger = _ledger_pool([])
        with patch.object(po, "get_pool", return_value=ledger), \
             patch("services.mt5_client.close_position", AsyncMock()):
            await po.close_positions_owned("EURUSD", [_OWN])

        args = ledger.fetch.call_args.args
        sql = " ".join(args[0].split())
        assert args[1] == "EURUSD"
        assert args[2] == r"smc\_%"          # '_' escapado: no virar wildcard
        assert "cycle_id LIKE $2 ESCAPE" in sql
        assert "status IN ('placed', 'filled')" in sql
        assert "mt5_order_id <> ''" in sql

    @pytest.mark.asyncio
    async def test_foreign_only_leaves_everything_open(self):
        ledger = _ledger_pool([])  # el EA no está en nuestro ledger
        with patch.object(po, "get_pool", return_value=ledger), \
             patch("services.mt5_client.close_position", AsyncMock()) as close:
            result = await po.close_positions_owned("EURUSD", [_EA])

        close.assert_not_called()
        assert result == {"closed": [], "foreign": [222], "failed": []}

    @pytest.mark.asyncio
    async def test_empty_candidates_skip_the_ledger_query(self):
        ledger = _ledger_pool([{"mt5_order_id": "111"}])
        with patch.object(po, "get_pool", return_value=ledger), \
             patch("services.mt5_client.close_position", AsyncMock()) as close:
            result = await po.close_positions_owned("EURUSD", [])

        ledger.fetch.assert_not_called()   # sin posiciones no hay nada que cerrar
        close.assert_not_called()
        assert result == {"closed": [], "foreign": [], "failed": []}

    @pytest.mark.asyncio
    async def test_one_close_failure_does_not_abort_the_rest(self):
        # Ledger: propios 111 y 333; 222 (EA) queda fuera — a pesar de estar
        # en la lista de candidatos debe ir a "foreign", no a "owned".
        ledger = _ledger_pool([{"mt5_order_id": "111"}, {"mt5_order_id": "333"}])
        own2 = dict(_OWN, ticket=333)
        with patch.object(po, "get_pool", return_value=ledger), \
             patch("services.mt5_client.close_position",
                   AsyncMock(side_effect=[RuntimeError("boom"), None])) as close:
            result = await po.close_positions_owned("EURUSD", [_OWN, _EA, own2])

        assert close.await_count == 2                      # no aborta en el 1º
        assert result["closed"] == [333]
        assert result["foreign"] == [222]
        assert len(result["failed"]) == 1
        assert "boom" in result["failed"][0]["error"]


# ─── Endpoint POST /v1/smc/close ─────────────────────────────────────────────

async def _call_close(positions, ledger_rows, close_side_effect=None):
    with ExitStack() as stack:
        stack.enter_context(patch.object(po, "get_pool", return_value=_ledger_pool(ledger_rows)))
        audit = _audit_pool()
        stack.enter_context(patch.object(smc, "get_pool", return_value=audit))
        stack.enter_context(patch("services.mt5_client.get_positions",
                                  AsyncMock(return_value={"open": positions})))
        close = stack.enter_context(patch("services.mt5_client.close_position",
                                          AsyncMock(side_effect=close_side_effect)))
        record = stack.enter_context(patch.object(smc, "record_trade_filled", AsyncMock()))
        resp = await smc.close_position(smc.CloseRequest(symbol="EURUSD"))
        return resp, close, record, audit


class TestCloseEndpoint:
    @pytest.mark.asyncio
    async def test_closes_and_records_only_own_position(self):
        resp, close, record, audit = await _call_close(
            [_OWN, _EA], ledger_rows=[{"mt5_order_id": "111"}]
        )

        assert resp["action"] == "closed"
        assert resp["tickets"] == [111]
        assert resp["foreign"] == [222]            # el EA queda visible, intacto
        close.assert_awaited_once_with(111)
        # auto-retrain: SOLO la suya (antes registraba las dos)
        assert record.await_count == 1
        assert record.call_args.kwargs["symbol"] == "EURUSD"
        # audit refleja lo cerrado y lo saltado (sql, cycle_id, event, data)
        audit_args = audit.execute.call_args.args
        assert audit_args[2] == "position_closed"
        data = json.loads(audit_args[3])
        assert data["closed_tickets"] == [111]
        assert data["skipped_foreign"] == [222]

    @pytest.mark.asyncio
    async def test_only_foreign_position_is_skipped_not_closed(self):
        resp, close, record, _ = await _call_close(
            [_EA], ledger_rows=[{"mt5_order_id": "111"}]
        )

        assert resp == {"action": "skip", "reason": "no_owned_position",
                        "foreign": [222], "failed": []}
        close.assert_not_called()      # la posición del EA NO se cierra
        record.assert_not_called()     # ni se registra para retrain

    @pytest.mark.asyncio
    async def test_out_of_domain_reason_maps_to_signal_end(self):
        # 'ema_cross' (default) viola el CHECK de trade_outcomes → asyncpg
        # rechazaba el INSERT y el cierre quedaba sin persistir.
        resp, close, record, _ = await _call_close(
            [_OWN], ledger_rows=[{"mt5_order_id": "111"}]
        )
        assert resp["action"] == "closed"
        assert record.call_args.kwargs["exit_reason"] == "signal_end"

    @pytest.mark.asyncio
    async def test_failed_close_reports_error_and_records_nothing(self):
        resp, close, record, _ = await _call_close(
            [_OWN], ledger_rows=[{"mt5_order_id": "111"}],
            close_side_effect=RuntimeError("gateway down"),
        )

        assert resp["action"] == "skip"
        assert resp["reason"] == "close_failed"
        assert "gateway down" in resp["failed"][0]["error"]
        record.assert_not_called()     # cierre fallido → no hay trade que registrar


# ─── Endpoint POST /v1/smc/news-check ────────────────────────────────────────

class TestNewsCheckSelective:
    @pytest.mark.asyncio
    async def test_news_close_only_own_and_persists_it(self):
        upcoming = [{"affected_symbols": ["EURUSD"], "title": "NFP",
                     "time_utc": "2026-09-30T12:00:00", "minutes_until": 10}]
        with ExitStack() as stack:
            stack.enter_context(patch.object(po, "get_pool",
                                             return_value=_ledger_pool([{"mt5_order_id": "111"}])))
            audit = _audit_pool()
            stack.enter_context(patch.object(smc, "get_pool", return_value=audit))
            stack.enter_context(patch("services.mt5_client.get_positions",
                                      AsyncMock(return_value={"open": [_OWN, _EA]})))
            stack.enter_context(patch("services.news_filter.get_upcoming_news",
                                      AsyncMock(return_value=upcoming)))
            close = stack.enter_context(patch("services.mt5_client.close_position", AsyncMock()))
            record = stack.enter_context(patch.object(smc, "record_trade_filled", AsyncMock()))

            resp = await smc.news_check(smc.NewsCheckRequest(symbol="EURUSD"))

        close.assert_awaited_once_with(111)
        # Convención AGENTS.md: el cierre propio SÍ se persiste (antes nunca)
        assert record.await_count == 1
        assert record.call_args.kwargs["exit_reason"] == "news"
        assert resp["action"] == "closed"
        assert resp["closed"][0]["tickets"] == [111]
        assert resp["closed"][0]["foreign"] == 1     # la del EA, no tocada

    @pytest.mark.asyncio
    async def test_news_close_with_only_foreign_closes_nothing(self):
        upcoming = [{"affected_symbols": ["EURUSD"], "title": "CPI",
                     "time_utc": "2026-09-30T12:00:00", "minutes_until": 5}]
        with ExitStack() as stack:
            stack.enter_context(patch.object(po, "get_pool",
                                             return_value=_ledger_pool([])))
            stack.enter_context(patch.object(smc, "get_pool", return_value=_audit_pool()))
            stack.enter_context(patch("services.mt5_client.get_positions",
                                      AsyncMock(return_value={"open": [_EA]})))
            stack.enter_context(patch("services.news_filter.get_upcoming_news",
                                      AsyncMock(return_value=upcoming)))
            close = stack.enter_context(patch("services.mt5_client.close_position", AsyncMock()))
            record = stack.enter_context(patch.object(smc, "record_trade_filled", AsyncMock()))

            resp = await smc.news_check(smc.NewsCheckRequest(symbol="EURUSD"))

        close.assert_not_called()
        record.assert_not_called()
        assert resp["action"] == "skip"
        assert resp["closed"] == []


class TestExecutionPortContract:
    def test_port_exposes_close_by_ticket(self):
        adapter = MT5ExecutionDataAdapter()
        assert isinstance(adapter, ExecutionPort)
        assert hasattr(adapter, "close_position")
        # Doctrina P3-1d: el puerto (y la librería) no guardan lo que nadie usa
        assert not hasattr(adapter, "close_positions_by_symbol")
        assert not hasattr(mt5_client, "close_positions_by_symbol")

    @pytest.mark.asyncio
    async def test_adapter_close_position_sees_service_patch(self):
        with patch("services.mt5_client.close_position",
                   AsyncMock(return_value={"ok": True})) as m:
            out = await MT5ExecutionDataAdapter().close_position(12345)
        m.assert_awaited_once_with(12345)
        assert out == {"ok": True}

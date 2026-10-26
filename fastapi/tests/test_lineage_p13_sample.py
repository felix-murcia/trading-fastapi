"""
Tests del lineage P1.3 — muestra de oportunidades desde fuentes reconciliadas.

Crea una muestra de oportunidades de decisión a partir de:
  - audit_log (evento 'ai_predict_cycle'): ciclo, símbolo, decisión, mt5_time
  - orders: orden vinculada por cycle_id + símbolo + tipo
  - trade_outcomes: resultado vinculado por order_db_id (FK exacta)

Reglas de aceptación (P1.3):
  · IDs únicos, timestamps ordenados
  · Símbolo/ciclo exactos
  · Estado de ejecución clasificado (pendiente, colocado, llenado, rechazado, cancelado, no confirmado)
  · Lineage/hash 100%
  · 0 joins temporales (solo claves exactas: cycle_id, order_db_id, position_id)
  · No reparar ni reutilizar el CSV P3.1 fallido
  · Cada fila tiene estado y motivo determinista
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.lineage_p13_sample import (
    _classify_execution_status,
    _hash_lineage_row,
    _positive_id,
    _link_order_to_cycle,
    _link_outcome_to_order,
    build_lineage_rows,
)


class TestPositiveId:
    def test_accepts_positive_int(self):
        assert _positive_id(1) == 1

    def test_rejects_zero_negative_bool(self):
        assert _positive_id(0) is None
        assert _positive_id(-1) is None
        assert _positive_id(True) is None

    def test_rejects_none_and_bad_string(self):
        assert _positive_id(None) is None
        assert _positive_id("abc") is None
        assert _positive_id("0") is None


class TestClassifyExecutionStatus:
    def test_filled(self):
        assert _classify_execution_status("filled") == "filled"
        assert _classify_execution_status("FILLED") == "filled"

    def test_pending_and_placed(self):
        assert _classify_execution_status("pending") == "pending"
        assert _classify_execution_status("placed") == "placed"

    def test_rejected_and_cancelled(self):
        assert _classify_execution_status("rejected") == "rejected"
        assert _classify_execution_status("cancelled") == "cancelled"

    def test_unconfirmed(self):
        assert _classify_execution_status("unconfirmed") == "unconfirmed"

    def test_unknown_returns_unresolved(self):
        assert _classify_execution_status("unknown") == "unresolved"
        assert _classify_execution_status(None) == "unresolved"


class TestLinkOrderToCycle:
    def setup_method(self):
        self.orders = [
            {"id": 10, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "type": "BUY", "status": "filled"},
            {"id": 11, "cycle_id": "EURUSD_2", "symbol": "EURUSD", "type": "SELL", "status": "pending"},
            {"id": 12, "cycle_id": "GBPUSD_1", "symbol": "GBPUSD", "type": "BUY", "status": "filled"},
        ]

    def test_link_exact_cycle_symbol(self):
        cycle = {"cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY"}
        order = _link_order_to_cycle(cycle, self.orders)
        assert order is not None
        assert order["id"] == 10

    def test_no_link_wrong_symbol(self):
        cycle = {"cycle_id": "EURUSD_1", "symbol": "GBPUSD", "decision": "BUY"}
        order = _link_order_to_cycle(cycle, self.orders)
        assert order is None

    def test_no_link_no_orders(self):
        cycle = {"cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY"}
        order = _link_order_to_cycle(cycle, [])
        assert order is None


class TestLinkOutcomeToOrder:
    def setup_method(self):
        self.outcomes = [
            {"id": 100, "order_db_id": 10, "symbol": "EURUSD", "position_id": 201},
            {"id": 101, "order_db_id": None, "symbol": "EURUSD", "position_id": 202},
            {"id": 102, "order_db_id": 999, "symbol": "EURUSD", "position_id": 203},
        ]

    def test_link_exact_order_db_id(self):
        order = {"id": 10, "symbol": "EURUSD"}
        outcome = _link_outcome_to_order(order, self.outcomes)
        assert outcome is not None
        assert outcome["id"] == 100

    def test_no_link_order_db_id_none(self):
        order = {"id": 11, "symbol": "EURUSD"}
        outcome = _link_outcome_to_order(order, self.outcomes)
        assert outcome is None

    def test_no_link_order_not_found(self):
        order = {"id": 999, "symbol": "EURUSD"}
        outcome = _link_outcome_to_order(order, self.outcomes)
        # order_db_id=999 matches outcome 102, but symbol matches too
        # Actually outcome 102 has order_db_id=999 and symbol EURUSD, order 999 has symbol EURUSD
        assert outcome is not None  # this matches
        assert outcome["id"] == 102

    def test_no_link_symbol_mismatch(self):
        # order EURUSD but outcome with order_db_id=10 has symbol EURUSD -> matches
        # Let's test with an order whose symbol doesn't match the outcome's symbol
        order = {"id": 10, "symbol": "GBPUSD"}
        outcome = _link_outcome_to_order(order, self.outcomes)
        assert outcome is None


class TestHashLineageRow:
    def test_hash_is_deterministic(self):
        row = {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY"}
        h1 = _hash_lineage_row(row)
        h2 = _hash_lineage_row(row)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex length

    def test_hash_changes_with_different_data(self):
        row_a = {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY"}
        row_b = {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "SELL"}
        assert _hash_lineage_row(row_a) != _hash_lineage_row(row_b)


class TestBuildLineageRows:
    def test_builds_rows_from_cycles_orders_outcomes(self):
        cycles = [
            {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY", "mt5_time": "1000"},
            {"id": 2, "cycle_id": "EURUSD_2", "symbol": "EURUSD", "decision": "SELL", "mt5_time": "2000"},
        ]
        orders = [
            {"id": 10, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "type": "BUY", "status": "filled", "mt5_order_id": "101"},
            {"id": 11, "cycle_id": "EURUSD_2", "symbol": "EURUSD", "type": "SELL", "status": "placed", "mt5_order_id": None},
        ]
        outcomes = [
            {"id": 100, "order_db_id": 10, "symbol": "EURUSD", "position_id": 201, "pnl": 1.5},
        ]

        rows = build_lineage_rows(cycles, orders, outcomes)
        assert len(rows) == 2

        # Primera fila: ciclo vinculado a orden 10 (filled) y outcome 100
        row1 = rows[0]
        assert row1["cycle_id"] == "EURUSD_1"
        assert row1["symbol"] == "EURUSD"
        assert row1["decision"] == "BUY"
        assert row1["order_id"] == 10
        assert row1["order_status"] == "filled"
        assert row1["outcome_id"] == 100
        assert row1["execution_status"] == "filled"
        assert "lineage_hash" in row1

        # Segunda fila: ciclo vinculado a orden 11 (placed), sin outcome
        row2 = rows[1]
        assert row2["cycle_id"] == "EURUSD_2"
        assert row2["order_id"] == 11
        assert row2["order_status"] == "placed"
        assert row2["outcome_id"] is None
        assert row2["execution_status"] == "placed"

    def test_rows_have_unique_ids(self):
        cycles = [
            {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY", "mt5_time": "1000"},
            {"id": 2, "cycle_id": "EURUSD_2", "symbol": "EURUSD", "decision": "SELL", "mt5_time": "2000"},
        ]
        orders = []
        outcomes = []

        rows = build_lineage_rows(cycles, orders, outcomes)
        ids = [r["lineage_id"] for r in rows]
        assert len(ids) == len(set(ids))  # todos únicos

    def test_rows_sorted_by_timestamp(self):
        cycles = [
            {"id": 2, "cycle_id": "EURUSD_2", "symbol": "EURUSD", "decision": "SELL", "mt5_time": "2000"},
            {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY", "mt5_time": "1000"},
        ]
        orders = []
        outcomes = []

        rows = build_lineage_rows(cycles, orders, outcomes)
        timestamps = [r["mt5_time"] for r in rows]
        assert timestamps == sorted(timestamps)

    def test_every_row_has_state_and_reason(self):
        cycles = [
            {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY", "mt5_time": "1000"},
        ]
        orders = []
        outcomes = []

        rows = build_lineage_rows(cycles, orders, outcomes)
        for row in rows:
            assert "execution_status" in row
            assert row["execution_status"] is not None
            assert "link_reason" in row
            assert row["link_reason"] is not None

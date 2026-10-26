"""
Tests del backfill P0.2c — enlaces order_db_id en trade_outcomes.

Vincula cada outcome sin order_db_id a su orden exacta en `orders` usando:
  outcome.position_id -> orders.mt5_position_id
  outcome.symbol -> orders.symbol
  outcome.direction -> orders.type (LONG→BUY, SHORT→SELL)

Reglas:
  - 0 joins temporales (solo enlaces exactos por posición+símbolo+dirección)
  - Si hay exactamente una coincidencia: se enlaza
  - Si hay cero o múltiples: no se toca, razón documentada
  - No silent failures
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Importar las funciones puras del script de backfill
from scripts.backfill_p02c_order_links import (
    _direction_to_order_type,
    _match_outcome_to_order,
    _positive_id,
    _summarize_backfill,
)


class TestPositiveId:
    def test_accepts_positive_int(self):
        assert _positive_id(1) == 1
        assert _positive_id(42) == 42

    def test_rejects_zero_and_negative(self):
        assert _positive_id(0) is None
        assert _positive_id(-1) is None

    def test_rejects_bool(self):
        assert _positive_id(True) is None
        assert _positive_id(False) is None

    def test_rejects_none_and_string(self):
        assert _positive_id(None) is None
        assert _positive_id("abc") is None
        assert _positive_id("0") is None


class TestDirectionToOrderType:
    def test_long_maps_to_buy(self):
        assert _direction_to_order_type("LONG") == "BUY"
        assert _direction_to_order_type("long") == "BUY"
        assert _direction_to_order_type("  LONG  ") == "BUY"

    def test_short_maps_to_sell(self):
        assert _direction_to_order_type("SHORT") == "SELL"
        assert _direction_to_order_type("short") == "SELL"

    def test_buy_and_sell_pass_through(self):
        assert _direction_to_order_type("BUY") == "BUY"
        assert _direction_to_order_type("SELL") == "SELL"

    def test_invalid_returns_none(self):
        assert _direction_to_order_type("HOLD") is None
        assert _direction_to_order_type(None) is None
        assert _direction_to_order_type("") is None


class TestMatchOutcomeToOrder:
    def setup_method(self):
        self.orders = [
            {"id": 10, "symbol": "EURUSD", "type": "BUY", "mt5_position_id": 201},
            {"id": 11, "symbol": "EURUSD", "type": "SELL", "mt5_position_id": 202},
            {"id": 12, "symbol": "GBPUSD", "type": "BUY", "mt5_position_id": 301},
            {"id": 13, "symbol": "GBPUSD", "type": "SELL", "mt5_position_id": 302},
            {"id": 14, "symbol": "USDCHF", "type": "BUY", "mt5_position_id": 401},
            # Orden sin mt5_position_id (no debe matchear por posición)
            {"id": 15, "symbol": "EURUSD", "type": "BUY", "mt5_position_id": None},
        ]

    def test_match_exact_position_symbol_direction(self):
        outcome = {"position_id": 201, "symbol": "EURUSD", "direction": "LONG"}
        order_id, reason = _match_outcome_to_order(outcome, self.orders)
        assert order_id == 10
        assert reason == "exact_position_symbol_direction_match"

    def test_match_short_direction(self):
        outcome = {"position_id": 202, "symbol": "EURUSD", "direction": "SHORT"}
        order_id, reason = _match_outcome_to_order(outcome, self.orders)
        assert order_id == 11
        assert reason == "exact_position_symbol_direction_match"

    def test_no_match_missing_position_id(self):
        outcome = {"position_id": None, "symbol": "EURUSD", "direction": "LONG"}
        order_id, reason = _match_outcome_to_order(outcome, self.orders)
        assert order_id is None
        assert reason == "missing_or_invalid_position_id"

    def test_no_match_wrong_symbol(self):
        outcome = {"position_id": 201, "symbol": "GBPUSD", "direction": "LONG"}
        order_id, reason = _match_outcome_to_order(outcome, self.orders)
        assert order_id is None
        assert reason == "no_order_for_position_symbol_direction"

    def test_no_match_wrong_direction(self):
        outcome = {"position_id": 201, "symbol": "EURUSD", "direction": "SHORT"}
        order_id, reason = _match_outcome_to_order(outcome, self.orders)
        assert order_id is None
        assert reason == "no_order_for_position_symbol_direction"

    def test_no_match_position_not_found(self):
        outcome = {"position_id": 999, "symbol": "EURUSD", "direction": "LONG"}
        order_id, reason = _match_outcome_to_order(outcome, self.orders)
        assert order_id is None
        assert reason == "no_order_for_position_symbol_direction"

    def test_no_match_order_without_position_id(self):
        # outcome.position_id es None, no puede matchear contra orden sin mt5_position_id
        outcome = {"position_id": None, "symbol": "EURUSD", "direction": "LONG"}
        order_id, reason = _match_outcome_to_order(outcome, self.orders)
        assert order_id is None
        assert reason == "missing_or_invalid_position_id"

    def test_multiple_matches_return_none(self):
        orders_dup = [
            {"id": 10, "symbol": "EURUSD", "type": "BUY", "mt5_position_id": 201},
            {"id": 11, "symbol": "EURUSD", "type": "BUY", "mt5_position_id": 201},
        ]
        outcome = {"position_id": 201, "symbol": "EURUSD", "direction": "LONG"}
        order_id, reason = _match_outcome_to_order(outcome, orders_dup)
        assert order_id is None
        assert reason == "multiple_orders_for_position_symbol_direction"


class TestSummarizeBackfill:
    def test_empty_results(self):
        summary = _summarize_backfill([])
        assert summary["total_outcomes_without_fk"] == 0
        assert summary["linked"] == 0
        assert summary["not_linked"] == 0
        assert summary["by_reason"] == {}

    def test_summarizes_links_and_reasons(self):
        results = [
            {"outcome_id": 1, "linked": True, "order_id": 10, "reason": "exact_position_symbol_direction_match"},
            {"outcome_id": 2, "linked": True, "order_id": 11, "reason": "exact_position_symbol_direction_match"},
            {"outcome_id": 3, "linked": False, "order_id": None, "reason": "missing_or_invalid_position_id"},
            {"outcome_id": 4, "linked": False, "order_id": None, "reason": "no_order_for_position_symbol_direction"},
            {"outcome_id": 5, "linked": False, "order_id": None, "reason": "multiple_orders_for_position_symbol_direction"},
        ]
        summary = _summarize_backfill(results)
        assert summary["total_outcomes_without_fk"] == 5
        assert summary["linked"] == 2
        assert summary["not_linked"] == 3
        assert summary["by_reason"] == {
            "exact_position_symbol_direction_match": 2,
            "missing_or_invalid_position_id": 1,
            "no_order_for_position_symbol_direction": 1,
            "multiple_orders_for_position_symbol_direction": 1,
        }

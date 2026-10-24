import json
from decimal import Decimal

from ops.reconcile_replay_sources import (
    _json_safe,
    build_reconciliation_report,
    classify_source_rows,
)


def test_classifies_every_internal_source_row_with_exact_links_only():
    report = classify_source_rows(
        cycles=[
            {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY", "mt5_time": "1"},
            {"id": 2, "cycle_id": "EURUSD_2", "symbol": "EURUSD", "decision": "BUY", "mt5_time": "2"},
            {"id": 3, "cycle_id": "EURUSD_2", "symbol": "EURUSD", "decision": "SELL", "mt5_time": "3"},
            {"id": 4, "cycle_id": "EURUSD_3", "symbol": "GBPUSD", "decision": "HOLD", "mt5_time": None},
        ],
        orders=[
            {
                "id": 10,
                "cycle_id": "EURUSD_1",
                "symbol": "EURUSD",
                "status": "filled",
                "mt5_order_id": "101",
                "mt5_position_id": 201,
            },
            {
                "id": 11,
                "cycle_id": "EURUSD_2",
                "symbol": "EURUSD",
                "status": "filled",
                "mt5_order_id": None,
                "mt5_position_id": None,
            },
            {
                "id": 12,
                "cycle_id": "not-found",
                "symbol": "EURUSD",
                "status": "rejected",
                "mt5_order_id": None,
                "mt5_position_id": None,
            },
            {
                "id": 13,
                "cycle_id": "EURUSD_1",
                "symbol": "EURUSD",
                "status": "cancelled",
                "mt5_order_id": None,
                "mt5_position_id": None,
            },
            {
                "id": 14,
                "cycle_id": "EURUSD_1",
                "symbol": "EURUSD",
                "status": "pending",
                "mt5_order_id": None,
                "mt5_position_id": None,
            },
            {
                "id": 15,
                "cycle_id": "EURUSD_1",
                "symbol": "EURUSD",
                "status": "unconfirmed",
                "mt5_order_id": None,
                "mt5_position_id": None,
            },
        ],
        fills=[
            {"id": 20, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "order_db_id": "10"},
            {"id": 21, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "order_db_id": None},
            {"id": 22, "cycle_id": "EURUSD_1", "symbol": "USDCHF", "order_db_id": 10},
        ],
        outcomes=[
            {"id": 30, "symbol": "EURUSD", "deal_ticket": 301, "position_id": 201, "order_db_id": 10},
            {"id": 31, "symbol": "EURUSD", "deal_ticket": None, "position_id": None, "order_db_id": None},
        ],
    )

    assert report["cycles"]["rows"] == report["cycles"]["classified_rows"] == 4
    assert report["cycles"]["unclassified_rows"] == 0
    assert report["cycles"]["rows_without_reason"] == 0
    assert report["cycles"]["status_counts"] == {"eligible": 1, "unresolved": 3}
    assert report["cycles"]["reason_counts"]["conflicting_cycle_decision"] == 2
    assert report["cycles"]["reason_counts"]["missing_mt5_time"] == 1
    assert report["orders"]["status_counts"] == {
        "awaiting_fill": 1,
        "filled_ids_present": 1,
        "terminal_unfilled": 1,
        "unresolved": 3,
    }
    assert report["orders"]["rows_without_reason"] == 0
    assert report["orders"]["reason_counts"]["cycle_id_ambiguous"] == 1
    assert report["orders"]["reason_counts"]["missing_or_invalid_mt5_order_id"] == 1
    assert report["orders"]["reason_counts"]["broker_execution_unconfirmed"] == 1
    assert report["fills"]["status_counts"] == {"linked_to_order": 1, "unresolved": 2}
    assert report["fills"]["rows_without_reason"] == 0
    assert report["fills"]["reason_counts"]["fill_order_symbol_mismatch"] == 1
    assert report["outcomes"]["status_counts"] == {
        "linked_to_internal_order": 1,
        "unresolved": 1,
    }
    assert report["outcomes"]["rows_without_reason"] == 0
    assert report["outcomes"]["reason_counts"]["missing_or_invalid_deal_ticket"] == 1


def test_row_classification_digest_is_stable_for_same_snapshot():
    source_rows = {
        "cycles": [
            {"id": 1, "cycle_id": "EURUSD_1", "symbol": "EURUSD", "decision": "BUY", "mt5_time": "1"}
        ],
        "orders": [],
        "fills": [],
        "outcomes": [],
    }

    first = classify_source_rows(**source_rows)
    second = classify_source_rows(**source_rows)

    assert first == second
    assert first["cycles"]["classification_sha256"]


def test_reports_unreconciled_cycle_trade_and_account_gaps():
    report = build_reconciliation_report(
        snapshot_utc="2026-10-23T13:24:48+00:00",
        cycles={
            "rows": 100,
            "unique_ids": 90,
            "repeated_rows": 10,
            "missing_mt5_time": 5,
            "outside_universe": 2,
        },
        decisions=[
            {"symbol": "EURUSD", "decision": "BUY", "n": 12},
            {"symbol": "EURUSD", "decision": "HOLD", "n": 88},
        ],
        orders={
            "total": 20,
            "by_status": {"filled": 18, "pending": 2},
            "with_audit_cycle": 15,
            "cycle_repeated_in_audit": 1,
            "without_audit_cycle": 5,
            "missing_mt5_order_id": 19,
        },
        outcomes={
            "total": 30,
            "realized_pnl": 12.5,
            "missing_deal_ticket": 10,
            "missing_position_id": 10,
            "missing_order_db_id": 30,
        },
        fills={"total": 15, "with_order_db_id": 15},
        equity={
            "snapshots": 5,
            "first_utc": "2026-10-21T00:00:00+00:00",
            "last_utc": "2026-10-23T00:00:00+00:00",
            "latest_balance": 508.94,
            "latest_equity": 510.0,
        },
    )

    assert report["source_linkage_status"] == "UNRECONCILED"
    assert report["cycles"]["repeated_rows"] == 10
    assert report["orders"]["without_audit_cycle"] == 5
    assert report["order_trade_linkage"]["status"] == "INCOMPLETE"
    assert report["order_trade_linkage"]["missing"] == 30
    assert "trade_outcomes_missing_exact_order_link" in report["blocking_gaps"]
    assert "trade_outcomes_missing_deal_identifiers" in report["blocking_gaps"]


def test_does_not_call_sources_reconciled_without_order_trade_key():
    report = build_reconciliation_report(
        snapshot_utc="2026-10-23T13:24:48+00:00",
        cycles={
            "rows": 4,
            "unique_ids": 4,
            "repeated_rows": 0,
            "missing_mt5_time": 0,
            "outside_universe": 0,
        },
        decisions=[
            {"symbol": "EURUSD", "decision": "BUY", "n": 2},
            {"symbol": "EURUSD", "decision": "HOLD", "n": 2},
        ],
        orders={
            "total": 1,
            "by_status": {"filled": 1},
            "with_audit_cycle": 1,
            "cycle_repeated_in_audit": 0,
            "without_audit_cycle": 0,
            "missing_mt5_order_id": 0,
        },
        outcomes={
            "total": 1,
            "realized_pnl": 3.0,
            "missing_deal_ticket": 0,
            "missing_position_id": 0,
            "missing_order_db_id": 0,
        },
        fills={"total": 1, "with_order_db_id": 1},
        equity={
            "snapshots": 1,
            "first_utc": "2026-10-23T00:00:00+00:00",
            "last_utc": "2026-10-23T00:00:00+00:00",
            "latest_balance": 500.0,
            "latest_equity": 500.0,
        },
    )

    assert report["source_linkage_status"] == "RECONCILED"
    assert report["blocking_gaps"] == []
    assert report["order_trade_linkage"]["key"] == "trade_outcomes.order_db_id -> orders.id"
    assert report["cash_reconciliation_status"] == "NOT_ESTABLISHED"


def test_json_safe_serializes_nested_decimal_values():
    value = {
        "realized_pnl": Decimal("347.2100"),
        "equity": [Decimal("513.26")],
    }

    serialized = json.dumps(_json_safe(value))

    assert json.loads(serialized) == {
        "realized_pnl": "347.2100",
        "equity": ["513.26"],
    }


def test_report_flags_link_migration_as_missing_without_assigning_historical_links():
    report = build_reconciliation_report(
        snapshot_utc="2026-10-23T13:30:00+00:00",
        cycles={
            "rows": 0,
            "unique_ids": 0,
            "repeated_rows": 0,
            "missing_mt5_time": 0,
            "outside_universe": 0,
        },
        decisions=[],
        orders={
            "total": 0,
            "without_audit_cycle": 0,
            "cycle_repeated_in_audit": 0,
            "missing_mt5_order_id": 0,
        },
        outcomes={
            "total": 2,
            "realized_pnl": "5.00",
            "missing_deal_ticket": 0,
            "missing_position_id": 0,
            "missing_order_db_id": 2,
        },
        fills={"total": 0, "with_order_db_id": 0},
        equity={
            "snapshots": 0,
            "first_utc": None,
            "last_utc": None,
            "latest_balance": None,
            "latest_equity": None,
        },
        order_link_schema_available=False,
    )

    assert report["source_linkage_status"] == "UNRECONCILED"
    assert report["order_trade_linkage"]["status"] == "SCHEMA_NOT_MIGRATED"
    assert report["order_trade_linkage"]["missing"] == 2
    assert "order_trade_link_schema_not_migrated" in report["blocking_gaps"]

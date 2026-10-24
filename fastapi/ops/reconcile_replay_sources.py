"""Read-only snapshot of decision, order, outcome, and equity source linkage."""

import asyncio
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any


_UNIVERSE = {"EURUSD", "GBPUSD", "USDCHF"}
_DECISIONS = {"BUY", "SELL", "HOLD"}


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    return None


def _summarize_classifications(rows, classifier):
    entries = []
    statuses = Counter()
    reasons = Counter()
    for row in rows:
        status, row_reasons = classifier(row)
        row_reasons = sorted(set(row_reasons))
        entries.append({
            "id": str(row.get("id", "")),
            "status": status,
            "reasons": row_reasons,
        })
        statuses[status] += 1
        reasons.update(row_reasons)

    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return {
        "rows": len(rows),
        "classified_rows": len(entries),
        "unclassified_rows": len(rows) - len(entries),
        "rows_without_reason": sum(not entry["reasons"] for entry in entries),
        "status_counts": dict(sorted(statuses.items())),
        "reason_counts": dict(sorted(reasons.items())),
        "classification_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def classify_source_rows(
    *,
    cycles: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    cycles_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cycle in cycles:
        cycle_id = cycle.get("cycle_id")
        if isinstance(cycle_id, str) and cycle_id:
            cycles_by_id[cycle_id].append(cycle)

    cycle_decisions = {
        cycle_id: {row.get("decision") for row in rows}
        for cycle_id, rows in cycles_by_id.items()
    }
    cycle_symbols = {
        cycle_id: {row.get("symbol") for row in rows}
        for cycle_id, rows in cycles_by_id.items()
    }

    def classify_cycle(row):
        reasons = []
        cycle_id = row.get("cycle_id")
        if not isinstance(cycle_id, str) or not cycle_id:
            reasons.append("missing_cycle_id")
        elif len(cycles_by_id[cycle_id]) > 1:
            reasons.append("duplicate_cycle_id")
            if len(cycle_decisions[cycle_id]) > 1:
                reasons.append("conflicting_cycle_decision")
            if len(cycle_symbols[cycle_id]) > 1:
                reasons.append("conflicting_cycle_symbol")
        if row.get("symbol") not in _UNIVERSE:
            reasons.append("symbol_outside_frozen_universe")
        if row.get("decision") not in _DECISIONS:
            reasons.append("invalid_decision")
        if row.get("mt5_time") in (None, ""):
            reasons.append("missing_mt5_time")
        status = "unresolved" if reasons else "eligible"
        if not reasons:
            reasons.append("required_fields_valid")
        return status, reasons

    orders_by_id = {
        _positive_id(row.get("id")): row
        for row in orders
        if _positive_id(row.get("id")) is not None
    }

    def cycle_link_reasons(row):
        reasons = []
        cycle_id = row.get("cycle_id")
        matches = cycles_by_id.get(cycle_id, [])
        if not matches:
            reasons.append("cycle_id_not_found")
        elif len(matches) > 1:
            reasons.append("cycle_id_ambiguous")
        elif matches[0].get("symbol") != row.get("symbol"):
            reasons.append("cycle_symbol_mismatch")
        return reasons

    def classify_order(row):
        reasons = cycle_link_reasons(row)
        status = row.get("status")
        if status == "filled":
            if _positive_id(row.get("mt5_order_id")) is None:
                reasons.append("missing_or_invalid_mt5_order_id")
            if _positive_id(row.get("mt5_position_id")) is None:
                reasons.append("missing_or_invalid_mt5_position_id")
            result = "filled_ids_present"
            if not reasons:
                reasons.append("exact_cycle_and_broker_ids_present")
        elif status in {"pending", "placed"}:
            result = "awaiting_fill"
            reasons.append("fill_not_yet_observed")
        elif status in {"rejected", "cancelled"}:
            result = "terminal_unfilled"
            reasons.append("terminal_without_fill")
        elif status == "unconfirmed":
            result = "unresolved"
            reasons.append("broker_execution_unconfirmed")
        else:
            reasons.append("invalid_order_status")
            result = "unresolved"
        if any(
            reason not in {
                "fill_not_yet_observed",
                "terminal_without_fill",
                "exact_cycle_and_broker_ids_present",
            }
            for reason in reasons
        ):
            result = "unresolved"
        return result, reasons

    def classify_fill(row):
        reasons = []
        order_id = _positive_id(row.get("order_db_id"))
        order = orders_by_id.get(order_id)
        if order_id is None:
            reasons.append("missing_or_invalid_order_db_id")
        elif order is None:
            reasons.append("order_db_id_not_found")
        else:
            if row.get("symbol") and row.get("symbol") != order.get("symbol"):
                reasons.append("fill_order_symbol_mismatch")
            if row.get("cycle_id") and row.get("cycle_id") != order.get("cycle_id"):
                reasons.append("fill_order_cycle_mismatch")
        status = "unresolved" if reasons else "linked_to_order"
        if not reasons:
            reasons.append("exact_order_db_id_match")
        return status, reasons

    def classify_outcome(row):
        reasons = []
        if _positive_id(row.get("deal_ticket")) is None:
            reasons.append("missing_or_invalid_deal_ticket")
        if _positive_id(row.get("position_id")) is None:
            reasons.append("missing_or_invalid_position_id")
        order_id = _positive_id(row.get("order_db_id"))
        order = orders_by_id.get(order_id)
        if order_id is None:
            reasons.append("missing_or_invalid_order_db_id")
        elif order is None:
            reasons.append("order_db_id_not_found")
        elif row.get("symbol") != order.get("symbol"):
            reasons.append("outcome_order_symbol_mismatch")
        status = "unresolved" if reasons else "linked_to_internal_order"
        if not reasons:
            reasons.append("exact_internal_order_fk_match")
        return status, reasons

    return {
        "cycles": _summarize_classifications(cycles, classify_cycle),
        "orders": _summarize_classifications(orders, classify_order),
        "fills": _summarize_classifications(fills, classify_fill),
        "outcomes": _summarize_classifications(outcomes, classify_outcome),
    }


def build_reconciliation_report(
    *,
    snapshot_utc: str,
    cycles: dict[str, int],
    decisions: list[dict[str, Any]],
    orders: dict[str, Any],
    outcomes: dict[str, Any],
    fills: dict[str, int],
    equity: dict[str, Any],
    order_link_schema_available: bool = True,
    source_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    gaps = []
    if cycles["repeated_rows"]:
        gaps.append("duplicate_cycle_ids")
    if cycles["missing_mt5_time"]:
        gaps.append("cycles_missing_mt5_time")
    if cycles["outside_universe"]:
        gaps.append("cycles_outside_frozen_universe")
    if cycles.get("decision_conflict_ids"):
        gaps.append("cycle_ids_with_conflicting_decisions")
    if cycles.get("symbol_conflict_ids") or cycles.get("cycle_id_symbol_mismatches"):
        gaps.append("cycle_id_symbol_mismatch")
    if orders["without_audit_cycle"] or orders["cycle_repeated_in_audit"]:
        gaps.append("orders_not_uniquely_linked_to_decisions")
    if orders["missing_mt5_order_id"]:
        gaps.append("filled_orders_missing_broker_order_id")
    if fills["total"] != fills["with_order_db_id"]:
        gaps.append("fill_events_missing_db_order_id")
    if outcomes["missing_deal_ticket"] or outcomes["missing_position_id"]:
        gaps.append("trade_outcomes_missing_deal_identifiers")
    missing_order_links = outcomes.get("missing_order_db_id", outcomes["total"])
    if not order_link_schema_available:
        gaps.append("order_trade_link_schema_not_migrated")
    elif missing_order_links:
        gaps.append("trade_outcomes_missing_exact_order_link")

    report = {
        "snapshot_utc": snapshot_utc,
        "source_linkage_status": "RECONCILED" if not gaps else "UNRECONCILED",
        "blocking_gaps": gaps,
        "cycles": cycles,
        "decision_counts": decisions,
        "orders": orders,
        "outcomes": outcomes,
        "order_trade_linkage": {
            "status": (
                "SCHEMA_NOT_MIGRATED" if not order_link_schema_available
                else "COMPLETE" if not missing_order_links
                else "INCOMPLETE"
            ),
            "linked": outcomes["total"] - missing_order_links,
            "missing": missing_order_links,
            "key": "trade_outcomes.order_db_id -> orders.id",
        },
        "fill_events": fills,
        "equity_snapshots": equity,
        "cash_reconciliation_status": "NOT_ESTABLISHED",
        "cash_reconciliation_note": (
            "No se han incluido extractos de depósitos/retiros ni un balance "
            "inicial de fuente verificable; realized PnL no se equipara a "
            "variación de balance/equity."
        ),
    }
    if source_rows is not None:
        report["row_classification"] = classify_source_rows(**source_rows)
    return report


_CYCLE_SUMMARY_SQL = """
WITH normalized AS (
    SELECT cycle_id,
           COALESCE(data->'data'->>'symbol', data->>'symbol') AS symbol,
           COALESCE(data->'data'->>'decision', data->>'decision') AS decision,
           COALESCE(data->'data'->>'mt5_time', data->>'mt5_time') AS mt5_time
    FROM audit_log
    WHERE event = 'ai_predict_cycle'
), repeated AS (
    SELECT cycle_id, count(*) AS n
    FROM normalized
    GROUP BY cycle_id
    HAVING count(*) > 1
)
SELECT count(*) AS rows,
       count(DISTINCT cycle_id) AS unique_ids,
       (SELECT COALESCE(sum(n - 1), 0)::bigint FROM repeated) AS repeated_rows,
       count(*) FILTER (WHERE mt5_time IS NULL) AS missing_mt5_time,
       count(*) FILTER (
           WHERE symbol IS NULL OR symbol NOT IN ('EURUSD', 'GBPUSD', 'USDCHF')
       ) AS outside_universe,
       (SELECT count(*) FROM (
           SELECT cycle_id FROM normalized
           GROUP BY cycle_id HAVING count(DISTINCT decision) > 1
       ) AS conflicts) AS decision_conflict_ids,
       (SELECT count(*) FROM (
           SELECT cycle_id FROM normalized
           GROUP BY cycle_id HAVING count(DISTINCT symbol) > 1
       ) AS conflicts) AS symbol_conflict_ids,
       count(*) FILTER (
           WHERE split_part(cycle_id, '_', 1) IN ('EURUSD', 'GBPUSD', 'USDCHF')
             AND split_part(cycle_id, '_', 1) <> symbol
       ) AS cycle_id_symbol_mismatches
FROM normalized
"""

_DECISION_COUNTS_SQL = """
SELECT COALESCE(data->'data'->>'symbol', data->>'symbol') AS symbol,
       COALESCE(data->'data'->>'decision', data->>'decision') AS decision,
       count(*) AS n
FROM audit_log
WHERE event = 'ai_predict_cycle'
GROUP BY 1, 2
ORDER BY 1, 2
"""

_ORDER_SUMMARY_SQL = """
WITH cycle_counts AS (
    SELECT cycle_id, count(*) AS n
    FROM audit_log
    WHERE event = 'ai_predict_cycle'
    GROUP BY cycle_id
)
SELECT count(*) AS total,
       count(*) FILTER (WHERE cycles.n > 0) AS with_audit_cycle,
       count(*) FILTER (WHERE cycles.n > 1) AS cycle_repeated_in_audit,
       count(*) FILTER (WHERE cycles.n IS NULL) AS without_audit_cycle,
       count(*) FILTER (
           WHERE orders.status = 'filled'
             AND (orders.mt5_order_id IS NULL OR orders.mt5_order_id = '')
       ) AS missing_mt5_order_id
FROM orders
LEFT JOIN cycle_counts AS cycles USING (cycle_id)
"""

_OUTCOME_SUMMARY_SQL = """
SELECT count(*) AS total,
       COALESCE(sum(pnl), 0) AS realized_pnl,
       count(*) FILTER (WHERE deal_ticket IS NULL) AS missing_deal_ticket,
       count(*) FILTER (WHERE position_id IS NULL) AS missing_position_id,
       count(*) FILTER (WHERE order_db_id IS NULL) AS missing_order_db_id
FROM trade_outcomes
"""

_OUTCOME_SUMMARY_SQL_LEGACY = """
SELECT count(*) AS total,
       COALESCE(sum(pnl), 0) AS realized_pnl,
       count(*) FILTER (WHERE deal_ticket IS NULL) AS missing_deal_ticket,
       count(*) FILTER (WHERE position_id IS NULL) AS missing_position_id
FROM trade_outcomes
"""

_CYCLE_ROWS_SQL = """
SELECT id,
       cycle_id,
       COALESCE(data->'data'->>'symbol', data->>'symbol') AS symbol,
       COALESCE(data->'data'->>'decision', data->>'decision') AS decision,
       COALESCE(data->'data'->>'mt5_time', data->>'mt5_time') AS mt5_time
FROM audit_log
WHERE event = 'ai_predict_cycle'
ORDER BY id
"""

_ORDER_ROWS_SQL = """
SELECT id, cycle_id, symbol, status, mt5_order_id, mt5_position_id
FROM orders
ORDER BY id
"""

_FILL_ROWS_SQL = """
SELECT id,
       cycle_id,
       COALESCE(data->'data'->>'symbol', data->>'symbol') AS symbol,
       COALESCE(data->'data'->>'order_db_id', data->>'order_db_id') AS order_db_id
FROM audit_log
WHERE event = 'order_filled'
ORDER BY id
"""

_OUTCOME_ROWS_SQL = """
SELECT id, symbol, deal_ticket, position_id, order_db_id
FROM trade_outcomes
ORDER BY id
"""

_OUTCOME_ROWS_SQL_LEGACY = """
SELECT id, symbol, deal_ticket, position_id, NULL::bigint AS order_db_id
FROM trade_outcomes
ORDER BY id
"""


async def _fetch_snapshot(pool):
    async with pool.acquire() as connection:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            snapshot_utc = await connection.fetchval("SELECT statement_timestamp()")
            order_link_schema_available = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.key_column_usage AS kcu
                    JOIN information_schema.table_constraints AS tc
                      USING (constraint_catalog, constraint_schema, constraint_name, table_name)
                    JOIN information_schema.constraint_column_usage AS ccu
                      USING (constraint_catalog, constraint_schema, constraint_name)
                    WHERE kcu.table_schema = current_schema()
                      AND kcu.table_name = 'trade_outcomes'
                      AND kcu.column_name = 'order_db_id'
                      AND tc.constraint_type = 'FOREIGN KEY'
                      AND ccu.table_schema = current_schema()
                      AND ccu.table_name = 'orders'
                      AND ccu.column_name = 'id'
                )
                """
            )
            cycles = await connection.fetchrow(_CYCLE_SUMMARY_SQL)
            decisions = await connection.fetch(_DECISION_COUNTS_SQL)
            orders = await connection.fetchrow(_ORDER_SUMMARY_SQL)
            outcomes = await connection.fetchrow(
                _OUTCOME_SUMMARY_SQL if order_link_schema_available
                else _OUTCOME_SUMMARY_SQL_LEGACY
            )
            fills = await connection.fetchrow(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE data ? 'order_db_id') AS with_order_db_id
                FROM audit_log
                WHERE event = 'order_filled'
                """
            )
            equity = await connection.fetchrow(
                """
                SELECT count(*) AS snapshots,
                       min(ts) AS first_utc,
                       max(ts) AS last_utc,
                       (array_agg(balance ORDER BY ts DESC))[1] AS latest_balance,
                       (array_agg(equity ORDER BY ts DESC))[1] AS latest_equity
                FROM equity_snapshots
                """
            )
            source_rows = {
                "cycles": [dict(row) for row in await connection.fetch(_CYCLE_ROWS_SQL)],
                "orders": [dict(row) for row in await connection.fetch(_ORDER_ROWS_SQL)],
                "fills": [dict(row) for row in await connection.fetch(_FILL_ROWS_SQL)],
                "outcomes": [
                    dict(row)
                    for row in await connection.fetch(
                        _OUTCOME_ROWS_SQL if order_link_schema_available
                        else _OUTCOME_ROWS_SQL_LEGACY
                    )
                ],
            }
    return (
        snapshot_utc, cycles, decisions, orders, outcomes, fills, equity,
        order_link_schema_available, source_rows,
    )


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


async def main() -> int:
    from db.connection import close_pool, get_pool, init_pool

    await init_pool()
    try:
        (
            snapshot_utc, cycles, decisions, orders, outcomes, fills, equity,
            order_link_schema_available, source_rows,
        ) = (
            await _fetch_snapshot(get_pool())
        )
    finally:
        await close_pool()

    report = build_reconciliation_report(
        snapshot_utc=snapshot_utc.isoformat(),
        cycles=dict(cycles),
        decisions=[dict(row) for row in decisions],
        orders=dict(orders),
        outcomes={
            **dict(outcomes),
            **(
                {"missing_order_db_id": outcomes["total"]}
                if not order_link_schema_available else {}
            ),
        },
        fills=dict(fills),
        equity=dict(equity),
        order_link_schema_available=order_link_schema_available,
        source_rows=source_rows,
    )
    print(json.dumps(_json_safe(report), indent=2, ensure_ascii=False))
    return 0 if report["source_linkage_status"] == "RECONCILED" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

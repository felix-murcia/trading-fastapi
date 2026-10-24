"""Read-only audit of exact broker-deal, outcome, and internal-order identifiers."""

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg
import httpx

_AIQ_COMMENT = re.compile(r"AIQ:([1-9][0-9]*)\Z")


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    return None


def _identifier_present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and value == "")


def _decimal_value(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a valid decimal value.") from None
    if not decimal_value.is_finite():
        raise ValueError(f"{field} must be finite.")
    return decimal_value


def _deal_order_candidates(
    deal: dict[str, Any],
    orders_by_ticket: dict[tuple[str, int], set[int]],
    orders_by_position: dict[tuple[str, int], set[int]],
    orders_by_id: dict[tuple[str, int], set[int]],
) -> tuple[set[int], bool, bool, bool, bool, bool, bool]:
    symbol = deal.get("symbol")
    if not isinstance(symbol, str):
        return set(), False, False, False, False, False, False

    order_id = _positive_id(deal.get("order"))
    order_candidates = (
        orders_by_ticket.get((symbol, order_id), set())
        if order_id is not None else set()
    )
    position_id = _positive_id(deal.get("position_id"))
    position_candidates = (
        orders_by_position.get((symbol, position_id), set())
        if position_id is not None else set()
    )
    comment = deal.get("comment")
    comment_match = _AIQ_COMMENT.fullmatch(comment) if isinstance(comment, str) else None
    comment_id = _positive_id(comment_match.group(1)) if comment_match else None
    comment_candidates = (
        orders_by_id.get((symbol, comment_id), set())
        if comment_id is not None else set()
    )

    sources = [
        candidates
        for candidates in (order_candidates, position_candidates, comment_candidates)
        if candidates
    ]
    candidates = set().union(*sources) if sources else set()
    conflicting = len(candidates) > 1
    ambiguous = any(len(source) > 1 for source in sources)
    unique_consistent = len(candidates) == 1 and not conflicting and not ambiguous
    return (
        candidates,
        bool(order_candidates),
        bool(position_candidates),
        bool(comment_candidates),
        conflicting,
        ambiguous,
        unique_consistent,
    )


def build_broker_link_report(
    *,
    snapshot_utc: str,
    query: dict[str, str],
    deals: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    orders_by_ticket: dict[tuple[str, int], set[int]] = defaultdict(set)
    orders_by_position: dict[tuple[str, int], set[int]] = defaultdict(set)
    orders_by_id: dict[tuple[str, int], set[int]] = defaultdict(set)
    for order in orders:
        symbol = order.get("symbol")
        order_id = _positive_id(order.get("id"))
        if not isinstance(symbol, str) or order_id is None:
            continue
        orders_by_id[(symbol, order_id)].add(order_id)
        mt5_order_id = _positive_id(order.get("mt5_order_id"))
        if mt5_order_id is not None:
            orders_by_ticket[(symbol, mt5_order_id)].add(order_id)
        mt5_position_id = _positive_id(order.get("mt5_position_id"))
        if mt5_position_id is not None:
            orders_by_position[(symbol, mt5_position_id)].add(order_id)

    deals_by_ticket: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for deal in deals:
        symbol = deal.get("symbol")
        if not isinstance(symbol, str):
            continue
        ticket = _positive_id(deal.get("ticket"))
        if ticket is not None:
            deals_by_ticket[(symbol, ticket)].append(deal)

    order_ticket_matches = 0
    position_matches = 0
    comment_matches = 0
    consistent_deals = 0
    conflicting_deals = 0
    ambiguous_deals = 0
    candidates_by_deal_ticket: dict[tuple[str, int], set[int]] = {}
    for deal in deals:
        (
            candidates,
            matched_ticket,
            matched_position,
            matched_comment,
            conflicting,
            ambiguous,
            consistent,
        ) = _deal_order_candidates(
            deal, orders_by_ticket, orders_by_position, orders_by_id
        )
        order_ticket_matches += matched_ticket
        position_matches += matched_position
        comment_matches += matched_comment
        conflicting_deals += conflicting
        ambiguous_deals += ambiguous
        consistent_deals += consistent
        symbol = deal.get("symbol")
        ticket = _positive_id(deal.get("ticket"))
        if isinstance(symbol, str) and ticket is not None:
            candidates_by_deal_ticket[(symbol, ticket)] = candidates

    deal_counts_by_symbol = Counter(
        deal["symbol"] for deal in deals if isinstance(deal.get("symbol"), str)
    )
    exact_aiq_comment_count = sum(
        bool(_AIQ_COMMENT.fullmatch(deal.get("comment", "")))
        for deal in deals
        if isinstance(deal.get("comment", ""), str)
    )
    outcome_counts = {
        "total": len(outcomes),
        "missing_deal_ticket": 0,
        "exact_deal_ticket_match": 0,
        "unmatched_deal_ticket": 0,
        "ambiguous_deal_ticket": 0,
        "exact_position_id_match": 0,
        "with_order_db_id": 0,
    }
    confirmed = 0
    unique_broker_without_fk = 0
    fk_identity_conflicts = 0
    pnl_reconciliation = {
        "exact_ticket_close_deals": 0,
        "exact_ticket_non_close_deals": 0,
        "exact_ticket_deals_with_unclassified_entry": 0,
        "close_deals_with_all_reported_cost_fields": 0,
        "recorded_pnl_matches_profit_only": 0,
        "recorded_pnl_matches_profit_plus_swap": 0,
        "recorded_pnl_matches_profit_plus_all_reported_costs": 0,
        "close_deals_with_nonzero_swap": 0,
        "nonzero_swap_unincluded_from_recorded_pnl": 0,
        "comparisons_missing_required_fields": 0,
    }
    for outcome in outcomes:
        symbol = outcome.get("symbol")
        ticket = _positive_id(outcome.get("deal_ticket"))
        position_id = _positive_id(outcome.get("position_id"))
        order_db_id = _positive_id(outcome.get("order_db_id"))
        outcome_counts["missing_deal_ticket"] += ticket is None
        outcome_counts["with_order_db_id"] += order_db_id is not None
        if not isinstance(symbol, str) or ticket is None:
            continue
        ticket_deals = deals_by_ticket.get((symbol, ticket), [])
        if not ticket_deals:
            outcome_counts["unmatched_deal_ticket"] += 1
            continue
        if len(ticket_deals) > 1:
            outcome_counts["ambiguous_deal_ticket"] += 1
            continue
        outcome_counts["exact_deal_ticket_match"] += 1
        deal_position_id = _positive_id(ticket_deals[0].get("position_id"))
        if position_id is not None and deal_position_id == position_id:
            outcome_counts["exact_position_id_match"] += 1
        deal = ticket_deals[0]
        if deal.get("entry") in (1, "1"):
            pnl_reconciliation["exact_ticket_close_deals"] += 1
            recorded_pnl = _decimal_value(outcome.get("pnl"), "outcome.pnl")
            profit = _decimal_value(deal.get("profit"), "deal.profit")
            swap = _decimal_value(deal.get("swap"), "deal.swap")
            commission = _decimal_value(deal.get("commission"), "deal.commission")
            fee = _decimal_value(deal.get("fee"), "deal.fee")
            missing_inputs = False
            if recorded_pnl is None or profit is None:
                missing_inputs = True
            else:
                tolerance = Decimal("0.01")
                matches_profit = abs(recorded_pnl - profit) <= tolerance
                pnl_reconciliation["recorded_pnl_matches_profit_only"] += matches_profit
                if swap is None:
                    missing_inputs = True
                else:
                    matches_profit_swap = abs(recorded_pnl - profit - swap) <= tolerance
                    pnl_reconciliation["recorded_pnl_matches_profit_plus_swap"] += (
                        matches_profit_swap
                    )
                    pnl_reconciliation["close_deals_with_nonzero_swap"] += swap != 0
                    pnl_reconciliation["nonzero_swap_unincluded_from_recorded_pnl"] += (
                        swap != 0 and matches_profit and not matches_profit_swap
                    )
                    if commission is None or fee is None:
                        missing_inputs = True
                    else:
                        pnl_reconciliation["close_deals_with_all_reported_cost_fields"] += 1
                        matches_all_costs = abs(
                            recorded_pnl - profit - swap - commission - fee
                        ) <= tolerance
                        pnl_reconciliation[
                            "recorded_pnl_matches_profit_plus_all_reported_costs"
                        ] += matches_all_costs
            pnl_reconciliation["comparisons_missing_required_fields"] += missing_inputs
        elif deal.get("entry") in (0, "0"):
            pnl_reconciliation["exact_ticket_non_close_deals"] += 1
        else:
            pnl_reconciliation["exact_ticket_deals_with_unclassified_entry"] += 1
        order_candidates = candidates_by_deal_ticket.get((symbol, ticket), set())
        if order_db_id is not None:
            if len(order_candidates) == 1 and order_db_id in order_candidates:
                confirmed += 1
            elif order_candidates and (
                len(order_candidates) > 1 or order_db_id not in order_candidates
            ):
                fk_identity_conflicts += 1
        elif len(order_candidates) == 1:
            unique_broker_without_fk += 1

    linkage_complete = (
        outcome_counts["total"] > 0
        and confirmed == outcome_counts["total"]
        and not outcome_counts["missing_deal_ticket"]
        and not outcome_counts["unmatched_deal_ticket"]
        and not outcome_counts["ambiguous_deal_ticket"]
        and not conflicting_deals
        and not ambiguous_deals
    )
    return {
        "snapshot_utc": snapshot_utc,
        "query": {
            **query,
            "time_filter_semantics": "Broker API interpretation not independently verified.",
        },
        "broker_deals": {
            "total": len(deals),
            "by_symbol": dict(sorted(deal_counts_by_symbol.items())),
            "with_exact_aiq_order_comment": exact_aiq_comment_count,
        },
        "orders": {
            "total": len(orders),
            "with_mt5_order_id": sum(
                _identifier_present(order.get("mt5_order_id")) for order in orders
            ),
            "with_valid_mt5_order_id": sum(
                _positive_id(order.get("mt5_order_id")) is not None for order in orders
            ),
            "invalid_mt5_order_id": sum(
                _identifier_present(order.get("mt5_order_id"))
                and _positive_id(order.get("mt5_order_id")) is None
                for order in orders
            ),
            "with_mt5_position_id": sum(
                _identifier_present(order.get("mt5_position_id")) for order in orders
            ),
            "with_valid_mt5_position_id": sum(
                _positive_id(order.get("mt5_position_id")) is not None for order in orders
            ),
            "invalid_mt5_position_id": sum(
                _identifier_present(order.get("mt5_position_id"))
                and _positive_id(order.get("mt5_position_id")) is None
                for order in orders
            ),
        },
        "outcomes": outcome_counts,
        "pnl_reconciliation": pnl_reconciliation,
        "broker_order_linkage": {
            "status": "COMPLETE" if linkage_complete else "INCOMPLETE",
            "deals_matching_order_ticket": order_ticket_matches,
            "deals_matching_position_id": position_matches,
            "deals_matching_exact_comment": comment_matches,
            "deals_with_unique_consistent_order": consistent_deals,
            "deals_with_conflicting_identifiers": conflicting_deals,
            "deals_with_ambiguous_identifiers": ambiguous_deals,
            "outcomes_confirmed_by_fk_and_broker_identity": confirmed,
            "outcomes_with_unique_broker_identity_but_no_fk": unique_broker_without_fk,
            "outcomes_with_fk_broker_identity_conflict": fk_identity_conflicts,
        },
        "historical_linkage_write_performed": False,
    }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("La fecha debe incluir una zona horaria.")
    return parsed.astimezone(timezone.utc)


async def _fetch_database_snapshot():
    from config import settings

    connection = await asyncpg.connect(settings.database_url)
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            snapshot_utc = await connection.fetchval("SELECT statement_timestamp()")
            orders = await connection.fetch(
                """
                SELECT id, symbol, mt5_order_id, mt5_position_id
                FROM orders
                """
            )
            outcomes = await connection.fetch(
                """
                SELECT symbol, deal_ticket, position_id, order_db_id, pnl
                FROM trade_outcomes
                """
            )
    finally:
        await connection.close()
    return snapshot_utc, [dict(row) for row in orders], [dict(row) for row in outcomes]


async def _fetch_broker_deals(
    symbols: set[str],
    *,
    from_date: str,
    to_date: str,
) -> list[dict[str, Any]]:
    from config import settings

    all_deals = []
    async with httpx.AsyncClient(timeout=settings.mt5_http_timeout) as client:
        for symbol in sorted(symbols):
            response = await client.get(
                f"{settings.mt5_http_url}/api/v1/history/deals",
                params={
                    "from_date": from_date,
                    "to_date": to_date,
                    "symbol": symbol,
                },
            )
            response.raise_for_status()
            deals = response.json()
            if not isinstance(deals, list):
                raise TypeError("history/deals debe devolver una lista JSON.")
            if any(not isinstance(deal, dict) for deal in deals):
                raise TypeError("Cada deal de history/deals debe ser un objeto JSON.")
            all_deals.extend(deals)
    return all_deals


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", required=True, type=_parse_datetime)
    parser.add_argument(
        "--to-date",
        type=_parse_datetime,
        default=datetime.now(timezone.utc),
    )
    args = parser.parse_args()
    if args.from_date > args.to_date:
        parser.error("--from-date no puede ser posterior a --to-date.")
    from_date = args.from_date.isoformat().replace("+00:00", "Z")
    to_date = args.to_date.isoformat().replace("+00:00", "Z")

    snapshot_utc, orders, outcomes = await _fetch_database_snapshot()
    symbols = {
        str(row["symbol"])
        for row in [*orders, *outcomes]
        if row.get("symbol")
    }
    deals = await _fetch_broker_deals(
        symbols,
        from_date=from_date,
        to_date=to_date,
    )
    report = build_broker_link_report(
        snapshot_utc=snapshot_utc.isoformat(),
        query={"from_date": from_date, "to_date": to_date},
        deals=deals,
        orders=orders,
        outcomes=outcomes,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["broker_order_linkage"]["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

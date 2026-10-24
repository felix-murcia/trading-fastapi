from ops.audit_broker_order_links import build_broker_link_report


def test_reconciles_only_exact_broker_order_identifiers():
    report = build_broker_link_report(
        snapshot_utc="2026-10-23T16:00:00+00:00",
        query={"from_date": "2026-08-01T00:00:00Z", "to_date": "2026-10-23T16:00:00Z"},
        deals=[
            {
                "ticket": 1001,
                "order": 501,
                "position_id": 701,
                "symbol": "EURUSD",
                "comment": "AIQ:10",
            },
            {
                "ticket": 1002,
                "order": 502,
                "position_id": 702,
                "symbol": "USDCHF",
                "comment": "AIQ:20",
            },
        ],
        orders=[
            {
                "id": 10,
                "symbol": "EURUSD",
                "mt5_order_id": 501,
                "mt5_position_id": 701,
            },
            {
                "id": 20,
                "symbol": "USDCHF",
                "mt5_order_id": 502,
                "mt5_position_id": 702,
            },
        ],
        outcomes=[
            {
                "symbol": "EURUSD",
                "deal_ticket": 1001,
                "position_id": 701,
                "order_db_id": 10,
            },
            {
                "symbol": "USDCHF",
                "deal_ticket": 1002,
                "position_id": 702,
                "order_db_id": None,
            },
        ],
    )

    assert report["outcomes"] == {
        "total": 2,
        "missing_deal_ticket": 0,
        "exact_deal_ticket_match": 2,
        "unmatched_deal_ticket": 0,
        "ambiguous_deal_ticket": 0,
        "exact_position_id_match": 2,
        "with_order_db_id": 1,
    }
    assert report["broker_order_linkage"] == {
        "status": "INCOMPLETE",
        "deals_matching_order_ticket": 2,
        "deals_matching_position_id": 2,
        "deals_matching_exact_comment": 2,
        "deals_with_unique_consistent_order": 2,
        "deals_with_conflicting_identifiers": 0,
        "deals_with_ambiguous_identifiers": 0,
        "outcomes_confirmed_by_fk_and_broker_identity": 1,
        "outcomes_with_unique_broker_identity_but_no_fk": 1,
        "outcomes_with_fk_broker_identity_conflict": 0,
    }
    assert report["broker_deals"]["with_exact_aiq_order_comment"] == 2
    assert "1001" not in str(report)
    assert "501" not in str(report)


def test_reports_conflicting_and_ambiguous_identifiers_without_linking():
    report = build_broker_link_report(
        snapshot_utc="2026-10-23T16:00:00+00:00",
        query={},
        deals=[
            {
                "ticket": 1001,
                "order": 501,
                "position_id": 701,
                "symbol": "EURUSD",
                "comment": "AIQ:20",
            },
            {
                "ticket": 1002,
                "order": None,
                "position_id": None,
                "symbol": "EURUSD",
                "comment": "AIQ:10",
            },
        ],
        orders=[
            {"id": 10, "symbol": "EURUSD", "mt5_order_id": 501, "mt5_position_id": 701},
            {"id": 20, "symbol": "EURUSD", "mt5_order_id": 501, "mt5_position_id": 701},
        ],
        outcomes=[
            {"symbol": "EURUSD", "deal_ticket": 1001, "position_id": 701, "order_db_id": 10},
            {"symbol": "EURUSD", "deal_ticket": 1002, "position_id": None, "order_db_id": None},
        ],
    )

    assert report["broker_order_linkage"]["deals_with_conflicting_identifiers"] == 1
    assert report["broker_order_linkage"]["deals_with_ambiguous_identifiers"] == 1
    assert report["broker_order_linkage"]["outcomes_confirmed_by_fk_and_broker_identity"] == 0
    assert report["broker_order_linkage"]["outcomes_with_fk_broker_identity_conflict"] == 1


def test_requires_exact_symbol_and_full_comment_match():
    report = build_broker_link_report(
        snapshot_utc="2026-10-23T16:00:00+00:00",
        query={},
        deals=[
            {
                "ticket": 1001,
                "order": 501,
                "position_id": 701,
                "symbol": "EURUSD",
                "comment": "AIQ:10 appended",
            },
        ],
        orders=[
            {"id": 10, "symbol": "USDCHF", "mt5_order_id": 501, "mt5_position_id": 701},
        ],
        outcomes=[
            {"symbol": "USDCHF", "deal_ticket": 1001, "position_id": 701, "order_db_id": None},
        ],
    )

    assert report["outcomes"]["exact_deal_ticket_match"] == 0
    assert report["broker_order_linkage"]["deals_matching_order_ticket"] == 0
    assert report["broker_order_linkage"]["deals_matching_position_id"] == 0
    assert report["broker_order_linkage"]["deals_matching_exact_comment"] == 0
    assert report["broker_deals"]["with_exact_aiq_order_comment"] == 0


def test_duplicate_deal_tickets_are_not_treated_as_exact_matches():
    report = build_broker_link_report(
        snapshot_utc="2026-10-23T16:00:00+00:00",
        query={},
        deals=[
            {"ticket": 1001, "order": 501, "position_id": 701, "symbol": "EURUSD", "comment": ""},
            {"ticket": 1001, "order": 502, "position_id": 702, "symbol": "EURUSD", "comment": ""},
        ],
        orders=[],
        outcomes=[
            {"symbol": "EURUSD", "deal_ticket": 1001, "position_id": None, "order_db_id": None},
        ],
    )

    assert report["outcomes"]["exact_deal_ticket_match"] == 0
    assert report["outcomes"]["ambiguous_deal_ticket"] == 1


def test_position_id_only_counts_when_it_agrees_with_the_ticket_matched_deal():
    report = build_broker_link_report(
        snapshot_utc="2026-10-23T16:00:00+00:00",
        query={},
        deals=[
            {"ticket": 1001, "order": 501, "position_id": 701, "symbol": "EURUSD", "comment": ""},
        ],
        orders=[],
        outcomes=[
            {"symbol": "EURUSD", "deal_ticket": 1001, "position_id": 702, "order_db_id": None},
        ],
    )

    assert report["outcomes"]["exact_deal_ticket_match"] == 1
    assert report["outcomes"]["exact_position_id_match"] == 0


def test_invalid_persisted_identifiers_are_not_counted_as_valid():
    report = build_broker_link_report(
        snapshot_utc="2026-10-23T16:00:00+00:00",
        query={},
        deals=[],
        orders=[
            {"id": 10, "symbol": "EURUSD", "mt5_order_id": "0", "mt5_position_id": 0},
        ],
        outcomes=[],
    )

    assert report["orders"] == {
        "total": 1,
        "with_mt5_order_id": 1,
        "with_valid_mt5_order_id": 0,
        "invalid_mt5_order_id": 1,
        "with_mt5_position_id": 1,
        "with_valid_mt5_position_id": 0,
        "invalid_mt5_position_id": 1,
    }


def test_reports_aggregate_swap_and_cost_reconciliation_for_exact_close_deals():
    report = build_broker_link_report(
        snapshot_utc="2026-10-23T16:00:00+00:00",
        query={},
        deals=[
            {
                "ticket": 1001,
                "symbol": "EURUSD",
                "entry": 1,
                "profit": "1.00",
                "swap": "0.50",
                "commission": "-0.20",
                "fee": "0.00",
            },
            {
                "ticket": 1002,
                "symbol": "EURUSD",
                "entry": "1",
                "profit": "2.00",
                "swap": "-0.25",
                "commission": "0.00",
                "fee": "0.00",
            },
        ],
        orders=[],
        outcomes=[
            {
                "symbol": "EURUSD",
                "deal_ticket": 1001,
                "position_id": None,
                "order_db_id": None,
                "pnl": "1.00",
            },
            {
                "symbol": "EURUSD",
                "deal_ticket": 1002,
                "position_id": None,
                "order_db_id": None,
                "pnl": "1.75",
            },
        ],
    )

    assert report["pnl_reconciliation"] == {
        "exact_ticket_close_deals": 2,
        "exact_ticket_non_close_deals": 0,
        "exact_ticket_deals_with_unclassified_entry": 0,
        "close_deals_with_all_reported_cost_fields": 2,
        "recorded_pnl_matches_profit_only": 1,
        "recorded_pnl_matches_profit_plus_swap": 1,
        "recorded_pnl_matches_profit_plus_all_reported_costs": 1,
        "close_deals_with_nonzero_swap": 2,
        "nonzero_swap_unincluded_from_recorded_pnl": 1,
        "comparisons_missing_required_fields": 0,
    }

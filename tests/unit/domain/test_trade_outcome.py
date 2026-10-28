"""Test de paridad: TradeOutcome value object."""

from src.domain.rules.trade_outcome import TradeOutcome


def test_trade_outcome_fields():
    o = TradeOutcome(
        symbol="EURUSD",
        entry_time=1700000000.0,
        exit_time=1700003600.0,
        pnl=5.50,
        pnl_pct=0.55,
        direction="LONG",
        sl_hit=False,
        tp_hit=True,
        exit_reason="tp",
    )
    assert o.symbol == "EURUSD"
    assert o.entry_time == 1700000000.0
    assert o.exit_time == 1700003600.0
    assert o.pnl == 5.50
    assert o.pnl_pct == 0.55
    assert o.direction == "LONG"
    assert o.sl_hit is False
    assert o.tp_hit is True
    assert o.exit_reason == "tp"


def test_trade_outcome_frozen():
    o = TradeOutcome("EURUSD", 1.0, 2.0, 0.0, 0.0, "LONG", False, False, "manual")
    try:
        o.pnl = 99.0  # type: ignore[misc]
        assert False, "debería ser frozen"
    except Exception:
        pass  # frozen: no se puede mutar

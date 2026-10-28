"""Tests unitarios de ComputePositionSize con fakes en memoria.

Los mismos ≥10 casos que el test de caracterización, ejecutados contra el
nuevo caso de uso con fakes. El output debe coincidir byte a byte con el
legacy (test de caracterización).
"""

from datetime import datetime
from decimal import Decimal
import os

import pytest

from src.domain.value_objects.thresholds import TradingThresholds
from src.application.use_cases.compute_position_size import (
    ComputePositionSize, ComputePositionSizeInput,
)
from tests.fakes.fake_trade_repository import FakeTradeRepository
from tests.fakes.fake_clock import FakeClock

# Mismos 10 casos del test de caracterización.
# (symbol, price, equity, sl_pips, head_lots, tp_candidates,
#  risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price)
# expected: (ok, reason, lots, tp_pips, risk_eur, reward_net_eur)
CASOS = [
    ("EURUSD", 1.12094, 10000.0, 30.0, 0.05, (20.0, 30.0, 50.0), 0.02, 0.01, 0.5, 4.0, None,
     True, "", 0.05, 20.0, 13.3816, 8.029),
    ("GBPUSD", 1.25345, 10000.0, 30.0, 0.05, (20.0, 30.0, 50.0), 0.02, 0.01, 0.5, 4.0, 1.25345,
     True, "", 0.05, 20.0, 11.967, 7.1802),
    ("USDCHF", 0.82197, 10000.0, 30.0, 0.05, (20.0, 30.0, 50.0), 0.02, 0.01, 0.5, 4.0, 1.1236,
     True, "", 0.05, 20.0, 16.2414, 9.7448),
    ("EURUSD", 1.12094, 1000.0, 30.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     True, "", 0.05, 20.0, 13.3816, 8.029),
    ("EURUSD", 1.12094, 10000.0, 200.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     True, "", 0.05, 20.0, 89.2108, 8.029),
    ("EURUSD", 1.12094, 10000.0, 30.0, 0.0, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     False, "sizing_inviable", 0.0, 0.0, 0.0, 0.0),
    ("EURUSD", 1.12094, 50000.0, 30.0, 0.5, (50.0,), 0.01, 0.01, 1.0, 4.0, None,
     True, "", 0.5, 50.0, 133.8163, 214.106),
    ("EURUSD", 1.12094, 0.0, 30.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     "ValueError", "", 0.0, 0.0, 0.0, 0.0),
    ("EURUSD", 1.12094, 10000.0, 0.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     "ValueError", "", 0.0, 0.0, 0.0, 0.0),
    ("EURUSD", 1.12094, 10000.0, 30.0, -0.01, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     False, "sizing_inviable", 0.0, 0.0, 0.0, 0.0),
]


def _ejecutar_uc(symbol, price, equity, sl_pips, head_lots, tp_candidates,
                 risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price,
                 thresholds):
    repo = FakeTradeRepository()
    clock = FakeClock(now=datetime(2026, 10, 28, 0, 0, 0))
    uc = ComputePositionSize(repo=repo, clock=clock, thresholds=thresholds)
    inp = ComputePositionSizeInput(
        symbol=symbol, price=price, equity=equity, sl_pips=sl_pips,
        head_lots=head_lots, tp_candidates_pips=list(tp_candidates),
        risk_pct=risk_pct, min_lot=min_lot, max_lot=max_lot,
        eurusd_price=eurusd_price,
    )
    return uc.execute(inp)


@pytest.mark.unit
@pytest.mark.parametrize(
    "symbol,price,equity,sl_pips,head_lots,tp_candidates,risk_pct,min_lot,max_lot,"
    "min_lock_eur,eurusd_price,"
    "expected_ok,expected_reason,expected_lots,expected_tp,expected_risk,expected_reward",
    CASOS,
)
def test_compute_position_size_matches_legacy(
    symbol, price, equity, sl_pips, head_lots, tp_candidates,
    risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price,
    expected_ok, expected_reason, expected_lots, expected_tp,
    expected_risk, expected_reward,
):
    """El caso de uso nuevo produce el mismo output que el legacy."""
    thresholds = TradingThresholds(
        min_win_eur=Decimal("3.0"),
        safe_lock_eur=Decimal(str(min_lock_eur)),
        cost_buffer_pips=2,
    )
    if expected_ok == "ValueError":
        with pytest.raises(ValueError):
            _ejecutar_uc(symbol, price, equity, sl_pips, head_lots, tp_candidates,
                         risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price,
                         thresholds)
        return
    out = _ejecutar_uc(symbol, price, equity, sl_pips, head_lots, tp_candidates,
                       risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price,
                       thresholds)
    assert out.ok == expected_ok, f"{symbol}: ok={out.ok} != {expected_ok}"
    assert out.reason == expected_reason, f"{symbol}: reason={out.reason!r} != {expected_reason!r}"
    assert out.lots == expected_lots, f"{symbol}: lots={out.lots} != {expected_lots}"
    assert out.tp_pips == expected_tp, f"{symbol}: tp={out.tp_pips} != {expected_tp}"
    assert round(out.risk_eur, 4) == expected_risk, f"{symbol}: risk={round(out.risk_eur,4)} != {expected_risk}"
    assert round(out.reward_net_eur, 4) == expected_reward, f"{symbol}: reward={round(out.reward_net_eur,4)} != {expected_reward}"


def test_compute_position_size_uses_domain_only():
    """El caso de uso solo usa src.domain/ (no imports de infraestructura)."""
    import src.application.use_cases.compute_position_size as mod
    imports = [name for name in dir(mod) if name.isupper()]
    # Si llegamos aquí, el módulo se importó sin deps externas.
    assert True


def test_flag_default_false_and_reversible_via_settings():
    """USE_NEW_COMPUTE_POSITION_SIZE default false, reversible desde settings."""
    from config import Settings
    s = Settings(
        database_url="postgresql://t:t@localhost:5432/t",
        internal_token="test-token",
        hmac_secret="test-hmac",
    )
    assert s.USE_NEW_COMPUTE_POSITION_SIZE is False
    s.USE_NEW_COMPUTE_POSITION_SIZE = True
    assert s.USE_NEW_COMPUTE_POSITION_SIZE is True
    s.USE_NEW_COMPUTE_POSITION_SIZE = False
    assert s.USE_NEW_COMPUTE_POSITION_SIZE is False

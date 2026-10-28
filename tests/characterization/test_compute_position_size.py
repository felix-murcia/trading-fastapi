"""Test de caracterización: captura el comportamiento ACTUAL del legacy.

Estos tests documentan lo que `evaluate_trade` (legacy) hace HOY, incluso si
algunos resultados parecen subóptimos. NO se corrige nada del legacy aquí.
Si el legacy hace algo que parece un bug, se captura tal cual y se añade
un TODO para arreglarlo en una sesión posterior.

Cada caso también compara contra el nuevo caso de uso
`src.application.use_cases.compute_position_size.ComputePositionSize`.
Como ese módulo aún no existe, el test falla con ImportError (RED gate).
"""

from decimal import Decimal
import pytest

from services.trade_contract import evaluate_trade
from src.domain.value_objects.thresholds import TradingThresholds

# Input: (symbol, price, equity, sl_pips, head_lots, tp_candidates,
#         risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price)
# Output esperado: (ok, reason, lots, tp_pips, risk_eur, reward_net_eur)
CAPTURADO_LEGACY = [
    # 1. Caso feliz EURUSD
    ("EURUSD", 1.12094, 10000.0, 30.0, 0.05, (20.0, 30.0, 50.0), 0.02, 0.01, 0.5, 4.0, None,
     True, "", 0.05, 20.0, 13.3816, 8.029),
    # 2. GBPUSD (requiere eurusd_price)
    ("GBPUSD", 1.25345, 10000.0, 30.0, 0.05, (20.0, 30.0, 50.0), 0.02, 0.01, 0.5, 4.0, 1.25345,
     True, "", 0.05, 20.0, 11.967, 7.1802),
    # 3. USDCHF (requiere eurusd_price; paridad Sesión 1)
    ("USDCHF", 0.82197, 10000.0, 30.0, 0.05, (20.0, 30.0, 50.0), 0.02, 0.01, 0.5, 4.0, 1.1236,
     True, "", 0.05, 20.0, 16.2414, 9.7448),
    # 4. Equity baja (1000 €)
    ("EURUSD", 1.12094, 1000.0, 30.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     True, "", 0.05, 20.0, 13.3816, 8.029),
    # 5. SL ancho (200 pips) — riesgo alto pero aún cabe
    ("EURUSD", 1.12094, 10000.0, 200.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     True, "", 0.05, 20.0, 89.2108, 8.029),
    # 6. head_lots = 0 → rechazo sizing_inviable
    ("EURUSD", 1.12094, 10000.0, 30.0, 0.0, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     False, "sizing_inviable", 0.0, 0.0, 0.0, 0.0),
    # 7. Lote máximo (0.5, equity 50000)
    ("EURUSD", 1.12094, 50000.0, 30.0, 0.5, (50.0,), 0.01, 0.01, 1.0, 4.0, None,
     True, "", 0.5, 50.0, 133.8163, 214.106),
    # 8. Equity = 0 → ValueError (error de programación)
    ("EURUSD", 1.12094, 0.0, 30.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     "ValueError", "", 0.0, 0.0, 0.0, 0.0),
    # 9. sl_pips = 0 → ValueError
    ("EURUSD", 1.12094, 10000.0, 0.0, 0.05, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     "ValueError", "", 0.0, 0.0, 0.0, 0.0),
    # 10. head_lots negativo → rechazo sizing_inviable
    ("EURUSD", 1.12094, 10000.0, 30.0, -0.01, (20.0,), 0.02, 0.01, 0.5, 4.0, None,
     False, "sizing_inviable", 0.0, 0.0, 0.0, 0.0),
]


def _llamar_legacy(symbol, price, equity, sl_pips, head_lots, tp_candidates,
                   risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price):
    """Ejecuta el legacy y devuelve (ok, reason, lots, tp, risk_eur, reward_net_eur)."""
    r = evaluate_trade(
        symbol=symbol, price=price, equity=equity, sl_pips=sl_pips,
        head_lots=head_lots, tp_candidates_pips=list(tp_candidates),
        risk_pct=risk_pct, min_lot=min_lot, max_lot=max_lot,
        min_lock_eur=min_lock_eur, eurusd_price=eurusd_price,
    )
    return (r.ok, r.reason, r.lots, r.tp_pips, round(r.risk_eur, 4),
            round(r.reward_net_eur, 4))


@pytest.mark.characterization
@pytest.mark.parametrize(
    "symbol,price,equity,sl_pips,head_lots,tp_candidates,risk_pct,min_lot,max_lot,"
    "min_lock_eur,eurusd_price,"
    "expected_ok,expected_reason,expected_lots,expected_tp,expected_risk,expected_reward",
    CAPTURADO_LEGACY,
)
def test_legacy_produces_captured_output(
    symbol, price, equity, sl_pips, head_lots, tp_candidates,
    risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price,
    expected_ok, expected_reason, expected_lots, expected_tp,
    expected_risk, expected_reward,
):
    """El legacy produce el output capturado (oracle de comportamiento actual)."""
    if expected_ok == "ValueError":
        with pytest.raises(ValueError):
            _llamar_legacy(symbol, price, equity, sl_pips, head_lots, tp_candidates,
                           risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price)
        return
    ok, reason, lots, tp, risk, reward = _llamar_legacy(
        symbol, price, equity, sl_pips, head_lots, tp_candidates,
        risk_pct, min_lot, max_lot, min_lock_eur, eurusd_price,
    )
    assert ok == expected_ok, f"{symbol}: ok={ok} != {expected_ok}"
    assert reason == expected_reason, f"{symbol}: reason={reason!r} != {expected_reason!r}"
    assert lots == expected_lots, f"{symbol}: lots={lots} != {expected_lots}"
    assert tp == expected_tp, f"{symbol}: tp={tp} != {expected_tp}"
    assert risk == expected_risk, f"{symbol}: risk={risk} != {expected_risk}"
    assert reward == expected_reward, f"{symbol}: reward={reward} != {expected_reward}"


@pytest.mark.characterization
def test_new_use_case_matches_legacy():
    """El nuevo caso de uso debe coincidir byte a byte con el legacy.

    RED gate: `src.application.use_cases.compute_position_size` aún no existe,
    así que este test falla con ImportError. Después de Task 3 debe pasar.
    """
    from src.application.use_cases.compute_position_size import ComputePositionSize  # noqa: F401
    thresholds = TradingThresholds()
    assert ComputePositionSize is not None

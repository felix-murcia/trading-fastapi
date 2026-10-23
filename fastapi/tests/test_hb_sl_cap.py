"""H-B (2026-10-23): tope de volatilidad del SL — 1,5×ATR.

Contexto medido (30.000 velas H1 × EURUSD/USDCHF, 12 velas de horizonte,
motor real):

  · orden 89 (EURUSD SELL): SL 29,9 p = 3,9×ATR, TP 59,8 p = 7,8×ATR,
    lote 0,03 — el tope de riesgo 10,32 € sólo permitía 0,03 lotes;
  · orden 88 (USDCHF BUY): SL 29,0 p = 2,5×ATR, TP 26,6 p, lote 0,03.

El tope 1,5×ATR deja el stop efectivo (geometría + buffer del EA) en
1,5×ATR físico. La medición agregada: E[R] −0,069 → −0,077 (Δ −0,008 R,
borde del ruido ya documentado en H-N), señales +3,5 %, lotes ≥0,05 de
25 % a 35 %. No se vende como mejora de edge: es corrección de la forma
del riesgo.

Estos tests fijan la física (ATR en pips, no la unidad relativa) y los dos
casos vivos. Antes del cambio fallan: el SL geométrico no tiene tope.
"""
import pytest

from ml.trading_env_v2 import (
    SL_MAX_ATR_MULT,
    SPREAD_BUFFER_PIPS,
    effective_sl_pips,
    production_sl,
    sl_cap_geom_pips,
)
from services.trade_contract import evaluate_trade

PIP = 0.0001

# ATR del ciclo medidos en la vela de la señal (pips físicos).
ATR_EURUSD = 7.7
ATR_USDCHF = 11.4
PRICE_EURUSD = 1.11907
PRICE_USDCHF = 0.82178
EQUITY = 508.94
RISK_PCT = 0.02
CAP_EUR = EQUITY * RISK_PCT


def _atr_rel(atr_pips: float, price: float) -> float:
    """ATR relativo (TR/close) que produce `atr_pips` a ese precio."""
    return atr_pips * PIP / price


class TestSlCapPhysics:
    def test_far_ceiling_is_capped_to_one_and_a_half_atr(self):
        """Orden 89: techo a 25,5 p (2,9×ATR) ⇒ el stop efectivo ≤ 1,5×ATR."""
        sl_geom = production_sl(
            _atr_rel(ATR_EURUSD, PRICE_EURUSD),
            PIP,
            direction=-1,
            floor_below=1.11806,
            ceiling_above=1.12162,
            current_price=PRICE_EURUSD,
        )
        sl = effective_sl_pips(sl_geom)
        # El suelo del sistema es 10 p geométricos ⇒ 12 p efectivos; con
        # ATR 7,7 el tope teórico (11,55) queda por debajo del suelo.
        assert sl <= max(1.5 * ATR_EURUSD, 12.0) + 1e-6
        assert sl < 20.0, f"SL {sl:.1f} p sigue siendo inalcanzable"
        assert sl_geom == pytest.approx(sl - SPREAD_BUFFER_PIPS)

    def test_near_ceiling_is_not_touched(self):
        """Un techo cercano manda sobre el tope: no se aleja el stop."""
        sl_geom = production_sl(
            0.0010, PIP, direction=-1,
            floor_below=1.0980, ceiling_above=1.1008,
            current_price=1.1000,
        )
        assert sl_geom == pytest.approx(10.0)  # 8 p + buffer 2, suelo 10
        assert effective_sl_pips(sl_geom) == pytest.approx(12.0)

    def test_cap_uses_physical_atr_not_the_relative_unit(self):
        """El mismo ATR relativo a precios distintos NO da el mismo tope.

        `atr_norm` es TR/close; el ATR físico en pips es atr_norm×precio/pip.
        Si se usara atr_norm/pip (la unidad antigua), USDCHF saldría un 22 %
        más ancho y EURUSD un 12 % más estrecho.
        """
        atr_rel = 0.0010
        sl_eur = production_sl(
            atr_rel, PIP, direction=1,
            floor_below=0.9000, ceiling_above=1.3000, current_price=1.1000,
        )
        sl_chf = production_sl(
            atr_rel, PIP, direction=1,
            floor_below=0.7000, ceiling_above=0.9500, current_price=0.8200,
        )
        # topes efectivos: 1,5 × (0,0010×precio/pip)
        assert effective_sl_pips(sl_eur) == pytest.approx(1.5 * 11.0)
        assert effective_sl_pips(sl_chf) == pytest.approx(1.5 * 8.2)

    def test_cap_is_inactive_without_price(self):
        """Compatibilidad: sin precio no se puede medir el ATR físico.

        Las llamadas históricas de `production_stops(atr, pip)` (tests y
        firmware) conservan la fórmula anterior.
        """
        assert production_sl(0.0010, PIP) == pytest.approx(15.0)
        assert sl_cap_geom_pips(0.0010, PIP, None) is None


class TestLiveOrderRegression:
    def test_order_89_now_fits_six_lots_hundredths_and_reachable_tp(self):
        sl_geom = production_sl(
            _atr_rel(ATR_EURUSD, PRICE_EURUSD),
            PIP,
            direction=-1,
            floor_below=1.11806,
            ceiling_above=1.12162,
            current_price=PRICE_EURUSD,
        )
        sl = effective_sl_pips(sl_geom)
        c = evaluate_trade(
            symbol="EURUSD",
            price=PRICE_EURUSD,
            equity=EQUITY,
            sl_pips=sl,
            head_lots=0.06,
            tp_candidates_pips=(2.0 * sl,),
            risk_pct=RISK_PCT,
            min_lot=0.01,
            max_lot=0.50,
            min_lock_eur=4.0,
        )
        assert c.ok, c.reason
        assert c.lots == pytest.approx(0.06, abs=0.011)
        assert c.risk_eur <= CAP_EUR + 1e-6
        assert c.tp_pips <= 3.5 * ATR_EURUSD, "el TP debe dejar de estar a 7,8×ATR"

    def test_order_88_now_fits_at_least_five_lots_hundredths(self):
        sl_geom = production_sl(
            _atr_rel(ATR_USDCHF, PRICE_USDCHF),
            PIP,
            direction=1,
            floor_below=0.81888,
            ceiling_above=0.82444,
            current_price=PRICE_USDCHF,
        )
        sl = effective_sl_pips(sl_geom)
        c = evaluate_trade(
            symbol="USDCHF",
            price=PRICE_USDCHF,
            equity=EQUITY,
            sl_pips=sl,
            head_lots=0.06,
            tp_candidates_pips=(26.6,),
            risk_pct=RISK_PCT,
            min_lot=0.01,
            max_lot=0.50,
            min_lock_eur=4.0,
            eurusd_price=PRICE_EURUSD,
        )
        assert c.ok, c.reason
        assert c.lots >= 0.05, f"lote {c.lots} sigue por debajo de ~0,06"
        assert c.risk_eur <= CAP_EUR + 1e-6
        assert c.tp_pips == pytest.approx(26.6)

    def test_cap_constant_is_the_measured_one(self):
        """El contrato de dinero es explícito: 1,5×ATR (H-B, 2026-10-23)."""
        assert SL_MAX_ATR_MULT == 1.5

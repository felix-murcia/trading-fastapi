"""
P1-4: tests donde hay dinero — Position Sizing (SL/TP/volumen reales).

Cubren las tres funciones de cálculo con valores numéricos verificados de forma
independiente (fórmula documentada en el módulo) y las salvaguardas:
- pip_value por símbolo (majors, base=USD, oro).
- Suelo de SL (sl_min_pips / sl_min_spread_mult) cuando el ancla está muy cerca.
- Techo de asequibilidad (sl_risk_usd a la mayor escala min_volume) cuando el
  ancla está muy lejos → el riesgo NUNCA supera sl_risk_usd por sl amplio.
- Volumen = riesgo / (sl_pips × pip_value), acotado a [min_volume, max_volume].
- Dirección: buy → sl < entry < tp; sell → tp < entry < sl.
- Símbolo no soportado → KeyError (fail-loud, sin fallback silencioso).

P2-2 (sizing del PPO delegado):
- `size_ppo_position`: riesgo fraccional por equity modulado por la cabeza
  `volume`, floor al paso de lote (nunca excede el tope) y 0.0 sin entrada
  cuando ni el lote mínimo cabe en el riesgo.
- PIP centralizado: order_validator importa EL MISMO dict (fuente única).
"""
import math

import pytest

from config import settings
from core.rules import MIN_WIN_EUR, is_winning_close
from services import position_sizing as ps


@pytest.fixture(autouse=True)
def pinned_sizing_settings(monkeypatch):
    """Fija la config de sizing a los defaults canónicos.

    El `.env` de producción tunea SL_MULT/RR_MIN/SL_MIN_PIPS (p. ej. 1.2/2.5/8.0),
    lo que cambia el suelo de SL y el volumen: estos tests verifican la FÓRMULA
    con valores numéricos conocidos, así que deben ser idénticos en host y
    contenedor — la reacción a la config de despliegue se testea aparte.
    """
    for attr, value in {
        "sl_risk_usd": 15.0,
        "sl_risk_usd_xauusd": 15.0,
        "sl_risk_usd_xaueur": 15.0,
        "sl_mult": 1.5,
        "sl_pct": 0.001,
        "rr_min": 1.0,
        "rr_min_xau": 1.2,
        "sl_min_spread_mult": 3,
        "sl_min_pips_default": 3.0,
        "sl_min_pips_usdjpy": 5.0,
        "sl_min_pips_xaueur": 10.0,
        "sl_min_pips_xauusd": 10.0,
        "min_volume": 0.01,
        "max_volume": 0.50,
        "ppo_risk_pct": 0.03,
    }.items():
        monkeypatch.setattr(settings, attr, value)


class TestPipValue:
    def test_quote_usd_majors_are_flat(self):
        assert ps.pip_value_per_lot("EURUSD", 1.10) == 10.0
        assert ps.pip_value_per_lot("GBPUSD", 1.25) == 10.0
        assert ps.pip_value_per_lot("XAUUSD", 2000.0) == 10.0

    def test_usd_base_scales_with_price(self):
        assert ps.pip_value_per_lot("USDJPY", 150.0) == pytest.approx(1000.0 / 150.0)
        assert ps.pip_value_per_lot("USDCAD", 1.35) == pytest.approx(10.0 / 1.35)

    def test_xaueur_applies_eur_cross(self):
        assert ps.pip_value_per_lot("XAUEUR", 1900.0) == 10.90


class TestSupportedSymbols:
    def test_known_symbols_supported(self):
        for symbol in ("EURUSD", "USDJPY", "XAUUSD", "XAUEUR"):
            assert ps.is_supported(symbol) is True

    def test_unknown_symbol_not_supported(self):
        assert ps.is_supported("BTCUSD") is False

    def test_derive_with_unknown_symbol_fails_loud(self):
        with pytest.raises(KeyError):
            ps.derive_order_from_bb("buy", "EURJPY", 160.0, 159.9, 160.2, 0.02)


class TestDeriveOrderFromBollinger:
    def test_buy_numeric_case(self):
        # EURUSD entry=1.1000, anclas ±50 pips, spread 1.5 pips
        entry, sl, tp, volume = ps.derive_order_from_bb(
            "buy", "EURUSD", 1.1000, 1.0950, 1.1100, 0.00015,
        )
        pip = 0.0001
        # base_dist=0.005 (50 pips) > floor max(3×pip, 3×spread)=0.00045 → sin suelo
        sl_dist = round(0.005 * settings.sl_mult, 5)      # 1.5× → 0.0075
        sl_pips = sl_dist / pip                            # 75
        risk_usd = settings.sl_risk_usd                    # 15
        expected_volume = max(
            settings.min_volume,
            min(settings.max_volume, round(risk_usd / (sl_pips * 10.0), 2)),
        )
        assert entry == 1.1000
        assert sl == pytest.approx(1.1000 - sl_dist, abs=1e-9)
        # TP sale DIRECTO del ancla de la banda superior (no de rr)
        assert tp == pytest.approx(1.1100, abs=1e-9)
        assert volume == expected_volume
        # Invariantes direccionales y de riesgo
        assert sl < entry < tp
        assert settings.min_volume <= volume <= settings.max_volume
        # Cota teórica del redondeo de volumen a 2 decimales (±0.005 lote)
        assert volume * sl_pips * 10.0 <= settings.sl_risk_usd + 0.005 * sl_pips * 10.0 + 1e-9
        assert volume * sl_pips * 10.0 >= settings.sl_risk_usd - 0.005 * sl_pips * 10.0 - 1e-9

    def test_sell_inverts_sl_and_tp(self):
        entry, sl, tp, volume = ps.derive_order_from_bb(
            "sell", "EURUSD", 1.1000, 1.1050, 1.0900, 0.00015,
        )
        assert sl > entry > tp
        sl_dist = round(0.005 * settings.sl_mult, 5)
        assert sl == pytest.approx(1.1000 + sl_dist, abs=1e-9)
        assert tp == pytest.approx(1.0900, abs=1e-9)   # ancla de la banda inferior
        assert volume > 0

    def test_sl_floor_applies_when_anchor_too_close(self):
        # Ancla a 0.5 pips → el suelo (sl_min_pips / spread) la eleva
        entry, sl, tp, volume = ps.derive_order_from_bb(
            "buy", "EURUSD", 1.1000, 1.09995, 1.1050, 0.00015,
        )
        pip = 0.0001
        sl_pips = abs(entry - sl) / pip
        assert sl_pips >= settings.sl_min_pips_default
        assert abs(entry - sl) >= settings.sl_min_spread_mult * 0.00015 - 1e-9
        assert volume >= settings.min_volume

    def test_affordability_cap_limits_risk(self):
        # Ancla 2000 pips lejos: sin tope el riesgo sería ~300× sl_risk_usd
        entry, sl, tp, volume = ps.derive_order_from_bb(
            "buy", "EURUSD", 1.1000, 0.9000, 1.1100, 0.00015,
        )
        pip = 0.0001
        ppv = ps.pip_value_per_lot("EURUSD", entry)
        sl_pips = abs(entry - sl) / pip
        max_affordable = settings.sl_risk_usd / (settings.min_volume * ppv)
        assert sl_pips == pytest.approx(max_affordable)          # cap exacto
        assert volume == settings.min_volume                     # volumen al mínimo
        assert volume * sl_pips * ppv <= settings.sl_risk_usd + 1e-9  # riesgo ≤ tope

    def test_longer_sl_reduces_volume_for_same_risk(self):
        _, _, _, vol_near = ps.derive_order_from_bb(
            "buy", "EURUSD", 1.1000, 1.0975, 1.1100, 0.00015)   # 25 pips
        _, _, _, vol_far = ps.derive_order_from_bb(
            "buy", "EURUSD", 1.1000, 1.0950, 1.1100, 0.00015)    # 50 pips
        assert vol_far < vol_near


class TestDeriveOrderFromCandleOpen:
    def test_numeric_case_with_rr(self):
        entry, sl, tp, volume = ps.derive_order_from_candle_open(
            "buy", "EURUSD", 1.1000, 1.0995, 0.00015,
        )
        pip = 0.0001
        sl_dist = round(0.0005 * settings.sl_mult, 5)     # 1.5× de 5 pips
        sl_pips = sl_dist / pip                            # 7.5
        expected_volume = max(
            settings.min_volume,
            min(settings.max_volume, round(settings.sl_risk_usd / (sl_pips * 10.0), 2)),
        )
        assert sl == pytest.approx(1.1000 - sl_dist, abs=1e-9)
        assert volume == expected_volume
        if settings.rr_min > 0:
            assert tp == pytest.approx(1.1000 + round(sl_dist * settings.rr_min, 5), abs=1e-9)
        assert sl < entry < tp
        # Riesgo real ≤ presupuesto (tolerancia de redondeo a 2 decimales)
        assert volume * sl_pips * 10.0 <= settings.sl_risk_usd + 0.005 * sl_pips * 10.0 + 1e-9


class TestDeriveOrderSimple:
    def test_numeric_case(self):
        entry, sl, tp, volume = ps.derive_order("buy", "EURUSD", 1.10)
        sl_dist = 1.10 * settings.sl_pct            # 0.1% → 0.0011
        sl_pips = sl_dist / 0.0001                  # 11
        expected_volume = max(
            settings.min_volume,
            round(settings.sl_risk_usd / (sl_pips * 10.0), 2),
        )
        assert entry == 1.10
        assert sl == pytest.approx(1.10 - sl_dist, abs=1e-9)
        assert tp == pytest.approx(1.10 + round(sl_dist * settings.rr_min, 5), abs=1e-9)
        assert volume == expected_volume
        assert volume >= settings.min_volume

    def test_sell_direction(self):
        _, sl, tp, _ = ps.derive_order("sell", "EURUSD", 1.10)
        assert sl > 1.10 > tp


class TestSizePPOPosition:
    """P2-2: sizing del PPO — riesgo fraccional por equity (fórmula verificada).

    lots = equity × risk_pct × clip(volume_norm, 0, 1) / (sl_pips × pip_value),
    floor al paso de lote; 0.0 = sin entrada.
    """

    @staticmethod
    def _size(v=1.0, eq=1000.0, sl=15.0, ppv=10.0, risk=0.03, mx=0.5, mn=0.01):
        return ps.size_ppo_position(
            volume_norm=v, sl_pips=sl, equity=eq, pip_value=ppv,
            risk_pct=risk, max_lot=mx, min_lot=mn,
        )

    def test_formula_full_risk(self):
        # 1000 × 3% × 1.0 / (15 × 10) = 0.20 lots
        assert self._size() == pytest.approx(0.20)

    def test_volume_head_modulates_risk_linearly(self):
        # 1000 × 3% × 0.4 / (15 × 10) = 0.08 lots (la cabeza VIVE)
        assert self._size(v=0.4) == pytest.approx(0.08)
        assert self._size(v=0.25) == pytest.approx(0.05)
        assert self._size(v=0.0) == 0.0            # abstención explícita

    def test_volume_norm_clipped_to_unit_range(self):
        assert self._size(v=-0.5) == 0.0           # nunca negativo
        assert self._size(v=1.7) == self._size(v=1.0)   # saturación en 1.0

    def test_risk_pct_is_linear(self):
        # Valores que caen EXACTOS en el paso de 0.01 (sin pérdida por floor):
        # 1000 × 1.5% / 150 = 0.10 y 1000 × 3% / 150 = 0.20 → doble riesgo = doble lote
        assert self._size(risk=0.015) == pytest.approx(0.10)
        assert self._size(risk=0.03) == pytest.approx(0.20)
        # y entre pasos el floor limita por debajo (crudo 0.0667 → 0.06)
        assert self._size(risk=0.01) == pytest.approx(0.06)

    def test_floor_never_exceeds_risk_budget(self):
        # eq=1004 → riesgo 30.12 → crudo 0.2008 → floor 0.20 (no 0.21)
        lots = self._size(eq=1004.0)
        assert lots == pytest.approx(0.20)
        assert lots * 15.0 * 10.0 <= 1004.0 * 0.03 + 1e-9   # riesgo real ≤ presupuesto

    def test_float_boundary_rounds_up_to_exact_step(self):
        # 24/160 = 0.15; sin epsilon, 0.15/0.01 = 14.999… → 0.14 (pierde un paso)
        assert self._size(eq=800.0, sl=16.0) == pytest.approx(0.15)

    def test_below_min_lot_returns_zero_no_entry(self):
        # eq=5 → 0.15 € de riesgo → 0.001 lots → ni el lote mínimo cabe
        assert self._size(eq=5.0) == 0.0

    def test_max_lot_cap(self):
        assert self._size(eq=1e7) == pytest.approx(0.5)

    def test_custom_min_lot_step(self):
        # paso 0.10: 0.08 → floor → 0.00 (debajo del mínimo) → sin entrada
        assert self._size(v=0.4, mn=0.10) == 0.0
        assert self._size(v=1.0, mn=0.10) == pytest.approx(0.2)

    def test_usd_base_pip_value(self):
        # USDJPY@150 → ppv = 1000/150; 1000 × 3% / (30 × 6.667) = 0.15
        ppv = ps.pip_value_per_lot("USDJPY", 150.0)
        assert self._size(sl=30.0, ppv=ppv) == pytest.approx(0.15)

    def test_guards_return_zero(self):
        assert self._size(eq=0.0) == 0.0
        assert self._size(eq=-500.0) == 0.0
        assert self._size(sl=0.0) == 0.0
        assert self._size(ppv=0.0) == 0.0
        assert self._size(risk=0.0) == 0.0


class TestPipSingleSource:
    """P2-2: pip por símbolo centralizado — una sola copia en el sistema."""

    def test_order_validator_imports_the_same_dict_object(self):
        from services import order_validator
        assert order_validator.PIP_SIZE is ps.PIP_SIZE

    def test_gold_pip_is_consistent(self):
        # La auditoría detectó 0.01 (heurística de dígitos de /predict) vs 0.10
        assert ps.PIP_SIZE["XAUUSD"] == 0.10
        assert ps.PIP_SIZE["XAUEUR"] == 0.10

    def test_symbols_merged_from_order_validator(self):
        assert ps.PIP_SIZE["USDCNH"] == 0.0001
        assert ps.PIP_SIZE["USDSEK"] == 0.0001
        # y su pip_value usa la regla base=USD (antes no existían aquí)
        assert ps.pip_value_per_lot("USDCNH", 7.80) == pytest.approx(10.0 / 7.80)
        assert ps.pip_value_per_lot("USDSEK", 10.50) == pytest.approx(10.0 / 10.50)


class TestExpectedCloseEur:
    """Opción A (Félix 2026-10-12): gate de recompensa mínima — cálculo del
    beneficio que produciría un cierre a TP, en la divisa de la cuenta
    (EUR), con paridad exacta al deal del bróker (verificado contra el
    trade #255: 21.6 p × 0.01 lot a 1.12094 → 1.93 € pagados)."""

    def test_eurusd_matches_broker_deal_255(self):
        assert ps.expected_close_eur("EURUSD", 21.6, 0.01, 1.12094) == pytest.approx(1.93, abs=0.01)

    def test_boundary_exact_min_win_is_not_blocked(self):
        # Frontera exacta del gate: 30 pips × 0.01 lot a price 1.0 = 3.00 €
        # = MIN_WIN_EUR. Paridad con la regla 3€ (is_winning_close(3.0) es
        # WIN) → el gate deja pasar el empate: comparación estricta `<`.
        reward = ps.expected_close_eur("EURUSD", 30.0, 0.01, 1.0)
        assert reward == pytest.approx(MIN_WIN_EUR)
        assert is_winning_close(reward)
        assert not (reward < MIN_WIN_EUR)

    def test_below_min_win_is_blocked(self):
        assert ps.expected_close_eur("EURUSD", 29.9, 0.01, 1.0) < MIN_WIN_EUR

    def test_non_eurusd_raises_no_invented_rate(self):
        # Sin tasa cross definible desde el par → ValueError: el gate no
        # aplica donde la conversión no es honesta (nada de tasas inventadas).
        with pytest.raises(ValueError):
            ps.expected_close_eur("GBPUSD", 30.0, 0.01, 1.27)

    @pytest.mark.parametrize(
        "pips,volume,price",
        [(0.0, 0.01, 1.10), (30.0, 0.0, 1.10), (30.0, 0.01, 0.0), (-5.0, 0.01, 1.10)],
    )
    def test_non_positive_inputs_raise(self, pips, volume, price):
        with pytest.raises(ValueError):
            ps.expected_close_eur("EURUSD", pips, volume, price)

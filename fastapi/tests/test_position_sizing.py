"""Tests donde hay dinero: pip por símbolo, conversión a EUR y sizing.

Cubren las funciones con valores numéricos verificados de forma independiente
(fórmula documentada en el módulo) y las salvaguardas:
- pip_value por símbolo (majors, base=USD, oro).
- `size_ppo_position`: riesgo fraccional por equity modulado por la cabeza
  `volume`, floor al paso de lote (nunca excede el tope) y 0.0 sin entrada
  cuando ni el lote mínimo cabe en el riesgo.
- PIP centralizado: order_validator importa EL MISMO dict (fuente única).
- `expected_close_eur`: paridad con el deal del bróker y fallo ruidoso sin tasa.
"""
import pytest
from unittest.mock import AsyncMock, patch

from core import rules
from core.rules import MIN_WIN_EUR, is_winning_close
from services import position_sizing as ps


class TestPipValue:
    def test_quote_usd_majors_are_flat(self):
        assert ps.pip_value_per_lot("EURUSD", 1.10) == 10.0
        assert ps.pip_value_per_lot("GBPUSD", 1.25) == 10.0
        assert ps.pip_value_per_lot("XAUUSD", 2000.0) == 10.0

    def test_symbol_sin_regla_de_pip_falla_ruidoso(self):
        # XAUEUR se retiró del sistema (2026-10-22): no hay regla de pip
        # inventada; el pip de un símbolo desconocido es un error, no 10.0.
        assert not ps.is_supported("XAUEUR")
        with pytest.raises(ValueError, match="sin regla de pip"):
            ps.pip_value_per_lot("XAUEUR", 1900.0)

    def test_usd_base_scales_with_price(self):
        assert ps.pip_value_per_lot("USDJPY", 150.0) == pytest.approx(1000.0 / 150.0)
        assert ps.pip_value_per_lot("USDCAD", 1.35) == pytest.approx(10.0 / 1.35)


class TestSupportedSymbols:
    def test_known_symbols_supported(self):
        for symbol in ("EURUSD", "USDJPY", "XAUUSD"):
            assert ps.is_supported(symbol) is True

    def test_unknown_symbol_not_supported(self):
        assert ps.is_supported("BTCUSD") is False


class TestSizePPOPosition:
    """P2-2: sizing del PPO — riesgo fraccional por equity (fórmula verificada).

    lots = equity × risk_pct × clip(volume_norm, 0, 1) / (sl_pips × pip_value),
    floor al paso de lote; 0.0 = sin entrada.
    """

    @staticmethod
    def _size(v=1.0, eq=1000.0, sl=15.0, ppv=10.0, risk=0.03, mx=0.5, mn=0.01):
        return rules.size_ppo_position(
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


# ─── B4.2: desacoplamiento de broker ──────────────────────────────────────────

class TestB42NoMt5ClientDirectImport:
    """position_sizing.py no importa services.mt5_client directamente."""

    def test_no_module_level_import_of_mt5_client(self):
        """El módulo position_sizing no hace `from services import mt5_client` ni `from services.mt5_client import`."""
        import services.position_sizing as ps_mod
        source = open(ps_mod.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source

    @pytest.mark.asyncio
    async def test_eurusd_price_uses_injected_market_port(self):
        """eurusd_price usa el market_port inyectado para get_candles."""
        from core.adapters import MT5MarketDataAdapter
        mp = MT5MarketDataAdapter()
        with patch("services.mt5_client.get_candles",
                   AsyncMock(return_value=[{"close": 1.33}])):
            price = await ps.eurusd_price(market_port=mp)
        assert price == pytest.approx(1.33)

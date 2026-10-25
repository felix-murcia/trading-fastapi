"""
G2-5: riesgo agregado multi-par (orden Félix 2026-10-17 — D2+D3, canarios 0.09/4).

Cobertura (aceptación del diseño §«G2-5 · DISEÑO DE RIESGO AGREGADO MULTI-PAR»):
- (a) cupo total de cuenta ya cubierto en test_p24_risk_state.py::TestAggregateExposure.
- (b) MAX_TOTAL_RISK: riesgo-1R agregado (EUR) >= equity × max_total_risk_pct
      → bloqueo; por debajo → pasa.
- (c) fail-closed: posición sin SL → contada al máximo teórico + WARNING;
      precio EURUSD inaccesible → RISK_RATE_UNAVAILABLE (bloqueo explícito).
- (d) SAME_DIR_LIMIT: posiciones misma-dirección en cuenta >= tope → bloqueo.
- (e) defaults off (None/0) → el comportamiento previo es byte-idéntico.
- (f) anti-ramas: cero comparaciones `symbol == "…"` nuevas en los módulos
      (el fetch de EURUSD para el rate es un DATO de mercado, no una rama
      de mundo).

Convenión B1 del rate: USD→EUR dividiendo por el precio EURUSD (lo que hace
el bróker con el P/L flotante).
"""
import logging
from unittest.mock import AsyncMock, patch

import pytest

import services.risk_guardian as rg
from services.risk_guardian import RiskGuardian
from services.order_manager import _total_risk_eur
from core.adapters import MT5MarketDataAdapter


@pytest.fixture
def fresh(monkeypatch):
    """Guardianes/cuenta limpios + canarios off (aislamiento entre tests)."""
    monkeypatch.setattr(rg, "_GUARDIANS", {})
    monkeypatch.setattr(rg, "_ACCOUNT", rg.AccountRiskState())
    monkeypatch.setattr(rg.settings, "max_total_risk_pct", None)
    monkeypatch.setattr(rg.settings, "max_total_same_dir_positions", 0)
    yield


def _guardian(max_positions: int = 5) -> RiskGuardian:
    g = RiskGuardian(max_positions=max_positions, daily_loss_pct=50.0)
    g.set_daily_start(1000.0)
    return g


class TestSameDirLimitD3:
    def test_blocks_at_limit(self, fresh, monkeypatch):
        monkeypatch.setattr(rg.settings, "max_total_same_dir_positions", 4)
        g = _guardian()
        ok, reason = g.can_open_position(
            1000.0, open_positions=0, total_open_positions=4,
            total_risk_eur=0.0, same_dir_open=4,
        )
        assert ok is False
        assert reason == "SAME_DIR_LIMIT 4 reached"

    def test_passes_below_limit(self, fresh, monkeypatch):
        monkeypatch.setattr(rg.settings, "max_total_same_dir_positions", 4)
        g = _guardian()
        ok, _ = g.can_open_position(
            1000.0, open_positions=0, total_open_positions=3,
            total_risk_eur=0.0, same_dir_open=3,
        )
        assert ok is True

    def test_default_off_ignores_count(self, fresh):
        g = _guardian()  # fresh fixture: max_total_same_dir_positions=0 (off)
        ok, _ = g.can_open_position(
            1000.0, open_positions=0, total_open_positions=3,
            total_risk_eur=0.0, same_dir_open=3,
        )
        assert ok is True


class TestTotalRiskCapD2:
    def test_blocks_at_budget(self, fresh, monkeypatch):
        monkeypatch.setattr(rg.settings, "max_total_risk_pct", 0.09)
        g = _guardian()
        # equity 1000 × 0.09 = 90 EUR de presupuesto; 91 en riesgo → bloquea
        ok, reason = g.can_open_position(
            1000.0, open_positions=0, total_open_positions=2,
            total_risk_eur=91.0, same_dir_open=1,
        )
        assert ok is False
        assert "MAX_TOTAL_RISK" in reason and "9.0%" in reason

    def test_passes_below_budget(self, fresh, monkeypatch):
        monkeypatch.setattr(rg.settings, "max_total_risk_pct", 0.09)
        g = _guardian()
        ok, _ = g.can_open_position(
            1000.0, open_positions=0, total_open_positions=2,
            total_risk_eur=45.0, same_dir_open=1,
        )
        assert ok is True

    def test_default_off_ignores_risk(self, fresh):
        g = _guardian()  # fresh fixture: max_total_risk_pct=None (off)
        ok, _ = g.can_open_position(
            1000.0, open_positions=0, total_open_positions=2,
            total_risk_eur=1e9, same_dir_open=0,
        )
        assert ok is True


class TestTotalRiskComputation:
    """_total_risk_eur: suma riesgo-1R de posiciones abiertas en EUR."""

    @pytest.mark.asyncio
    async def test_empty_positions_is_zero(self):
        risk, err = await _total_risk_eur([], 5000.0, MT5MarketDataAdapter())
        assert (risk, err) == (0.0, "")

    @pytest.mark.asyncio
    async def test_two_eurusd_longs_exact_eur(self, caplog):
        # 50 pips × 10 USD/pip/lote × 0.10 lot = 50 USD / 1.33 = 37.59 EUR c.u.
        pos = [
            {"symbol": "EURUSD", "volume": 0.10, "price": 1.3300, "sl": 1.3250},
            {"symbol": "EURUSD", "volume": 0.10, "price": 1.3300, "sl": 1.3250},
        ]
        with patch("services.mt5_client.get_candles",
                   AsyncMock(return_value=[{"close": 1.33}])):
            risk, err = await _total_risk_eur(pos, 455.0, MT5MarketDataAdapter())
        assert err == ""
        assert risk == pytest.approx(2 * 50.0 / 1.33, rel=1e-6)

    @pytest.mark.asyncio
    async def test_missing_sl_counts_worst_case_with_warning(self, caplog):
        pos = [{"symbol": "EURUSD", "volume": 0.50, "price": 1.3300, "sl": 0}]
        with caplog.at_level(logging.WARNING, logger="services.order_manager"):
            with patch("services.mt5_client.get_candles",
                       AsyncMock(return_value=[{"close": 1.33}])):
                risk, err = await _total_risk_eur(pos, 455.0, MT5MarketDataAdapter())
        assert err == ""
        # Fail-closed: contado al máximo teórico (equity × ppo_risk_pct)
        assert risk == pytest.approx(455.0 * rg.settings.ppo_risk_pct)
        assert any("máximo teórico" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unknown_symbol_counts_worst_case(self, caplog):
        pos = [{"symbol": "EXOTICXX", "volume": 0.10, "price": 2.0, "sl": 1.0}]
        with caplog.at_level(logging.WARNING, logger="services.order_manager"):
            with patch("services.mt5_client.get_candles",
                       AsyncMock(return_value=[{"close": 1.33}])):
                risk, err = await _total_risk_eur(pos, 500.0, MT5MarketDataAdapter())
        assert err == ""
        assert risk == pytest.approx(500.0 * rg.settings.ppo_risk_pct)

    @pytest.mark.asyncio
    async def test_rate_unavailable_blocks_explicitly(self, caplog):
        pos = [{"symbol": "EURUSD", "volume": 0.10, "price": 1.33, "sl": 1.32}]
        with patch("services.mt5_client.get_candles",
                   AsyncMock(side_effect=RuntimeError("feed caído"))):
            risk, err = await _total_risk_eur(pos, 455.0, MT5MarketDataAdapter())
        assert err == "RISK_RATE_UNAVAILABLE"  # no se mide ⇒ no se añade
        assert risk == 0.0

    @pytest.mark.asyncio
    async def test_gbpusd_uses_same_convention(self):
        # GBPUSD cotiza vs USD: pip value 10 USD/lote → misma fórmula,
        # el rate de conversión sigue siendo EURUSD (convenio B1).
        pos = [{"symbol": "GBPUSD", "volume": 0.10, "price": 1.3400, "sl": 1.3350}]
        with patch("services.mt5_client.get_candles",
                   AsyncMock(return_value=[{"close": 1.33}])):
            risk, err = await _total_risk_eur(pos, 455.0, MT5MarketDataAdapter())
        assert err == ""
        assert risk == pytest.approx(50.0 / 1.33, rel=1e-6)


class TestNoNewSymbolBranching:
    """(f) Anti-ramas: el riesgo agregado es config-driven, sin `symbol ==`."""

    def test_guardian_has_no_symbol_comparisons(self):
        from services import risk_guardian
        src = open(risk_guardian.__file__).read()
        assert '== "EURUSD"' not in src and "== 'EURUSD'" not in src
        assert '== "GBPUSD"' not in src and "== 'GBPUSD'" not in src

    def test_order_manager_has_no_symbol_comparisons(self):
        from services import order_manager
        src = open(order_manager.__file__).read()
        # El rate EURUSD es un dato de mercado (fetch), no una rama de mundo.
        assert 'if symbol ==' not in src and 'req.symbol ==' not in src
        assert '== "EURUSD"' not in src and '== "GBPUSD"' not in src

"""
P3-1d — riesgo por par: guardianes independientes, presupuesto de equity y
código muerto eliminado.

- UN guardián POR PAR (`_GUARDIANS[symbol]`): cupo de posiciones, racha de
  pérdidas y cooldown de alertas viven en el mundo que los sufre — un par
  jamás consume la capacidad de otro.
- El baseline del día (equity de la cuenta) se COMPARTE: `set_daily_start`
  siembra a todos, y un guardián tardío se siembra con él (nunca queda sin
  protección diaria).
- `equity_allocation_pct` en PairConfig: fracción (0, 100] del techo de size
  del par; ausente = 100 (el techo global de siempre).
- Wiring: el lifespan crea el guardián de cada par habilitado ANTES del
  baseline; código muerto (place_order_with_fallback + correlation_sizing)
  eliminado de verdad.
"""
import os

import pytest

import core.pair as pair_mod
import services.mt5_client as mt5_client
from services import risk_guardian as rg

_MAIN = os.path.join(os.path.dirname(__file__), "..", "main.py")
_MT5 = mt5_client.__file__
_CORRELATION = os.path.join(os.path.dirname(mt5_client.__file__), "correlation_sizing.py")


@pytest.fixture
def fresh_risk(monkeypatch):
    """Registro de guardianes y baseline limpios (sin polución de otros tests)."""
    monkeypatch.setattr(rg, "_GUARDIANS", {})
    monkeypatch.setattr(rg, "_day_baseline", 0.0)
    return rg


class TestGuardiansPerPair:
    def test_one_guardian_per_symbol(self, fresh_risk):
        eu1 = fresh_risk.get_risk_guardian("EURUSD")
        eu2 = fresh_risk.get_risk_guardian("EURUSD")
        gb = fresh_risk.get_risk_guardian("GBPUSD")
        assert eu1 is eu2          # mismo mundo → MISMO guardián
        assert eu1 is not gb       # mundos distintos → instancias distintas

    def test_default_symbol_is_legacy(self, fresh_risk):
        assert fresh_risk.get_risk_guardian() is fresh_risk.get_risk_guardian("EURUSD")

    def test_pair_states_are_independent(self, fresh_risk):
        eu = fresh_risk.get_risk_guardian("EURUSD")
        gb = fresh_risk.get_risk_guardian("GBPUSD")
        eu.state.max_positions = 1
        eu.state.consecutive_losses = 7
        assert gb.state.max_positions == 5      # default intacto
        assert gb.state.consecutive_losses == 0

    def test_check_risk_routes_to_pair_guardian(self, fresh_risk):
        fresh_risk.get_risk_guardian("GBPUSD").state.max_positions = 0
        ok, reason = fresh_risk.check_risk(1000.0, 1, symbol="GBPUSD")
        assert ok is False and "MAX_POSITIONS" in reason
        # EURUSD NO hereda la decisión de GBPUSD
        ok_eu, _ = fresh_risk.check_risk(1000.0, 1, symbol="EURUSD")
        assert ok_eu is True

    def test_baseline_seeds_late_guardian(self, fresh_risk):
        fresh_risk.set_daily_start(2000.0)              # aún sin guardianes
        late = fresh_risk.get_risk_guardian("GBPUSD")   # aparece DESPUÉS
        assert late.state.daily_start_equity == 2000.0
        assert late.state.peak_equity == 2000.0

    def test_set_daily_start_updates_all(self, fresh_risk):
        a = fresh_risk.get_risk_guardian("EURUSD")
        b = fresh_risk.get_risk_guardian("GBPUSD")
        fresh_risk.set_daily_start(1500.0)
        assert a.state.daily_start_equity == 1500.0
        assert b.state.daily_start_equity == 1500.0


class TestEquityAllocation:
    def test_default_is_full_ceiling(self):
        cfg = pair_mod.PairConfig.from_spec({"symbol": "EURUSD", "enabled": True})
        assert cfg.equity_allocation_pct == 100.0

    @pytest.mark.parametrize("bad", [0, -10, 100.1, 150, True, "50", None])
    def test_invalid_allocation_rejected_loudly(self, bad):
        with pytest.raises(ValueError, match="equity_allocation_pct"):
            pair_mod.PairConfig.from_spec(
                {"symbol": "GBPAUD", "enabled": True, "equity_allocation_pct": bad}
            )


class TestWiringAndDeadCode:
    def test_lifespan_creates_guardian_per_enabled_pair_before_baseline(self):
        with open(_MAIN, encoding="utf-8") as f:
            src = f.read()
        create = "get_risk_guardian(_ctx.config.symbol)"
        assert "build_registry_from_settings().enabled()" in src
        assert create in src
        # El guardián se crea ANTES de sembrar el baseline del día
        assert src.index(create) < src.index("set_daily_start(equity)")

    def test_dead_code_removed_for_real(self):
        with open(_MT5, encoding="utf-8") as f:
            mt5_src = f.read()
        assert "place_order_with_fallback" not in mt5_src
        assert "correlation_sizing" not in mt5_src
        assert not os.path.exists(_CORRELATION)
        with open(os.path.join(os.path.dirname(__file__), "..", "core", "adapters.py"),
                  encoding="utf-8") as f:
            assert "fue eliminado en P3-1d" in f.read()

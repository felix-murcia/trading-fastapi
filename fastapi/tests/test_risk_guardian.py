"""
P1-4: tests donde hay dinero — Risk Guardian (bloqueos de riesgo en vivo).

Cobertura de comportamiento (sin red, instancias frescas):
- Pérdida diaria ≥ límite → check_equity devuelve mensaje y can_open_position bloquea.
- Dentro del límite → pasa.
- Max posiciones alcanzado → bloquea con razón.
- Drawdown ≥10% desde peak → alerta (log ERROR) pero NO bloquea.
- Cooldown de alertas: la segunda del mismo tipo en <1h no se emite.
- Racha de pérdidas: se acumula y se resetea con un resultado no negativo.
- Sin daily_start (0) → sin ZeroDivisionError, sin bloqueo.
"""
import logging
import time

import pytest

from services.risk_guardian import RiskGuardian, check_risk, get_risk_guardian


class TestDailyLossLimit:
    def test_breach_blocks_new_orders(self):
        g = RiskGuardian(max_positions=5, daily_loss_pct=5.0)
        g.set_daily_start(1000.0)

        allowed, reason = g.can_open_position(950.0, open_positions=0)

        assert allowed is False
        assert "DAILY LOSS LIMIT 5.0%" in reason

    def test_breach_threshold_is_inclusive(self):
        g = RiskGuardian(daily_loss_pct=5.0)
        g.set_daily_start(1000.0)
        # Exactamente -5% (pnl_pct <= -limit) → bloquea
        assert g.check_equity(950.0) is not None
        # -4.99% → dentro del límite
        assert g.check_equity(950.01) is None

    def test_only_down_vs_daily_start_counts(self):
        g = RiskGuardian(daily_loss_pct=5.0)
        g.set_daily_start(1000.0)
        # Subida posterior y caída relativa al pico NO es pérdida diaria
        g.update_peak(1200.0)
        assert g.check_equity(1150.0) is None  # +15% vs inicio del día

    def test_zero_daily_start_skips_check_without_error(self):
        g = RiskGuardian(daily_loss_pct=5.0)
        # Sin set_daily_start → daily_start_equity=0: sin división por cero
        assert g.check_equity(0.0) is None


class TestMaxPositions:
    def test_blocks_at_limit(self):
        g = RiskGuardian(max_positions=3, daily_loss_pct=5.0)
        g.set_daily_start(1000.0)

        assert g.can_open_position(1000.0, open_positions=2) == (True, "")
        allowed, reason = g.can_open_position(1000.0, open_positions=3)
        assert allowed is False
        assert reason == "MAX_POSITIONS 3 reached"

    def test_daily_loss_takes_precedence_over_positions(self):
        g = RiskGuardian(max_positions=3, daily_loss_pct=5.0)
        g.set_daily_start(1000.0)

        allowed, reason = g.can_open_position(900.0, open_positions=9)
        assert allowed is False
        assert "DAILY LOSS" in reason  # chequeo de equity primero


class TestDrawdownAlert:
    def test_ten_percent_dd_alerts_but_does_not_block(self, caplog):
        g = RiskGuardian(daily_loss_pct=50.0)  # límite diario alto: aísla el DD
        g.set_daily_start(1000.0)
        g.update_peak(1500.0)

        with caplog.at_level(logging.ERROR):
            reason = g.check_equity(1340.0)  # DD = 160/1500 ≈ 10.67% ≥ 10%

        assert reason is None  # alerta, no bloqueo
        assert any("DRAWDOWN" in r.getMessage() for r in caplog.records)

    def test_small_dd_does_not_alert(self, caplog):
        g = RiskGuardian(daily_loss_pct=50.0)
        g.set_daily_start(1000.0)
        g.update_peak(1100.0)

        with caplog.at_level(logging.ERROR):
            g.check_equity(1050.0)  # DD ≈ 4.5% < 10%

        assert not any("DRAWDOWN" in r.getMessage() for r in caplog.records)

    def test_alert_cooldown_suppresses_repeats(self, caplog):
        g = RiskGuardian(daily_loss_pct=50.0)
        g.set_daily_start(1000.0)

        with caplog.at_level(logging.ERROR):
            g._alert("DAILY-LOSS", 900.0, -10.0)
            g._alert("DAILY-LOSS", 890.0, -11.0)   # dentro del cooldown (1h)

        alerts = [r for r in caplog.records if "DAILY-LOSS" in r.getMessage()]
        assert len(alerts) == 1

        # Caducidad del cooldown → la siguiente sí se emite
        g.state.last_alert_ts = time.time() - g.state.alert_cooldown_s - 1.0
        with caplog.at_level(logging.ERROR):
            g._alert("DAILY-LOSS", 880.0, -12.0)
        alerts = [r for r in caplog.records if "DAILY-LOSS" in r.getMessage()]
        assert len(alerts) == 2


class TestConsecutiveLosses:
    def test_losses_accumulate_and_win_resets(self):
        g = RiskGuardian()
        g.record_trade_result(-0.02)
        g.record_trade_result(-0.01)
        assert g.state.consecutive_losses == 2

        g.record_trade_result(0.03)
        assert g.state.consecutive_losses == 0

    def test_zero_pnl_resets_streak(self):
        g = RiskGuardian()
        g.record_trade_result(-0.01)
        g.record_trade_result(0.0)  # pnl_pct >= 0 → racha limpia
        assert g.state.consecutive_losses == 0


class TestGlobalWrapper:
    def test_check_risk_blocks_on_max_positions(self, monkeypatch):
        g = get_risk_guardian()
        # Estado determinista independiente de tests anteriores (lifespan u otros)
        g.state.daily_start_equity = 0.0
        g.state.peak_equity = 0.0

        assert check_risk(1000.0, open_positions=0) == (True, "")
        allowed, reason = check_risk(1000.0, open_positions=g.state.max_positions + 1)
        assert allowed is False
        assert "MAX_POSITIONS" in reason

"""
P1-4: tests donde hay dinero — Risk Guardian (bloqueos de riesgo en vivo).

Cobertura de comportamiento (sin red, instancias frescas):
- Pérdida diaria ≥ límite → check_equity devuelve mensaje y can_open_position bloquea.
- Dentro del límite → pasa.
- Max posiciones alcanzado → bloquea con razón; exposición TOTAL de la cuenta
  alcanzada → bloquea aunque el par tenga cupo (P2-4).
- Drawdown ≥10% desde peak → alerta (log ERROR) pero NO bloquea.
- Cooldown de alertas: la segunda del mismo tipo en <1h no se emite.
- Racha de pérdidas: se acumula con is_win=False y se resetea con is_win=True
  (la clasificación ≥3 EUR la decide el caller — core.rules).
- Sin daily_start (0) → sin ZeroDivisionError, sin bloqueo.
- El baseline del día vive en AccountRiskState (cuenta compartida) — los
  guardianes leídos como g.account.*
"""
import logging
import time

import pytest

from services.risk_guardian import RiskGuardian, check_risk, get_risk_guardian


class TestDailyLossLimit:
    def test_breach_blocks_new_orders(self):
        g = RiskGuardian(max_positions=5, daily_loss_pct=5.0)
        g.set_daily_start(1000.0)

        allowed, reason = g.can_open_position(950.0, open_positions=0, total_open_positions=0, total_risk_eur=0.0, same_dir_open=0)

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

        assert g.can_open_position(1000.0, open_positions=2, total_open_positions=2, total_risk_eur=0.0, same_dir_open=0) == (True, "")
        allowed, reason = g.can_open_position(1000.0, open_positions=3, total_open_positions=3, total_risk_eur=0.0, same_dir_open=0)
        assert allowed is False
        assert reason == "MAX_POSITIONS 3 reached"

    def test_daily_loss_takes_precedence_over_positions(self):
        g = RiskGuardian(max_positions=3, daily_loss_pct=5.0)
        g.set_daily_start(1000.0)

        allowed, reason = g.can_open_position(900.0, open_positions=9, total_open_positions=9, total_risk_eur=0.0, same_dir_open=0)
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
        g.record_trade_result(False)
        g.record_trade_result(False)
        assert g.state.consecutive_losses == 2

        g.record_trade_result(True)
        assert g.state.consecutive_losses == 0

    def test_streak_change_marks_dirty_for_persistence(self):
        # P2-4: la racha solo persiste si cambió (dirty en False→0 no se marca)
        g = RiskGuardian()
        assert g.state.dirty is False
        g.record_trade_result(False)
        assert g.state.dirty is True
        g.state.dirty = False
        g.record_trade_result(True)
        assert g.state.dirty is True and g.state.consecutive_losses == 0
        g.state.dirty = False
        g.record_trade_result(True)  # sin cambio → sin dirty
        assert g.state.dirty is False


class TestGlobalWrapper:
    def test_check_risk_blocks_on_max_positions(self, monkeypatch):
        g = get_risk_guardian()
        # Estado determinista independiente de tests anteriores (lifespan u otros)
        g.account.daily_start_equity = 0.0
        g.account.peak_equity = 0.0

        assert check_risk(1000.0, open_positions=0, total_open_positions=0, total_risk_eur=0.0, same_dir_open=0) == (True, "")
        allowed, reason = check_risk(1000.0, open_positions=g.state.max_positions + 1, total_open_positions=1, total_risk_eur=0.0, same_dir_open=0)
        assert allowed is False
        assert "MAX_POSITIONS" in reason


class TestMaxDrawdownBlockConfig:
    """P3-3: bloqueo por DD de cuenta config-driven (canario = 15%).

    None (default) = comportamiento histórico: alerta al 10%, nunca bloquea.
    """

    def test_block_at_configured_threshold(self, monkeypatch):
        import services.risk_guardian as rg
        monkeypatch.setattr(rg.settings, "max_drawdown_block_pct", 15.0)
        g = RiskGuardian(daily_loss_pct=50.0)  # límite diario alto: aísla el DD
        g.set_daily_start(1500.0)
        g.update_peak(1500.0)

        reason = g.check_equity(1200.0)   # DD = 300/1500 = 20% ≥ 15%

        assert reason is not None
        assert reason.startswith("MAX DRAWDOWN")

    def test_below_block_threshold_keeps_alert_only(self, monkeypatch):
        import services.risk_guardian as rg
        monkeypatch.setattr(rg.settings, "max_drawdown_block_pct", 15.0)
        g = RiskGuardian(daily_loss_pct=50.0)
        g.set_daily_start(1000.0)
        g.update_peak(1500.0)

        reason = g.check_equity(1340.0)   # DD ≈ 10.67%: alerta ≥10, no bloquea

        assert reason is None

    def test_unset_threshold_never_blocks(self, monkeypatch):
        import services.risk_guardian as rg
        monkeypatch.setattr(rg.settings, "max_drawdown_block_pct", None)
        g = RiskGuardian(daily_loss_pct=50.0)
        g.set_daily_start(1500.0)
        g.update_peak(1500.0)

        assert g.check_equity(1200.0) is None   # DD 20% sin umbral → solo alerta


class TestDailyLossFromPairSpec:
    """P3-3: el límite diario por par sale del spec en PAIRS (canario = 2%)."""

    def test_guardian_uses_spec_daily_loss(self, monkeypatch):
        import services.risk_guardian as rg
        monkeypatch.setattr(rg.settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": False, "daily_loss_pct": 2.0},
        ])
        monkeypatch.setattr(rg, "_GUARDIANS", {})

        gb = rg.get_risk_guardian("GBPUSD")
        eu = rg.get_risk_guardian("EURUSD")

        assert gb.state.daily_loss_limit_pct == 2.0
        assert eu.state.daily_loss_limit_pct == 5.0   # ausente → default

    def test_unlisted_symbol_keeps_default(self, monkeypatch):
        import services.risk_guardian as rg
        monkeypatch.setattr(rg.settings, "pairs",
                            [{"symbol": "EURUSD", "enabled": True}])
        assert rg._daily_loss_pct_for("AUDUSD") == 5.0

    def test_spec_daily_loss_validated(self):
        from core.pair import PairConfig
        base = {"symbol": "GBPUSD", "enabled": True}
        assert PairConfig.from_spec({**base, "daily_loss_pct": 2.0}).daily_loss_pct == 2.0
        assert PairConfig.from_spec(base).daily_loss_pct == 5.0        # ausente
        for bad in (0, -1, 101, True, "2", None):
            with pytest.raises(ValueError, match="daily_loss_pct"):
                PairConfig.from_spec({**base, "daily_loss_pct": bad})

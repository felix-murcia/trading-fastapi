"""
Risk Guardian — protege contra pérdidas excesivas.
- Daily loss limit: cierra todas las posiciones si DDD > threshold
- Max open positions: bloquea nuevas órdenes si se alcanza el límite
- Drawdown alerting: loguea cuando equity cae de peak

P3-1d: UN guardián POR PAR (_GUARDIANS[symbol]) — el cupo de posiciones
abiertas, la racha de pérdidas y el cooldown de alertas son del mundo que
los vive; un par nunca consume la capacidad de otro. El baseline de día
(daily_start/peak) es del ACCOUNT equity y se comparte: set_daily_start
sembrará a todos los guardiánies (incluidos los creados después).
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from core.pair import LEGACY_SYMBOL

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    peak_equity: float = 0.0
    daily_start_equity: float = 0.0
    daily_start_ts: float = field(default_factory=time.time)
    max_positions: int = 5
    daily_loss_limit_pct: float = 5.0  # 5% del equity inicial del día
    consecutive_losses: int = 0
    last_alert_ts: float = 0.0
    alert_cooldown_s: float = 3600.0  # 1h entre alertas del mismo tipo


class RiskGuardian:
    """
    Vigila equity y bloquea operaciones cuando se superan umbrales de riesgo.
    """

    def __init__(self, max_positions: int = 5, daily_loss_pct: float = 5.0):
        self.state = RiskState(
            max_positions=max_positions,
            daily_loss_limit_pct=daily_loss_pct,
        )
        self._position_count: int = 0

    def set_daily_start(self, equity: float):
        """Llamar al iniciar el día (o al arrancar el servidor)."""
        self.state.daily_start_equity = equity
        self.state.peak_equity = equity
        self.state.daily_start_ts = time.time()
        logger.warning("[RISK-GUARDIAN] Día iniciado — equity=%.2f", equity)

    def update_peak(self, equity: float):
        """Actualiza el peak de equity tras cada ciclo."""
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity

    def check_equity(self, current_equity: float) -> Optional[str]:
        """
        Verifica drawdown y pérdida diaria.
        Retorna mensaje de alerta o None si todo OK.
        """
        self.update_peak(current_equity)

        # 1. Daily loss
        if self.state.daily_start_equity > 0:
            daily_pnl_pct = (
                (current_equity - self.state.daily_start_equity)
                / self.state.daily_start_equity * 100
            )
            if daily_pnl_pct <= -self.state.daily_loss_limit_pct:
                self._alert("DAILY-LOSS", current_equity, daily_pnl_pct)
                return f"DAILY LOSS LIMIT {abs(daily_pnl_pct):.1f}% reached — blocking new orders"

        # 2. Drawdown desde peak
        if self.state.peak_equity > 0:
            dd_pct = (
                (self.state.peak_equity - current_equity)
                / self.state.peak_equity * 100
            )
            if dd_pct >= 10.0:  # 10% drawdown
                self._alert("DRAWDOWN", current_equity, dd_pct)

        return None

    def can_open_position(self, current_equity: float, open_positions: int) -> tuple[bool, str]:
        """
        Retorna (True, "") si puede abrir posición, o (False, razón) si no.
        """
        # Check equity limits
        reason = self.check_equity(current_equity)
        if reason:
            return False, reason

        # Check max positions
        if open_positions >= self.state.max_positions:
            reason = f"MAX_POSITIONS {self.state.max_positions} reached"
            logger.warning("[RISK-GUARDIAN] %s", reason)
            return False, reason

        return True, ""

    def record_trade_result(self, pnl_pct: float):
        """Llama tras cerrar una operación para trackear racha."""
        if pnl_pct < 0:
            self.state.consecutive_losses += 1
            logger.warning("[RISK-GUARDIAN] Pérdida detectada — racha: %d",
                          self.state.consecutive_losses)
        else:
            self.state.consecutive_losses = 0

    def _alert(self, alert_type: str, equity: float, value: float):
        now = time.time()
        if now - self.state.last_alert_ts < self.state.alert_cooldown_s:
            return  # Cooldown activo
        self.state.last_alert_ts = now
        logger.error(
            "[RISK-GUARDIAN] 🚨 %s — equity=%.2f, value=%.2f",
            alert_type, equity, value
        )


# ─── Registro por par (P3-1d) ────────────────────────────────────────────────
_GUARDIANS: dict[str, RiskGuardian] = {}
_day_baseline: float = 0.0  # equity al inicio del día (la cuenta es UNA)


def get_risk_guardian(symbol: str = LEGACY_SYMBOL) -> RiskGuardian:
    """Guardián de ESTE par: uno por mundo, creado bajo demanda.

    Si el baseline del día ya se fijó, el guardián tardío se siembra con él —
    un par que aparece después del arranque jamás queda sin protección diaria.
    """
    guardian = _GUARDIANS.get(symbol)
    if guardian is None:
        guardian = RiskGuardian()
        if _day_baseline > 0:
            guardian.set_daily_start(_day_baseline)
        _GUARDIANS[symbol] = guardian
        logger.warning("[RISK-GUARDIAN] Guardián creado para %s", symbol)
    return guardian


def set_daily_start(equity: float):
    """Baseline del día para TODOS los guardiánies (el equity es de la cuenta)."""
    global _day_baseline
    _day_baseline = equity
    for guardian in list(_GUARDIANS.values()):
        guardian.set_daily_start(equity)


def check_risk(
    equity: float, open_positions: int, *, symbol: str = LEGACY_SYMBOL
) -> tuple[bool, str]:
    return get_risk_guardian(symbol).can_open_position(equity, open_positions)


def record_trade(pnl_pct: float, *, symbol: str = LEGACY_SYMBOL):
    get_risk_guardian(symbol).record_trade_result(pnl_pct)

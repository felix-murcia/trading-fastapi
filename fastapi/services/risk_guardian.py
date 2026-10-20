"""
Risk Guardian — protege contra pérdidas excesivas.
- Daily loss limit: bloquea nuevas órdenes si la pérdida del día supera el umbral
- Max open positions: cupo por par
- Max total positions (P2-4): exposición AGREGADA de todas las estrategias/pares
- Drawdown alerting: loguea cuando equity cae de peak

P3-1d: UN guardián POR PAR (_GUARDIANS[symbol]) — el cupo de posiciones
abiertas, la racha de pérdidas y el cooldown de alertas son del mundo que
los vive; un par nunca consume la capacidad de otro.

P2-4: el baseline del día (day/daily_start/peak — propiedades del equity de
la CUENTA) vive en UN `AccountRiskState` compartido por todos los guardianes:
una sola fuente de verdad (sin divergencia de peak entre mundos) que
`services.risk_state_store` persiste en `risk_account_state`:
- Un reinicio a mitad de día NO borra la pérdida ya sufrida ni el peak
  (restauración desde DB en vez de reiniciar el baseline con el equity actual).
- `observe(equity)` hace rollover automático al cambiar la fecha UTC: el
  límite diario mide de verdad un día, no "desde el último arranque".
- La racha del par se persiste en `risk_pair_state` y se registra bajo la
  convención >= 3 EUR (el caller decide quién ganó — core.rules).
"""
import time
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from config import settings
from core.pair import LEGACY_SYMBOL, PairConfig

logger = logging.getLogger(__name__)


def _utc_today() -> date:
    """DÍA DE LA CUENTA = día del BRÓKER (T-31.9, 2026-10-20).

    Antes se usaba el día civil UTC. Medido: el día del bróquer (y el del
    extracto de cuenta) empieza a las 21:00 UTC con el desfase actual de
    +3 h ⇒ el límite de pérdida diaria se reiniciaba 3 h ANTES de que
    terminara el día del bróquer (y tras el cambio de horario del 25-10,
    4 h). El estado de cuenta y su extracto miden el día del bróquer ⇒ el
    corte diario debe seguirlo.

    Degradación: si el desfase aún no se ha detectado (MCP caído al
    arrancar), cae al día UTC (comportamiento histórico) — nunca rompe.
    """
    try:
        from services.timebase import broker_today
        return broker_today()
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).date()


@dataclass
class AccountRiskState:
    """Equity de la CUENTA — UNA instancia compartida por todos los guardianes.

    `dirty` = cambios aún no persistidos (lo consume services.risk_state_store).
    """
    day: Optional[date] = None
    daily_start_equity: float = 0.0
    peak_equity: float = 0.0
    daily_start_ts: float = 0.0
    dirty: bool = False

    def start(self, equity: float) -> None:
        """Baseline nuevo (día nuevo o primer arranque)."""
        self.day = _utc_today()
        self.daily_start_equity = equity
        self.peak_equity = equity
        self.daily_start_ts = time.time()
        self.dirty = True

    def restore(self, day: date, daily_start_equity: float, peak_equity: float) -> None:
        """Restauración desde DB (mismo día): preserva el baseline REAL del día."""
        self.day = day
        self.daily_start_equity = daily_start_equity
        self.peak_equity = peak_equity
        self.dirty = False

    def observe(self, equity: float) -> None:
        """Rollover automático + peak. Llamar en cada ciclo que conozca el equity."""
        today = _utc_today()
        if self.day != today:  # None (nunca sembrado) o día vencido → día nuevo
            logger.warning(
                "[RISK-GUARDIAN] Rollover diario (%s → %s) — baseline = equity actual %.2f",
                self.day, today, equity,
            )
            self.start(equity)
        elif equity > self.peak_equity:
            self.peak_equity = equity
            self.dirty = True


@dataclass
class RiskState:
    """Estado del PAR (cupo, racha, alertas). El baseline del día NO vive aquí:
    es de la cuenta y se comparte vía AccountRiskState."""
    max_positions: int = 5
    daily_loss_limit_pct: float = 5.0  # 5% del equity inicial del día (cuenta)
    consecutive_losses: int = 0
    last_alert_ts: float = 0.0
    alert_cooldown_s: float = 3600.0  # 1h entre alertas del mismo tipo
    dirty: bool = False  # racha/alerta sin persistir


class RiskGuardian:
    """
    Vigila equity y bloquea operaciones cuando se superan umbrales de riesgo.
    """

    def __init__(self, max_positions: int = 5, daily_loss_pct: float = 5.0,
                 *, account: AccountRiskState | None = None):
        # Cada guardián nace con SU cuenta (tests/instancias sueltas); los
        # registrados por par comparten la cuenta real del módulo.
        self.account = account if account is not None else AccountRiskState()
        self.state = RiskState(
            max_positions=max_positions,
            daily_loss_limit_pct=daily_loss_pct,
        )

    def set_daily_start(self, equity: float):
        """Fija el baseline del día en la cuenta compartida."""
        self.account.start(equity)
        logger.warning("[RISK-GUARDIAN] Día iniciado — equity=%.2f", equity)

    def update_peak(self, equity: float):
        """Rollover + peak en la cuenta (una sola fuente para todos los guardianes)."""
        self.account.observe(equity)

    def check_equity(self, current_equity: float) -> Optional[str]:
        """
        Verifica drawdown y pérdida diaria.
        Retorna mensaje de alerta o None si todo OK.
        """
        self.account.observe(current_equity)  # rollover + peak (P2-4)

        # 1. Daily loss (baseline del día vigente en la cuenta)
        if self.account.daily_start_equity > 0:
            daily_pnl_pct = (
                (current_equity - self.account.daily_start_equity)
                / self.account.daily_start_equity * 100
            )
            if daily_pnl_pct <= -self.state.daily_loss_limit_pct:
                self._alert("DAILY-LOSS", current_equity, daily_pnl_pct)
                return f"DAILY LOSS LIMIT {abs(daily_pnl_pct):.1f}% reached — blocking new orders"

        # 2. Drawdown desde peak (cuenta)
        if self.account.peak_equity > 0:
            dd_pct = (
                (self.account.peak_equity - current_equity)
                / self.account.peak_equity * 100
            )
            if dd_pct >= 10.0:  # 10% drawdown
                self._alert("DRAWDOWN", current_equity, dd_pct)
            # P3-3: bloqueo por DD de cuenta CONFIG-DRIVEN (canario = 15%).
            # None (default) = comportamiento previo: solo alerta a 10%,
            # nunca bloquea. El DD es de la cuenta (un equity) → el bloqueo
            # afecta a todos los pares: es la MISMA dinero.
            block_pct = settings.max_drawdown_block_pct
            if block_pct is not None and dd_pct >= block_pct:
                logger.error(
                    "[RISK-GUARDIAN] 🚨 MAX-DD BLOCK — equity=%.2f, dd=%.1f%% >= %.1f%%",
                    current_equity, dd_pct, block_pct,
                )
                return f"MAX DRAWDOWN {dd_pct:.1f}% >= {block_pct:.1f}% — blocking new orders"

        return None

    def can_open_position(
        self, current_equity: float, open_positions: int, *, total_open_positions: int,
        total_risk_eur: float, same_dir_open: int
    ) -> tuple[bool, str]:
        """
        Retorna (True, "") si puede abrir posición, o (False, razón) si no.

        `open_positions` = posiciones de ESTE par; `total_open_positions` = de
        TODA la cuenta (todas las estrategias y pares) — exigido, sin default:
        un call sin exposición agregada no puede pasar en silencio.
        `total_risk_eur` = riesgo-1R agregado de la cuenta en EUR (G2-5/D2).
        `same_dir_open` = posiciones abiertas en la MISMA dirección que la
        propuesta (G2-5/D3). Ambos exigidos sin default (misma razón).
        """
        # Check equity limits
        reason = self.check_equity(current_equity)
        if reason:
            return False, reason

        # Check max positions (POR PAR)
        if open_positions >= self.state.max_positions:
            reason = f"MAX_POSITIONS {self.state.max_positions} reached"
            logger.warning("[RISK-GUARDIAN] %s", reason)
            return False, reason

        # P2-4: exposición AGREGADA de la cuenta (PPO + SMC + todos los pares)
        total_limit = settings.max_total_positions
        if total_open_positions >= total_limit:
            reason = f"MAX_TOTAL_POSITIONS {total_limit} reached"
            logger.warning("[RISK-GUARDIAN] %s (abiertas en cuenta)", reason)
            return False, reason

        # G2-5/D3 (orden Félix 2026-10-17, canario 4): tope de posiciones MISMA
        # DIRECCIÓN en la cuenta — EURUSD y GBPUSD comparten factor USD
        # (ρ≈0.7-0.9); sin esta capa, N mismos largos = exposición USD doble
        # sin freno. 0 = desactivado (config-driven, sin branching por símbolo).
        same_dir_limit = settings.max_total_same_dir_positions
        if same_dir_limit > 0 and same_dir_open >= same_dir_limit:
            reason = f"SAME_DIR_LIMIT {same_dir_limit} reached"
            logger.warning(
                "[RISK-GUARDIAN] %s (%d abiertas en la misma dirección)",
                reason, same_dir_open,
            )
            return False, reason

        # G2-5/D2 (orden Félix 2026-10-17, canario 0.09): tope de RIESGO-1R
        # AGREGADO en EUR — el conteo de posiciones no distingue 0.01 de
        # 0.50 lot; esta capa mide el dinero realmente en riesgo. None = off.
        risk_cap_pct = settings.max_total_risk_pct
        if risk_cap_pct is not None and current_equity > 0:
            budget = current_equity * risk_cap_pct
            if total_risk_eur >= budget:
                reason = (
                    f"MAX_TOTAL_RISK {total_risk_eur:.2f} >= "
                    f"{budget:.2f} EUR ({risk_cap_pct:.1%} del equity)"
                )
                logger.warning("[RISK-GUARDIAN] %s", reason)
                return False, reason

        return True, ""

    def record_trade_result(self, is_win: bool):
        """Racha del PAR. `is_win` llega ya clasificado bajo la convención
        >= 3 EUR (core.rules.is_winning_close) — el guardián no re-deriva."""
        if not is_win:
            self.state.consecutive_losses += 1
            self.state.dirty = True
            logger.warning("[RISK-GUARDIAN] Pérdida detectada — racha: %d",
                          self.state.consecutive_losses)
        elif self.state.consecutive_losses != 0:
            self.state.consecutive_losses = 0
            self.state.dirty = True

    def _alert(self, alert_type: str, equity: float, value: float):
        now = time.time()
        if now - self.state.last_alert_ts < self.state.alert_cooldown_s:
            return  # Cooldown activo
        self.state.last_alert_ts = now
        self.state.dirty = True  # cooldown persistido (P2-4)
        logger.error(
            "[RISK-GUARDIAN] 🚨 %s — equity=%.2f, value=%.2f",
            alert_type, equity, value
        )


# ─── Registro por par (P3-1d) + cuenta única (P2-4) ──────────────────────────
_GUARDIANS: dict[str, RiskGuardian] = {}
_ACCOUNT = AccountRiskState()  # baseline de la cuenta: UNO, compartido


def _daily_loss_pct_for(symbol: str) -> float:
    """Límite de pérdida diaria configurado para `symbol` en PAIRS (P3-3).

    El canario GBPUSD lo fija a 2 (spec de config); símbolo sin entrada →
    5.0 (mismo default que RiskState). La validación del spec es de
    PairConfig.from_spec — una sola fuente de reglas, cero duplicación.
    """
    for spec in settings.pairs:
        if isinstance(spec, dict) and spec.get("symbol") == symbol:
            return PairConfig.from_spec(spec).daily_loss_pct
    return 5.0


def get_account_state() -> AccountRiskState:
    """La cuenta es una: la fuente de verdad que persiste risk_state_store."""
    return _ACCOUNT


def registered_guardians() -> dict[str, RiskGuardian]:
    """Guardianes creados (copia) — para persistir sin crear bajo demanda."""
    return dict(_GUARDIANS)


def observe_equity(equity: float) -> None:
    """P2-4: cada ciclo con equity observa la cuenta (rollover + peak en vivo)."""
    _ACCOUNT.observe(equity)


def get_risk_guardian(symbol: str = LEGACY_SYMBOL) -> RiskGuardian:
    """Guardián de ESTE par: uno por mundo, creado bajo demanda.

    Comparte la cuenta (_ACCOUNT): un guardián tardío ve el baseline del día
    sin siembra explícita — nunca queda sin protección diaria.
    """
    guardian = _GUARDIANS.get(symbol)
    if guardian is None:
        # P3-3: el límite diario sale del spec del par en PAIRS (canario=2%).
        guardian = RiskGuardian(
            daily_loss_pct=_daily_loss_pct_for(symbol),
            account=_ACCOUNT,
        )
        _GUARDIANS[symbol] = guardian
        logger.warning(
            "[RISK-GUARDIAN] Guardián creado para %s (daily_loss=%.1f%%)",
            symbol, guardian.state.daily_loss_limit_pct,
        )
    return guardian


def set_daily_start(equity: float):
    """Baseline del día para la cuenta entera (AccountRiskState compartido)."""
    _ACCOUNT.start(equity)


def check_risk(
    equity: float, open_positions: int, *, total_open_positions: int,
    total_risk_eur: float, same_dir_open: int,
    symbol: str = LEGACY_SYMBOL
) -> tuple[bool, str]:
    """G2-5 (D2/D3, orden Félix 2026-10-17): `total_risk_eur` y `same_dir_open`
    exigidos SIN default, como `total_open_positions` desde P2-4 — un call sin
    la exposición completa no puede pasar en silencio."""
    return get_risk_guardian(symbol).can_open_position(
        equity, open_positions, total_open_positions=total_open_positions,
        total_risk_eur=total_risk_eur, same_dir_open=same_dir_open,
    )


def record_trade(is_win: bool, *, symbol: str = LEGACY_SYMBOL):
    get_risk_guardian(symbol).record_trade_result(is_win)

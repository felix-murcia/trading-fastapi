"""
P2-4: estado de riesgo persistente — la protección sobrevive a reinicios.

Tablas (DDL en db/schema.sql):
- `risk_account_state` (fila única id=1): baseline del día (daily_start) +
  peak de la cuenta. Sin esto, un reinicio a mitad de día REINICIABA el
  baseline con el equity actual: la pérdida ya sufrida desaparecía y el
  límite diario dejaba de proteger (hallazgo P2-4 "estado de riesgo
  persistente con rollover diario").
- `risk_pair_state` (por símbolo): racha de pérdidas + cooldown de alertas.

Flujo:
- `init_risk_state(equity)` en el lifespan: restaura el estado (mismo día →
  baseline REAL preservado) o rueda el día (vencido/primera vez → baseline =
  equity actual, escrito ya). Sin equity (MCP caído) el primer observe() del
  día fija el baseline y lo persiste el primer flush.
- `flush_risk_state(symbol)` en cada punto con equity/órdenes: solo escribe
  si hay cambios dirty — la mayoría de los ciclos NO toca la BD. Fallo de
  escritura → log ruidoso + dirty conservado (se reintenta; la protección en
  memoria sigue viva).
"""
import logging
from datetime import datetime, timezone

from db.connection import get_pool
from services.risk_guardian import (
    get_account_state,
    get_risk_guardian,
    registered_guardians,
)

logger = logging.getLogger(__name__)

_ACCOUNT_UPSERT = """
    INSERT INTO risk_account_state (id, day, daily_start_equity, peak_equity)
    VALUES (1, $1, $2, $3)
    ON CONFLICT (id) DO UPDATE
        SET day = $1, daily_start_equity = $2, peak_equity = $3, updated_at = NOW()
"""

_PAIR_UPSERT = """
    INSERT INTO risk_pair_state (symbol, consecutive_losses, last_alert_ts)
    VALUES ($1, $2, $3)
    ON CONFLICT (symbol) DO UPDATE
        SET consecutive_losses = $2, last_alert_ts = $3, updated_at = NOW()
"""


async def init_risk_state(current_equity: float | None) -> None:
    """Arranque: restaura desde DB o rueda el día; siembra guardianes por par.

    Los guardianes deben estar YA creados (lifespan los crea antes de llamar).
    """
    pool = get_pool()
    today = datetime.now(timezone.utc).date()
    row = await pool.fetchrow(
        "SELECT day, daily_start_equity, peak_equity FROM risk_account_state WHERE id = 1"
    )
    account = get_account_state()

    if row is not None and row["day"] == today:
        # RESTART A MITAD DE DÍA: conserva el baseline real — la pérdida ya
        # sufrida sigue contando y el peak no se olvida.
        account.restore(row["day"], float(row["daily_start_equity"]), float(row["peak_equity"]))
        eq_txt = "n/d" if current_equity is None else f"{current_equity:.2f}"
        logger.warning(
            "[RISK-STATE] Día restaurado desde DB — start=%.2f peak=%.2f (equity actual=%s)",
            account.daily_start_equity, account.peak_equity, eq_txt,
        )
    elif current_equity is not None:
        # Día vencido o primera vez → baseline = equity actual, escrito ya.
        account.start(current_equity)
        account.dirty = False  # se limpia antes de escribir (observe concurrente
        await _persist_account(pool)  # durante el await deja dirty=True → siguiente flush)
        logger.warning("[RISK-STATE] Día nuevo/primera vez — baseline=%.2f persistido", current_equity)
    else:
        # Sin equity (MCP caído) y sin fila vigente: observe() siembra en el
        # primer ciclo con equity y flush_risk_state lo persiste.
        logger.warning("[RISK-STATE] Sin equity al arrancar — baseline se siembra en el primer ciclo")

    # Guardianes por par: racha + cooldown de alertas (fila vencida no aplica:
    # la racha no está day-scoped y el cooldown es de 1h, no de día).
    rows = await pool.fetch(
        "SELECT symbol, consecutive_losses, last_alert_ts FROM risk_pair_state"
    )
    guardians = registered_guardians()
    restored = 0
    for r in rows:
        guardian = guardians.get(r["symbol"])
        if guardian is not None:
            guardian.state.consecutive_losses = int(r["consecutive_losses"])
            guardian.state.last_alert_ts = float(r["last_alert_ts"])
            restored += 1
    if rows:
        logger.warning("[RISK-STATE] Estado de par restaurado: %d/%d filas", restored, len(rows))


async def _persist_account(pool) -> None:
    account = get_account_state()
    await pool.execute(
        _ACCOUNT_UPSERT, account.day, account.daily_start_equity, account.peak_equity
    )


async def flush_risk_state(*, symbol: str | None = None) -> None:
    """Persiste cambios pendientes (dirty) de la cuenta y del par.

    Sin dirty → NO toca la BD (los ciclos de /predict y prepare no pagan I/O).
    Nunca lanza: la protección en memoria sigue viva; el fallo queda ruidoso en
    log y dirty conservado → se reintenta en el próximo flush.
    """
    account = get_account_state()
    guardian = registered_guardians().get(symbol) if symbol else None

    acc_dirty = account.dirty
    pair_dirty = guardian is not None and guardian.state.dirty
    if not acc_dirty and not pair_dirty:
        return

    try:
        pool = get_pool()
        if acc_dirty:
            # Limpia ANTES de escribir: un observe() concurrente durante el
            # await deja dirty=True de nuevo y se persiste en el próximo flush.
            account.dirty = False
            await _persist_account(pool)
            logger.warning(
                "[RISK-STATE] account persistido — day=%s start=%.2f peak=%.2f",
                account.day, account.daily_start_equity, account.peak_equity,
            )
        if pair_dirty:
            guardian.state.dirty = False
            await pool.execute(
                _PAIR_UPSERT, symbol, guardian.state.consecutive_losses,
                guardian.state.last_alert_ts,
            )
    except Exception:
        if acc_dirty:
            account.dirty = True
        if pair_dirty:
            guardian.state.dirty = True
        logger.exception("[RISK-STATE] flush falló (dirty conservado, se reintenta) symbol=%s", symbol)

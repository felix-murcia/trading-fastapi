"""
P2-H14 (2026-10-09) — watchdog de silencio de ai_predict_cycle.

Contexto: el blackout de ~62 h (H14) se detectó leyendo la BD a mano; el
sistema no tenía NINGUNA alerta de silencio. Este loop comprueba la edad
del último `ai_predict_cycle` en audit_log y avisa si supera
SILENCE_THRESHOLD_S (45 min).

Evidencia de cadencia (audit_log 2026-10-06→10-09):
  - Nominal: 5 min. Peor observado entre semana: ~10 min (12-15 UTC).
  - → 45 min = 4.5× la peor cadencia observada (sin falsos positivos por
    caídas puntuales de un ciclo o dos) y suficiente para cortar pronto un
    EA/MT5 caído (el blackout real habría alertado a los 45 min).

Ventana de supresión — cuándo NO se esperan ciclos (evidencia histórica
audit_log desde 2026-09-14):
  - Sábado y domingo: CERO ciclos (solo hay ticks → el EA no polla).
  - Viernes a partir de 21:00 UTC: el último ciclo del viernes es 20:55
    (cierre FX ~21:00); la supresión entra a las 21:00 con la edad del
    último ciclo en 5 min, mucho antes del umbral de 45 min.
  - Lunes desde 00:00 SÍ hay ciclos (patrón normal: 00:00 con cadencia
    completa) → el lunes a medianoche se alerta de verdad si el EA no
    arrancó (así se habría detectado el incidente del 05 a las 00:45 en
    lugar del descubrimiento manual a las 11:17).

Alerta con nivel ERROR → el rate-limit nativo de alerting.py (600 s por
huella) limita la re-emisión mientras el silencio persista: una alerta cada
~10 min, sin spam y sin quedarse muda.
"""
import asyncio
import logging
from datetime import datetime, timezone

from services.alerting import AlertLevel, send_alert

logger = logging.getLogger(__name__)

SILENCE_THRESHOLD_S = 45 * 60
CHECK_INTERVAL_S = 5 * 60
FIRST_CHECK_DELAY_S = 60


def _market_closed(now: datetime) -> bool:
    """True si, según la evidencia histórica, no se esperan ciclos.

    weekdays: 0=lunes … 4=viernes … 6=domingo.
    """
    wd = now.weekday()
    if wd in (5, 6):                       # sábado y domingo: 0 ciclos
        return True
    if wd == 4 and now.hour >= 21:         # viernes post-cierre (último: 20:55)
        return True
    return False


async def check_silence(now: datetime | None = None) -> int | None:
    """Comprueba la edad del último ai_predict_cycle.

    Devuelve la edad en segundos (para tests/logging) o None si no hay fila.
    Alerta PREDICT-SILENCE cuando la edad supera el umbral fuera de la
    ventana de mercado cerrado.
    """
    from db.connection import get_pool

    now = now or datetime.now(timezone.utc)
    last = await get_pool().fetchval(
        "SELECT max(created_at) FROM audit_log WHERE event='ai_predict_cycle'"
    )
    if last is None:
        # Nunca hubo ni un ciclo: sistema recién instalado o BD rota.
        if not _market_closed(now):
            send_alert(
                AlertLevel.ERROR, "PREDICT-SILENCE",
                "Sin NINGÚN ai_predict_cycle en audit_log (el sistema nunca "
                "ha emitido un ciclo)",
            )
        return None

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    age_s = (now - last).total_seconds()
    if age_s < SILENCE_THRESHOLD_S or _market_closed(now):
        return int(age_s)

    send_alert(
        AlertLevel.ERROR, "PREDICT-SILENCE",
        f"ai_predict_cycle enmudece desde hace {int(age_s // 60)} min "
        f"(umbral {SILENCE_THRESHOLD_S // 60} min) — ¿EA/MT5 caído?",
        context={"ultimo_ciclo": last.isoformat(), "edad_s": int(age_s)},
    )
    return int(age_s)


async def _predict_silence_loop() -> None:
    """Watchdog de silencio: primera pasada a los 60 s, luego cada 5 min."""
    logger.info(
        "[SILENCE] watchdog activo: umbral=%ss, chequeo cada %ss",
        SILENCE_THRESHOLD_S, CHECK_INTERVAL_S,
    )
    await asyncio.sleep(FIRST_CHECK_DELAY_S)
    while True:
        try:
            age_s = await check_silence()
            if age_s is not None:
                logger.debug("[SILENCE] edad del último ciclo=%ss", age_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            send_alert(
                AlertLevel.ERROR, "PREDICT-SILENCE",
                f"Watchdog de silencio falló: {type(exc).__name__}: {exc}",
                exc=exc,
            )
        await asyncio.sleep(CHECK_INTERVAL_S)

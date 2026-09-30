"""
Alerting service — dispara notificaciones cuando el sistema falla.
Logs + Telegram si hay token; preparado para más canales.

B-1 (plan 2026-09-30): rate-limit por clase de alerta — la misma combinación
(nivel + componente + huella del mensaje) no se re-emite dentro de la ventana
definida por _ALERT_RATE_WINDOW_S: las repeticiones se contabilizan en
silencio (DEBUG) y el recuento se anexa a la siguiente emisión permitida, de
modo que la supresión nunca es invisible. CRITICAL jamás se limita: si el
sistema puede estar perdiendo datos, cada aviso cuenta.
"""
import logging
import re
import traceback
from enum import Enum
from time import monotonic

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    WARNING = "WARNING"   # Algo falló pero el sistema sigue funcionando
    ERROR = "ERROR"       # Algo crítico falló — requiere atención
    CRITICAL = "CRITICAL" # El sistema puede estar perdiendo datos


_ALERT_RATE_WINDOW_S = 600.0

# (nivel, componente, huella) → monotonic de la última emisión, y las
# repeticiones suprimidas desde entonces. La huella colapsa cifras, así que
# mensajes con marcas de tiempo o contadores variables comparten clase (p. ej.
# errores de red con IP/puerto). La poda descarta clases inactivas pasada la
# ventana; las que quedan con supresiones pendientes se conservan hasta poder
# reportarlas (acotado: una entrada por clase de alerta distinta).
_alert_last_emit: dict[tuple[str, str, str], float] = {}
_alert_suppressed: dict[tuple[str, str, str], int] = {}


def _alert_shape(message: str) -> str:
    """Huella estable de la clase de alerta: cifras→#, espacios colapsados."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", message))[:200]


def _prune_alert_state(now: float) -> None:
    """Higiene de memoria: olvida clases sin actividad más allá de la ventana."""
    for key, ts in list(_alert_last_emit.items()):
        if now - ts >= _ALERT_RATE_WINDOW_S and not _alert_suppressed.get(key):
            del _alert_last_emit[key]


def send_alert(
    level: AlertLevel,
    component: str,
    message: str,
    exc: BaseException | None = None,
    context: dict | None = None,
) -> None:
    """
    Envía una alerta por el canal configurado (log; Telegram si hay token).

    Rate-limit (B-1): WARNING/ERROR repetidos con la misma huella dentro de
    _ALERT_RATE_WINDOW_S no se re-emiten (ni log ni Telegram) — típico en
    PIPELINE-HEALTH por poll o drift sin baseline por chequeo. La siguiente
    emisión permitida lleva el recuento "(+N repetidas suprimidas en 600s)".
    CRITICAL y la primera emisión de cada ventana salen siempre.
    """
    if level != AlertLevel.CRITICAL:
        key = (level.value, component, _alert_shape(message))
        now = monotonic()
        last = _alert_last_emit.get(key)
        if last is not None and now - last < _ALERT_RATE_WINDOW_S:
            _alert_suppressed[key] = _alert_suppressed.get(key, 0) + 1
            logger.debug("[ALERTING] rate-limit %ss: %s", int(_ALERT_RATE_WINDOW_S), key)
            return
        missed = _alert_suppressed.pop(key, 0)
        _alert_last_emit[key] = now
        if missed:
            message = (
                f"{message} "
                f"(+{missed} repetidas suprimidas en {int(_ALERT_RATE_WINDOW_S)}s)"
            )
        _prune_alert_state(now)

    parts = [f"[{level.value}] {component}: {message}"]
    if exc is not None:
        parts.append(f"  Exception: {type(exc).__name__}: {exc}")
        parts.append(f"  Traceback: {traceback.format_exc()}")
    if context:
        parts.append(f"  Context: {context}")

    full = "\n".join(parts)

    if level == AlertLevel.CRITICAL:
        logger.error(full)
    elif level == AlertLevel.ERROR:
        logger.error(full)
    else:
        logger.warning(full)

    # TODO: integrar Telegram/Slack/email cuando esté configurado
    try:
        _send_to_telegram(level, message, context)
    except Exception:
        # No dejar que un fallo en el canal de notificación mate la alerta
        logger.warning(f"Alerting channel failed: {context}")


def _send_to_telegram(level: AlertLevel, message: str, context: dict | None) -> None:
    """Envía a Telegram si está configurado. No falla si no puede."""
    try:
        import os
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return

        import httpx
        emoji = "🔴" if level == AlertLevel.CRITICAL else ("🟠" if level == AlertLevel.ERROR else "🟡")
        text = f"{emoji} *[{level.value}]* {message}"
        if context:
            import json
            text += f"\n```json\n{json.dumps(context, indent=2)}\n```"

        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5.0,
        )
    except Exception as e:
        logger.warning("[ALERTING] Telegram falló: %s", e)

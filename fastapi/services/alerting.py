"""
Alerting service — dispara notificaciones cuando el sistema falla.
Por ahora: logs de nivel ERROR. Preparado para Telegram/Slack/email.
"""
import logging
import traceback
from enum import Enum

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    WARNING = "WARNING"   # Algo falló pero el sistema sigue funcionando
    ERROR = "ERROR"       # Algo crítico falló — requiere atención
    CRITICAL = "CRITICAL" # El sistema puede estar perdiendo datos


def send_alert(
    level: AlertLevel,
    component: str,
    message: str,
    exc: BaseException | None = None,
    context: dict | None = None,
) -> None:
    """
    Envía una alerta por el canal configurado.
    Actualmente solo logea, pero está preparado para扩展 a Telegram/Slack.
    """
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
    _send_to_telegram(level, message, context)


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
    except Exception:
        pass  # No fallar la alerta por un fallo en el canal de notificación

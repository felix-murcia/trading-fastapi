"""
Logging estructurado con trace_id para correlación MQL5 ↔ FastAPI ↔ MT5.

Cada request recibe un trace_id único que:
1. Se propagan desde MQL5 via header X-Trace-ID (si existe)
2. Se genera uno nuevo si no existe
3. Aparece en TODOS los logs del request
4. Se devuelve en headers de respuesta
5. Se guarda en audit_log para correlación post-hoc
"""
import logging
import uuid
import json
import contextvars
from typing import Optional
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace_id", default=None)

_access_log = logging.getLogger("structured_access")


def get_trace_id() -> Optional[str]:
    """Obtiene el trace_id actual del contexto."""
    return trace_id_var.get()


def generate_trace_id() -> str:
    """Genera un nuevo trace_id."""
    return str(uuid.uuid4())[:16]


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware que:
    - Extrae X-Trace-ID del header o genera uno nuevo
    - Lo almacena en contextvars para acceso global
    - Añade trace_id a todos los logs del request
    - Devuelve trace_id en headers de respuesta
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        # Extraer trace_id del header o generar nuevo
        incoming_trace = request.headers.get("x-trace-id", "")
        trace_id = incoming_trace.strip() if incoming_trace.strip() else generate_trace_id()

        # Almacenar en contextvars
        token = trace_id_var.set(trace_id)

        # timestamps de enriquecimiento
        import time
        ts_start = time.time()

        # Enriquecer request state para acceso en handlers
        request.state.trace_id = trace_id
        request.state.trace_ts_start = ts_start

        # Log del request entrante (usa logger propio, no uvicorn.access)
        _access_log.info(
            "[trace_id=%s] %s %s", trace_id, request.method, request.url.path
        )

        try:
            response: Response = await call_next(request)

            # Añadir trace_id a headers de respuesta
            response.headers["X-Trace-ID"] = trace_id
            response.headers["X-Trace-Timestamp"] = str(int(ts_start))

            # Log de respuesta
            elapsed_ms = (time.time() - ts_start) * 1000
            _access_log.info(
                "[trace_id=%s] %s %s → %d (%.1fms)",
                trace_id, request.method, request.url.path,
                response.status_code, elapsed_ms
            )

            return response

        finally:
            trace_id_var.reset(token)


class StructuredLogger:
    """
    Logger wrapper que automáticamente incluye trace_id en todos los mensajes.
    Uso: logger = StructuredLogger(__name__)
    """

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def _format(self, msg: str, *args) -> tuple[str, list]:
        """Añade trace_id al mensaje."""
        tid = get_trace_id() or "no-trace"
        formatted = f"[trace_id={tid}] {msg}"
        return formatted, list(args)

    def debug(self, msg: str, *args, **kwargs):
        msg_fmt, args_fmt = self._format(msg, *args)
        self._logger.debug(msg_fmt, *args_fmt, **kwargs)

    def info(self, msg: str, *args, **kwargs):
        msg_fmt, args_fmt = self._format(msg, *args)
        self._logger.info(msg_fmt, *args_fmt, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        msg_fmt, args_fmt = self._format(msg, *args)
        self._logger.warning(msg_fmt, *args_fmt, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        msg_fmt, args_fmt = self._format(msg, *args)
        self._logger.error(msg_fmt, *args_fmt, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        msg_fmt, args_fmt = self._format(msg, *args)
        self._logger.critical(msg_fmt, *args_fmt, **kwargs)


async def log_to_audit(audit_event: str, data: dict, pool=None) -> None:
    """
    Helper para guardar un evento con trace_id en audit_log.
    Incluye el trace_id automáticamente si está disponible.
    """
    from db.connection import get_pool as _get_pool
    import time

    if pool is None:
        pool = _get_pool()

    trace = get_trace_id() or "no-trace"
    enriched = {
        "trace_id": trace,
        "event": audit_event,
        "data": data,
    }

    try:
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1, $2, $3)",
            f"trace_{trace}",
            audit_event,
            json.dumps(enriched),
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "[trace_id=%s] No se pudo guardar audit_log: %s", trace, exc
        )

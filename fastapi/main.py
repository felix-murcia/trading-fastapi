import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from config import settings
from db.connection import init_pool, close_pool
from routers import orders, smc, journal
from routers.deps import verify_token
from services.performance_metrics import get_metrics, reset_metrics
from services.risk_state_store import init_risk_state
from services.structured_logging import StructuredLoggingMiddleware, log_to_audit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

_mcp_status: dict = {"connected": False, "last_check": None, "detail": "pending"}
# Tareas de fondo con referencia viva (P0-6/P3-2): sin referencia el task puede
# recolectarse y sus excepciones mueren en silencio; se cancelan en shutdown.
_bg_tasks: set[asyncio.Task] = set()


async def _mcp_health_loop():
    was_connected = None
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(settings.mt5_http_url + "/api/v1/account/info")
                r.raise_for_status()
                data = r.json()
            connected = "balance" in data
            _mcp_status["connected"] = connected
            _mcp_status["detail"] = f"equity={data.get('equity')}" if connected else data.get("detail", "unknown")
        except Exception as exc:
            connected = False
            _mcp_status["connected"] = False
            _mcp_status["detail"] = str(exc)

        if connected != was_connected:
            if connected:
                logger.warning("[MCP-HEALTH] MT5 CONNECTED — %s", _mcp_status["detail"])
            else:
                logger.error("[MCP-HEALTH] MT5 DISCONNECTED — %s", _mcp_status["detail"])
            was_connected = connected

        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    logger.info("PostgreSQL pool inicializado")

    # Opción 5: inicializar tabla de journal
    try:
        from services.trade_journal import init_journal_table
        await init_journal_table()
    except Exception as exc:
        logger.warning("[JOURNAL] init_journal_table falló: %s", exc)

    # P3-1d: UN guardián POR PAR habilitado — se crea ANTES del baseline del
    # día (un par sin guardián = sin protección diaria). Fuera del try: una
    # PAIRS inválida debe impedir el arranque, no dejar guardianes a medias.
    from core.container import build_registry_from_settings
    from services.risk_guardian import get_risk_guardian
    for _ctx in build_registry_from_settings().enabled():
        get_risk_guardian(_ctx.config.symbol)

    # P2-4: estado de riesgo persistente — RESTAURA el baseline del día (un
    # reinicio a mitad de día no borra la pérdida ya sufrida ni el peak) o
    # rueda si el día venció. Fuera del try: un fallo de BD debe impedir el
    # arranque (sin protección de riesgo no hay operativa). El fetch de equity
    # sí puede fallar (MCP caído): entonces init(None) deja que el primer
    # observe() del día fije el baseline y el primer flush lo persista.
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(settings.mt5_http_url + "/api/v1/account/info")
            r.raise_for_status()
            data = r.json()
            equity = data.get("equity", data.get("balance", 0)) if ("balance" in data or "equity" in data) else 0
        boot_equity = float(equity) if equity and float(equity) > 0 else None
    except Exception as exc:
        logger.warning("[RISK-STATE] No se pudo leer equity para el baseline: %s", exc)
        boot_equity = None
    try:
        await init_risk_state(boot_equity)
    except RuntimeError as exc:
        # Solo alcanzable con el pool sin inicializar (suite sin BD): en
        # producción init_pool() ya habría reventado el arranque antes, y un
        # fallo REAL de query (asyncpg) sí propaga y detiene el boot.
        logger.warning("[RISK-STATE] Sin pool: estado de riesgo no restaurado (%s)", exc)

    _bg_tasks.add(asyncio.create_task(_mcp_health_loop()))
    # P3-2b: backup programado del bundle (primera pasada a los 60 s).
    from services.model_backup import _model_backup_loop
    _bg_tasks.add(asyncio.create_task(_model_backup_loop()))
    # P3-2c: drift programado (PSI + decay; primera pasada a los 300 s).
    from services.drift_monitor import _drift_monitor_loop
    _bg_tasks.add(asyncio.create_task(_drift_monitor_loop()))
    # P3-2d: calendario de retrain programado (revisión horaria).
    from services.auto_retrain import _retrain_schedule_loop
    _bg_tasks.add(asyncio.create_task(_retrain_schedule_loop()))
    yield
    for _task in _bg_tasks:
        _task.cancel()
    await close_pool()
    logger.info("Shutdown completo")


app = FastAPI(
    title="Trading API",
    version="5.0.0",
    docs_url="/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(StructuredLoggingMiddleware)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.error("422 %s — body: %s — errors: %s", request.url.path, body.decode()[:500], exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/api/v1/metrics")
async def metrics_endpoint():
    """Exposición de métricas de rendimiento."""
    return get_metrics()


@app.post("/api/v1/metrics/reset")
async def reset_metrics_endpoint():
    """Resetea métricas (útil entre sesiones de testing)."""
    reset_metrics()
    logger.warning("[METRICS] Reseteadas por request")
    return {"status": "reset"}


from routers import ai
app.include_router(ai.router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(journal.router)
app.include_router(smc.router)
app.include_router(orders.router)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok" if _mcp_status["connected"] else "degraded",
        "mcp": _mcp_status,
    }

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
from services.order_queue import get_pending_orders, mark_order_processing, mark_order_placed, mark_order_failed
from services.mt5_client import place_order
from services.performance_metrics import get_metrics, reset_metrics
from services.risk_guardian import set_daily_start
from services.structured_logging import StructuredLoggingMiddleware, log_to_audit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

_mcp_status: dict = {"connected": False, "last_check": None, "detail": "pending"}
_queue_task: asyncio.Task | None = None


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


async def _order_queue_processor():
    """
    Background task que reprocesa órdenes pendientes cuando MT5 está disponible.
    Se activa cada 60s y también cuando MT5 pasa de desconectado a conectado.
    """
    while True:
        try:
            if not _mcp_status["connected"]:
                await asyncio.sleep(30)
                continue

            pending = await get_pending_orders(limit=10)
            if not pending:
                await asyncio.sleep(60)
                continue

            logger.info("[ORDER-QUEUE] Procesando %d órdenes pendientes", len(pending))
            for order in pending:
                order_id = order["id"]
                await mark_order_processing(order_id)
                try:
                    result = await place_order(
                        symbol=order["symbol"],
                        order_type=order["order_type"],
                        volume=float(order["volume"]),
                        price=float(order["entry_price"]),
                        sl=float(order["stop_loss"]),
                        tp=float(order["take_profit"]),
                        comment=order.get("comment", ""),
                    )
                    ticket = result.get("ticket", "")
                    await mark_order_placed(order_id, str(ticket))
                except httpx.HTTPStatusError as exc:
                    error_msg = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                    await mark_order_failed(order_id, error_msg)
                except Exception as exc:
                    await mark_order_failed(order_id, str(exc)[:200])

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[ORDER-QUEUE] Error en processor: %s", exc)
            await asyncio.sleep(60)


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

    # Inicializar Risk Guardian con equity actual
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(settings.mt5_http_url + "/api/v1/account/info")
            r.raise_for_status()
            data = r.json()
            if "balance" in data or "equity" in data:
                equity = data.get("equity", data.get("balance", 0))
                set_daily_start(equity)
    except Exception as exc:
        logger.warning("[RISK-GUARDIAN] No se pudo inicializar equity: %s", exc)

    global _queue_task
    asyncio.create_task(_mcp_health_loop())
    _queue_task = asyncio.create_task(_order_queue_processor())
    yield
    _queue_task.cancel()
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

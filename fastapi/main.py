import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from config import settings
from db.connection import init_pool, close_pool
from routers import orders, smc
from routers.deps import verify_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_mcp_status: dict = {"connected": False, "last_check": None, "detail": "pending"}


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
    task = asyncio.create_task(_mcp_health_loop())
    yield
    task.cancel()
    await close_pool()
    logger.info("Shutdown completo")


app = FastAPI(
    title="Trading API",
    version="5.0.0",
    docs_url="/docs",
    lifespan=lifespan,
)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.error("422 %s — body: %s — errors: %s", request.url.path, body.decode()[:500], exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


app.include_router(smc.router)
app.include_router(orders.router)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok" if _mcp_status["connected"] else "degraded",
        "mcp": _mcp_status,
    }

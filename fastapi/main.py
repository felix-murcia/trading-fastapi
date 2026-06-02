import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from db.connection import init_pool, close_pool
from services import mt5_client
from routers import context, analysis, risk, orders, market, llm, test_simulate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    logger.info("PostgreSQL pool inicializado")
    yield
    await close_pool()
    logger.info("Shutdown completo")


app = FastAPI(
    title="Forex Trading API",
    version="4.0.0",
    docs_url="/docs",
    lifespan=lifespan,
)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.error("422 %s — body: %s — errors: %s", request.url.path, body.decode()[:500], exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


app.include_router(context.router)
app.include_router(analysis.router)
app.include_router(risk.router)
app.include_router(orders.router)
app.include_router(market.router)
app.include_router(llm.router)
app.include_router(test_simulate.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}

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
from api.routers import signals
from services.performance_metrics import get_metrics, reset_metrics
from services.broker_clock_store import restore_broker_offset
from services.risk_state_store import init_risk_state
from services.structured_logging import StructuredLoggingMiddleware
from services.error_taxonomy import DB_ERRORS, HTTP_ERRORS, LOOP_ERRORS, PARSE_ERRORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

_mcp_status: dict = {"connected": False, "last_check": None, "detail": "pending"}
# Tareas de fondo con referencia viva (P0-6/P3-2): sin referencia el task puede
# recolectarse y sus excepciones mueren en silencio; se cancelan en shutdown.
_bg_tasks: set[asyncio.Task] = set()
_risk_state_ready = False


async def _persist_validated_broker_offset() -> None:
    from services.timebase import (
        broker_offset_is_persisted,
        broker_offset_is_validated,
        broker_offset_source,
        current_broker_offset,
    )
    if not broker_offset_is_validated() or broker_offset_is_persisted():
        return
    try:
        from services.broker_clock_store import persist_broker_offset
        await persist_broker_offset(
            current_broker_offset(), broker_offset_source()
        )
    except DB_ERRORS + (RuntimeError,):
        logger.exception("[TIMEBASE] No se pudo persistir el offset validado")


async def _recover_broker_clock_and_risk(data: dict, market_port) -> None:
    global _risk_state_ready
    from services.timebase import (
        broker_clock_ready,
        broker_offset_hours,
        broker_offset_is_validated,
    )

    if not broker_offset_is_validated():
        try:
            await broker_offset_hours("EURUSD", market_port)
        except LOOP_ERRORS as exc:
            logger.warning(
                "[TIMEBASE] No se pudo refrescar el offset del bróquer: %s",
                exc,
            )
    await _persist_validated_broker_offset()
    if not broker_clock_ready() or _risk_state_ready:
        return

    try:
        equity = data.get("equity", data.get("balance"))
        current_equity = float(equity) if equity and float(equity) > 0 else None
        await init_risk_state(current_equity)
        _risk_state_ready = True
    except DB_ERRORS + (RuntimeError,):
        logger.exception(
            "[RISK-STATE] No se pudo inicializar tras validar el offset; "
            "se reintentará"
        )


async def _mcp_health_loop(market_port) -> None:
    was_connected = None
    fallos_consecutivos = 0
    alerta_caida_emitida = False
    while True:
        try:
            data = await market_port.get_account()
            connected = "balance" in data
            _mcp_status["connected"] = connected
            _mcp_status["detail"] = f"equity={data.get('equity')}" if connected else data.get("detail", "unknown")
            if connected:
                await _recover_broker_clock_and_risk(data, market_port)
        except LOOP_ERRORS as exc:
            connected = False
            _mcp_status["connected"] = False
            _mcp_status["detail"] = str(exc)

        if connected:
            fallos_consecutivos = 0
        else:
            fallos_consecutivos += 1

        if connected != was_connected:
            if connected:
                logger.warning("[MCP-HEALTH] MT5 CONNECTED — %s", _mcp_status["detail"])
            else:
                logger.error("[MCP-HEALTH] MT5 DISCONNECTED — %s", _mcp_status["detail"])
            # T-31.9 (2026-10-20): la caída del servidor MT5 (el terminal que
            # hospeda el EA y el MCP) era un log invisible para el operador:
            # el sistema queda en HOLD (fail-safe) y se recupera solo, pero
            # nadie se enteraba de que la operativa estaba en pausa. Ahora
            # hay UNA alerta clara (caída) y otra al volver.
            # En un ARRANQUE en frío con el MT5 ya caído no se avisa al
            # primer chequeo (puede ser un arranque normal): se avisa tras 3
            # comprobaciones seguidas sin respuesta (90 s).
            avisar_caida = (not connected) and (
                was_connected is True or (was_connected is None and fallos_consecutivos >= 3)
            )
            if avisar_caida and not alerta_caida_emitida:
                alerta_caida_emitida = True
                try:
                    from services.alerting import AlertLevel, send_alert
                    send_alert(
                        AlertLevel.ERROR, "mcp_health",
                        "Servidor MT5 SIN RESPUESTA — operativa en pausa por "
                        "seguridad (HOLD, sin nuevas entradas). Es el terminal "
                        "que hospeda el EA y el MCP: hay que relanzarlo. El "
                        "backend sigue vivo y reanuda solo cuando vuelva.",
                        context={"url": settings.mt5_http_url,
                                 "detail": str(_mcp_status["detail"])[:200]},
                    )
                except HTTP_ERRORS as exc:  # noqa: BLE001 — la alerta nunca tumba el bucle
                    logger.warning("[MCP-HEALTH] no se pudo emitir la alerta: %s", exc)
            if connected and was_connected is False:
                try:
                    from services.alerting import AlertLevel, send_alert
                    send_alert(
                        AlertLevel.WARNING, "mcp_health",
                        "Servidor MT5 CONECTADO de nuevo — la operativa se reanuda "
                        f"({_mcp_status['detail']})",
                    )
                except HTTP_ERRORS as exc:  # noqa: BLE001
                    logger.warning("[MCP-HEALTH] no se pudo emitir la alerta: %s", exc)
            was_connected = connected

        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _risk_state_ready
    _risk_state_ready = False
    await init_pool()
    logger.info("PostgreSQL pool inicializado")

    # Opción 5: inicializar tabla de journal
    try:
        from services.trade_journal import init_journal_table
        await init_journal_table()
    except DB_ERRORS as exc:
        logger.warning("[JOURNAL] init_journal_table falló: %s", exc)

    # Curva de equity mark-to-market (2026-10-21): el DD deja de ser una
    # cifra narrada. La tabla se crea de forma idempotente (schema.sql sólo
    # corre en un volumen nuevo).
    try:
        from services.equity_curve import init_equity_table
        await init_equity_table()
    except DB_ERRORS as exc:
        logger.warning("[EQUITY] init_equity_table falló: %s", exc)

    # P3-1d: UN guardián POR PAR habilitado — se crea ANTES del baseline del
    # día (un par sin guardián = sin protección diaria). Fuera del try: una
    # PAIRS inválida debe impedir el arranque, no dejar guardianes a medias.
    # Un mismo registro sirve para guardianes, el health loop y el equity de
    # arranque: el broker se resuelve por configuración (settings.broker_type),
    # nunca por imports directos de services.mt5_client.
    from core.container import build_registry_from_settings
    from services.risk_guardian import get_risk_guardian
    registry = build_registry_from_settings()
    market_port = registry.get("EURUSD").market
    if market_port is None:
        enabled = registry.enabled()
        if not enabled:
            raise ValueError("PAIRS: ningún par habilitado para el health loop")
        market_port = enabled[0].market
    for _ctx in registry.enabled():
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
            data = r.json() if r.status_code == 200 else {}
            equity = data.get("equity", data.get("balance", 0)) if ("balance" in data or "equity" in data) else 0
        boot_equity = float(equity) if equity and float(equity) > 0 else None
    # MCP caído o payload raro al arrancar: baseline en el primer observe().
    except (HTTP_ERRORS + PARSE_ERRORS) as exc:
        logger.warning("[RISK-STATE] No se pudo leer equity para el baseline: %s", exc)
        boot_equity = None
    await _initialize_clock_and_risk(boot_equity, market_port)

    _bg_tasks.add(asyncio.create_task(_mcp_health_loop(market_port)))
    # P3-2b: backup programado del bundle (primera pasada a los 60 s).
    from services.model_backup import _model_backup_loop
    _bg_tasks.add(asyncio.create_task(_model_backup_loop()))
    # P3-2c: drift programado (PSI + decay; primera pasada a los 300 s).
    from services.drift_monitor import _drift_monitor_loop
    _bg_tasks.add(asyncio.create_task(_drift_monitor_loop()))
    # P3-2d: calendario de retrain programado (revisión horaria).
    from services.auto_retrain import _retrain_schedule_loop
    _bg_tasks.add(asyncio.create_task(_retrain_schedule_loop()))
    # P2-H14: watchdog de silencio de ai_predict_cycle (cada 5 min).
    from services.predict_silence import _predict_silence_loop
    _bg_tasks.add(asyncio.create_task(_predict_silence_loop()))
    # T-31 (2026-10-19): autoridad del veto direccional del LLM — se mide en
    # vivo y se refresca por par (TTL 30 min en la propia función). El veto
    # sólo revierte si el juez ha demostrado skill; si no, queda inerte y la
    # señal pasa (los vetoes mecánicos siguen intactos).
    from core.container import build_registry_from_settings as _build_registry
    from services.qwen_authority import refresh_loop as _qwen_auth_refresh
    for _ctx in _build_registry().enabled():
        _bg_tasks.add(asyncio.create_task(_qwen_auth_refresh(_ctx.config.symbol)))
    # P2 (orden Félix 2026-10-20): auditoría cruzada — recomputa
    # INDEPENDIENTEMENTE los números publicados y avisa si discrepan. Es lo
    # que habría detectado en minutos los 4 fallos de medición del 20-10
    # (un error del MEDIDOR, no del modelo).
    from services.cross_audit import _cross_audit_loop
    _bg_tasks.add(asyncio.create_task(_cross_audit_loop()))
    # REGISTRO DIARIO INMUTABLE (append-only, versionado en git): los números
    # del sistema los escribe el propio sistema y quedan en el historial —
    # para que "es cierto" se compruebe con el fichero, no con quien narra.
    from services.daily_ledger import _daily_ledger_loop
    _bg_tasks.add(asyncio.create_task(_daily_ledger_loop()))
    # CURVA DE EQUITY MARK-TO-MARKET (2026-10-21): una foto por minuto con
    # equity/balance/flotante. Es la serie que permite reproducir el DD real
    # (el 30,2 % del 20-10 era mark-to-market y no tenía serie). Sólo mide.
    from services.equity_curve import _equity_curve_loop
    _bg_tasks.add(asyncio.create_task(_equity_curve_loop()))
    # P4 (orden Félix 2026-10-20): replay del LLM como decisor con la
    # MECÁNICA REAL (SL/TP, sizing, spread, regla 3 €, churn) → su
    # expectancy, que es la vara de admisión. Sólo lectura.
    from services.llm_replay import _llm_replay_loop
    _bg_tasks.add(asyncio.create_task(_llm_replay_loop()))

    # Carga de artefactos de inferencia por par (antes era on_startup en routers/ai.py).
    from api.routers.signals import load_inference_artifacts
    from api.routers._shared import _get_registry
    for _ctx in _get_registry().enabled():
        if _ctx.config.decision_source != "ppo":
            logger.info(
                "[AI-STARTUP] %s: decisor por REGLA (%s) — sin bundle PPO",
                _ctx.config.symbol, _ctx.config.decision_source,
            )
            continue
        load_inference_artifacts(_ctx.config.symbol)

    yield
    for _task in _bg_tasks:
        _task.cancel()
    await close_pool()
    logger.info("Shutdown completo")


async def _initialize_clock_and_risk(boot_equity: float | None, market_port) -> None:
    global _risk_state_ready
    from services.timebase import (
        broker_clock_ready,
        broker_offset_hours,
        broker_offset_is_validated,
    )

    try:
        if await restore_broker_offset():
            logger.info("[TIMEBASE] Offset validado restaurado desde PostgreSQL")
    except DB_ERRORS + (RuntimeError,):
        logger.exception("[TIMEBASE] No se pudo restaurar el offset persistido")

    try:
        offset_h = await broker_offset_hours("EURUSD", market_port)
        if broker_offset_is_validated():
            logger.info(
                "[TIMEBASE] Offset validado disponible al arrancar: %+d h",
                offset_h,
            )
        else:
            logger.error(
                "[TIMEBASE] Offset desconocido: no hay medición fresca ni "
                "valor persistido; se bloquean nuevas órdenes"
            )
    except LOOP_ERRORS as exc:
        logger.warning(
            "[TIMEBASE] No se pudo detectar el desfase del bróquer: %s", exc
        )
    await _persist_validated_broker_offset()

    if not broker_clock_ready():
        logger.error(
            "[RISK-STATE] No se inicializa el baseline del día: el offset "
            "del bróquer no está validado y persistido; trading bloqueado"
        )
        return
    try:
        await init_risk_state(boot_equity)
        _risk_state_ready = True
    except RuntimeError as exc:
        logger.warning("[RISK-STATE] Sin pool: estado de riesgo no restaurado (%s)", exc)


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


from api.routers import health
from api.routers import model
from api.routers import sessions
from api.routers import signals
from api.routers import trades
from api.routers import retrain

app.include_router(signals.router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(health.router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(model.router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(sessions.router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(trades.router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(retrain.router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(journal.router)
app.include_router(smc.router)
app.include_router(orders.router)


@app.get("/health")
async def health() -> dict:
    from services.timebase import (
        broker_clock_ready,
        broker_offset_is_validated,
        broker_offset_is_persisted,
        current_broker_offset,
    )
    clock_ready = broker_clock_ready()
    return {
        "status": "ok" if _mcp_status["connected"] and clock_ready else "degraded",
        "mcp": _mcp_status,
        "broker_clock": {
            "validated": broker_offset_is_validated(),
            "persisted": broker_offset_is_persisted(),
            "offset_h": current_broker_offset() if broker_offset_is_validated() else None,
        },
    }

"""Subrouter: completion de trades (riesgo medio-alto — tiene flag USE_NEW_RECORD_TRADE_FILL).

Endpoints:
  POST /trade/filled — webhook del EA cuando una orden se cierra

El path público NO cambia: prefix="/api/v1/ai" en main.py.
El flag USE_NEW_RECORD_TRADE_FILL se mueve CON el endpoint (if + import juntos).
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError
from services.auto_retrain import (
    TradeOutcomeLinkConflict,
    _parse_timestamp,
    record_trade_filled,
)
from services.error_taxonomy import HTTP_ERRORS, PARSE_ERRORS
from services.order_manager import mark_order_filled
from services.position_sizing import PIP_SIZE
from services.structured_logging import log_to_audit
from src.application.use_cases.record_trade_fill import (
    RecordTradeFill,
    RecordTradeFillInput,
)
from src.domain.value_objects.thresholds import TradingThresholds

from config import settings
from .._shared import _QUOTE_CACHE, DIRECTION_CANONICAL, VALID_EXIT_REASONS
from .deps import verify_token

router = APIRouter()

logger = logging.getLogger(__name__)


class TradeFilledRequest(BaseModel):
    symbol: str
    entry_time: float | str  # Unix timestamp (float) or numeric string
    exit_time: float | str   # Unix timestamp (float) or numeric string
    pnl: float | str
    pnl_pct: float | str
    direction: str   # "LONG", "SHORT", "BUY", "SELL" (case-insensitive)
    sl_hit: bool
    tp_hit: bool
    exit_reason: str  # "sl", "tp", "manual", "news", "signal_end", "ea"
    deal_ticket: int | str | None = None
    position_id: int | str | None = None
    order_db_id: int | None = None
    opening_order_ticket: int | None = None
    # P1.2b (2026-10-25): identifica el ciclo de decisión para vincular el
    # cierre con su quote y calcular slippage. `None` = EA antiguo.
    cycle_id: str | None = None
    # P1.2b (2026-10-25): precio de fill de apertura y costes del bróker.
    # `None` = EA antiguo que no los envía ⇒ valores por defecto (declarado en
    # el audit).
    entry_price: float | str | None = None
    commission: float | str | None = None
    fee: float | str | None = None
    # G3-2 (2026-10-21): el SWAP del cierre (financiación por mantener la
    # posición abierta). Antes NO llegaba al backend ⇒ el pnl registrado era
    # el bruto del bróker y el coste de la financiación se perdía en todas las
    # métricas (ledger, win rate, expectativa). Medido: −0,16 € en la
    # operación 76 (EURUSD, 7,3 h) y −1,12 € acumulados en el histórico.
    # `None` = EA antiguo que no lo envía ⇒ 0 (declarado en el audit).
    swap: float | str | None = None

    @field_validator("direction")
    @classmethod
    def _canonical_direction(cls, v: str) -> str:
        canonical = DIRECTION_CANONICAL.get(str(v).strip().upper())
        if canonical is None:
            # PydanticCustomError en vez de ValueError: pydantic mete el
            # ValueError original en errors()['ctx'] y FastAPI no puede
            # serializarlo a JSON → el 422 se rompería en un 500.
            raise PydanticCustomError(
                "value_error.direction",
                f"direction must be one of {sorted(DIRECTION_CANONICAL)} (got {v!r})",
            )
        return canonical

    @field_validator("exit_reason")
    @classmethod
    def _valid_exit_reason(cls, v: str) -> str:
        reason = str(v).strip().lower()
        if reason not in VALID_EXIT_REASONS:
            raise PydanticCustomError(
                "value_error.exit_reason",
                f"exit_reason must be one of {sorted(VALID_EXIT_REASONS)} (got {v!r})",
            )
        return reason

    @field_validator("order_db_id")
    @classmethod
    def _positive_order_db_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise PydanticCustomError(
                "value_error.order_db_id",
                "order_db_id must be a positive integer",
            )
        return value


async def _mark_trade_order_filled(
    req: TradeFilledRequest,
    direction: str,
    position_id: int | None,
) -> None:
    await mark_order_filled(
        req.symbol,
        "BUY" if direction == "LONG" else "SELL",
        order_db_id=req.order_db_id,
        mt5_order_id=req.opening_order_ticket,
        position_id=position_id,
    )


async def _record_and_mark_trade_filled(
    req: TradeFilledRequest,
    entry_time: str,
    exit_time: str,
    pnl: float,
    pnl_pct: float,
    direction: str,
    deal_ticket: int | None,
    position_id: int | None,
    # P1.2b: costes de ejecución del cierre (opcionales; EA antiguo no los envía).
    entry_price: float | None = None,
    commission: float | None = None,
    fee: float | None = None,
    slippage_pips: float | None = None,
    quote_bid: float | None = None,
    quote_ask: float | None = None,
    quote_time: Optional[datetime] = None,
) -> None:
    try:
        await record_trade_filled(
            symbol=req.symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            pnl=pnl,
            pnl_pct=pnl_pct,
            direction=direction,
            sl_hit=req.sl_hit,
            tp_hit=req.tp_hit,
            exit_reason=req.exit_reason,
            deal_ticket=deal_ticket,
            position_id=position_id,
            order_db_id=req.order_db_id,
            # P1.2b: costes de ejecución.
            entry_price=entry_price,
            commission=commission,
            fee=fee,
            slippage_pips=slippage_pips,
            quote_bid=quote_bid,
            quote_ask=quote_ask,
            quote_time=quote_time,
        )
    except TradeOutcomeLinkConflict as exc:
        logger.error("[TRADE-FILLED] conflicto de enlace de cierre: %s", exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _mark_trade_order_filled(req, direction, position_id)


@router.post("/trade/filled")
async def trade_filled_webhook(req: TradeFilledRequest, _: None = Depends(verify_token)):
    """
    Webhook que el MT5 EA llama cuando una orden se cierra.
    Registra el trade y dispara auto-retrain si corresponde.

    DOMINIO DE LAS MARCAS (T-31.9, 2026-10-20): `entry_time` y `exit_time`
    llegan del EA en el **reloj del BRÓKER** (medido: el `mt5_time` del EA va
    +3 h respecto al `created_at` de la auditoría, que es UTC real). Se
    guardan TAL CUAL (la marca en bruto, sin(false) convertir) y quien las
    agrupe por día o las compare con velas debe convertirlas primero
    (`services.timebase.broker_stamp_to_utc` / `to_utc`).
    """
    if settings.USE_NEW_RECORD_TRADE_FILL:
        # Camino nuevo: caso de uso aislado en src/application/ (paridad byte a byte con legacy)
        from decimal import Decimal
        from src.application.use_cases.record_trade_fill import RecordTradeFill, RecordTradeFillInput
        from src.domain.value_objects.thresholds import TradingThresholds
        uc = RecordTradeFill(
            repo=None, clock=None, audit=None,
            thresholds=TradingThresholds(
                min_win_eur=Decimal("3.0"),
                safe_lock_eur=Decimal(str(settings.min_lock_eur)),
                cost_buffer_pips=2,
            ),
        )
        _result = uc.execute(RecordTradeFillInput(
            cycle_id=req.cycle_id,
            symbol=req.symbol,
            direction=req.direction,
            entry_price=float(req.entry_price) if req.entry_price not in (None, "") else None,
            exit_price=float(req.exit_price) if req.exit_price not in (None, "") else None,
            pnl=float(req.pnl) if isinstance(req.pnl, str) else req.pnl,
            pnl_pct=float(req.pnl_pct) if isinstance(req.pnl_pct, str) else req.pnl_pct,
            deal_ticket=int(req.deal_ticket) if req.deal_ticket not in (None, "") else None,
            position_id=int(req.position_id) if req.position_id not in (None, "") else None,
            entry_time=req.entry_time,
            exit_time=req.exit_time,
            mt5_order_id=int(req.opening_order_ticket) if req.opening_order_ticket not in (None, "") else None,
        ))
        return {"status": "recorded" if _result.persisted else "error"}
    else:
        # Camino legacy (byte-idéntico al comportamiento previo)
        from services.auto_retrain import _parse_timestamp
    entry_ts = _parse_timestamp(req.entry_time)
    exit_ts = _parse_timestamp(req.exit_time)

    # El EA (OnTradeTransaction) reporta también el deal de APERTURA:
    # DEAL_ENTRY_IN tiene beneficio 0 por definición y entry==exit (no hay
    # duración). No es un cierre → no se fabrican timestamps ni se persiste.
    # Fix raíz en EA v13.2 (filtra DEAL_ENTRY_IN); este guard protege la BD
    # mientras la versión compilada siga siendo anterior.
    if entry_ts == exit_ts and float(req.pnl) == 0.0 and float(req.pnl_pct) == 0.0:
        logger.warning(
            "[TRADE-FILLED] Reporte de APERTURA ignorado (entry==exit, pnl=0): "
            "symbol=%s entry=%s deal=%s position=%s",
            req.symbol, entry_ts, req.deal_ticket, req.position_id,
        )
        # O-1: la apertura confirma la ejecución → la orden sale del pool de
        # `duplicate_order` (pending/placed → filled). Si este reporte se
        # pierde, la rama de cierre hace la misma transición (idempotente).
        await _mark_trade_order_filled(
            req,
            req.direction,
            int(req.position_id) if req.position_id not in (None, "") else None,
        )
        return {"status": "ignored_open_deal"}

    minimum_timestamp = 946684800.0  # 2000-01-01 UTC
    if entry_ts == 0:
        entry_ts = exit_ts - 1
    if exit_ts == 0:
        exit_ts = entry_ts + 1
    if entry_ts == exit_ts:
        exit_ts = entry_ts + 1
    if entry_ts < minimum_timestamp or exit_ts < minimum_timestamp:
        raise HTTPException(status_code=422, detail="entry_time/exit_time must be >= 2000-01-01 UTC")
    if exit_ts <= entry_ts:
        raise HTTPException(status_code=422, detail="exit_time must be greater than entry_time")
    entry_time_out = str(int(entry_ts))
    exit_time_out = str(int(exit_ts))

    logger.info("[TRADE-FILLED] payload symbol=%s entry=%s exit=%s pnl=%.2f pct=%.4f dir=%s reason=%s",
                req.symbol, entry_time_out, exit_time_out, req.pnl, req.pnl_pct, req.direction, req.exit_reason)
    direction_map = {"BUY": "LONG", "SELL": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}
    direction = direction_map.get(req.direction.upper()) if req.direction else req.direction
    pnl = float(req.pnl) if isinstance(req.pnl, str) else req.pnl
    pnl_pct = float(req.pnl_pct) if isinstance(req.pnl_pct, str) else req.pnl_pct
    deal_ticket = int(req.deal_ticket) if req.deal_ticket not in (None, "") else None
    position_id = int(req.position_id) if req.position_id not in (None, "") else None
    swap = float(req.swap) if req.swap not in (None, "") else 0.0
    # P1.2b: costes de ejecución del cierre.
    entry_price = (
        float(req.entry_price) if req.entry_price not in (None, "") else None
    )
    commission = (
        float(req.commission) if req.commission not in (None, "") else None
    )
    fee = float(req.fee) if req.fee not in (None, "") else None
    cycle_id = req.cycle_id

    # Validación de sanidad: el PnL de un cierre no puede exceder 1/5 del
    # equity disponible de la cuenta. Un cierre que pierda más es datos
    # corruptos del bróker/EA y debe rechazarse.
    try:
        from core.container import build_registry_from_settings
        registry = build_registry_from_settings()
        market_port = registry.get(req.symbol).market
        if market_port is not None:
            account = await market_port.get_account()
            account_info = account if isinstance(account, dict) else {}
            equity = float(account_info.get("equity", 0.0)) or float(account_info.get("balance", 0.0))
            if equity > 0 and abs(pnl) > equity / 5.0:
                raise HTTPException(
                    status_code=422,
                    detail=f"pnl={pnl:.2f} EUR excede equity/5 ({equity/5.0:.2f} EUR) — posible dato corrupto",
                )
    except HTTPException:
        raise
    except (HTTP_ERRORS + PARSE_ERRORS) as exc:
        # Frontera externa (red MCP / payload ilegible): no bloquear, registrar.
        logger.warning(
            "[TRADE-FILLED] no se pudo consultar equity para validar pnl: %s", exc
        )

    # Calcular slippage: |entry_price - quote_relevante| / pip_size.
    # Para BUY, el quote relevante es el ask (precio al que compras);
    # para SELL, es el bid (precio al que vendes).
    slippage_pips = None
    quote_bid = quote_ask = quote_time_ts = None
    if entry_price is not None and cycle_id is not None:
        quote = _QUOTE_CACHE.get(cycle_id)
        if quote is not None:
            quote_bid = quote.get("bid")
            quote_ask = quote.get("ask")
            quote_time_ts = quote.get("quote_time")
            pip = PIP_SIZE.get(sym, 0.0001)
            if direction == "LONG" and quote.get("ask") is not None:
                slippage_pips = abs(entry_price - quote["ask"]) / pip
            elif direction == "SHORT" and quote.get("bid") is not None:
                slippage_pips = abs(entry_price - quote["bid"]) / pip
            # Purgar el quote usado para no acumular entradas viejas.
            _QUOTE_CACHE.pop(cycle_id, None)

    # El pnl que se registra es el RESULTADO REAL (bruto + swap): el swap es
    # un coste cierto de mantener la posición y esconderlo falsea el ledger.
    pnl_total = pnl + swap
    if swap:
        logger.info("[TRADE-FILLED] swap=%.2f € (bruto %.2f → real %.2f) symbol=%s",
                    swap, pnl, pnl_total, req.symbol)
    if slippage_pips is not None:
        logger.info(
            "[TRADE-FILLED] slippage=%.2f pips (entry=%.5f, quote=%s) symbol=%s",
            slippage_pips, entry_price,
            quote_ask if direction == "LONG" else quote_bid, req.symbol,
        )
    # O-1: catch-all de transición — si el reporte de apertura no llegó (el EA
    # v13.2 filtra DEAL_ENTRY_IN), este cierre saca la orden del pool de
    # `duplicate_order`. Idempotente: si la apertura ya la marcó, no matchea.
    await _record_and_mark_trade_filled(
        req,
        entry_time_out,
        exit_time_out,
        pnl_total,
        pnl_pct,
        direction,
        deal_ticket,
        position_id,
        entry_price=entry_price,
        commission=commission,
        fee=fee,
        slippage_pips=slippage_pips,
        quote_bid=quote_bid,
        quote_ask=quote_ask,
        quote_time=quote_time_ts,
    )
    return {"status": "recorded"}

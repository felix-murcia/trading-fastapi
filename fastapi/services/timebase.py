"""
BASE DE TIEMPOS CANÓNICA (T-31.9, 2026-10-20) — un solo sitio donde vive
la verdad sobre los relojes del sistema.

POR QUÉ EXISTE (Félix, 2026-10-20): *«revisa que todos los timeframes
estén alineados (EA, sistema, aplicación, MT5, broker, etc). Es fundamental
que todas las franjas horarias y UTCs estén alineados. Es lo más básico y
fundamental; si eso no está bien no estamos haciendo nada»*.

MEDICIÓN (2026-10-20, auditoría sobre 200 ciclos):
  · host (NTP), contenedor y Postgres ⇒ los tres en UTC real (offsets < 0,1 s);
  · servidor MT5 (bróquer) ⇒ **+3,00 h** (cambiará a +2,00 h el 25-10-2026,
    el cambio de horario de verano europeo);
  · EA ⇒ usa el reloj del bróquer (su `mt5_time` va +3 h respecto al
    `created_at` de la auditoría, que es UTC real);
  · los sellos de vela (H1/H4/D1) ⇒ también en el reloj del bróquer;
  · el historial de deals (entry/exit de `trade_outcomes`) ⇒ también en el
    reloj del bróquer.

DOMINIOS (dos, y hay que declararlos SIEMPRE):
  · **UTC real** — el reloj del mundo. El backend, la base de datos, la
    auditoría (`created_at`), las etiquetas de sesión y las franjas
    horarias.
  · **reloj del bróker** — el del servidor MT5. Las velas, el EA y el
    historial de deals.

FRANJAS (comprobadas el 2026-10-20):
  · todas las velas caen en el minuto 0 de su hora ⇒ la rejilla horaria del
    bróquer y la del UTC real COINCIDEN (el desfase son horas enteras);
  · los ciclos del EA caen en :00/:05/…/:55 del UTC real;
  · la vela en formación es la hora UTC real en curso (19:00 del bróquer =
    16:00 UTC);
  · ⚠️ el DÍA del bróquer (y el del estado de cuenta) empieza a las 21:00
    UTC, no a medianoche ⇒ el corte diario del riesgo debe seguir al día
    del bróquer (este módulo lo expone: `broker_day_bounds`).

TRAMPA MEDIDA (2026-10-21): el MCP/MT5 etiqueta sus sellos ISO con
`+02:00` mientras el reloj del servidor va **+3,00 h** (comprobado: el
precio del servidor dice 12:41 y el UTC real 09:41, mientras la posición
abierta se etiqueta `2026-10-21T14:35:52+02:00` = 09:35:52 UTC real) ⇒
**la etiqueta de zona horaria que devuelve el bróker NO es su desfase
real** ⇒ nunca se confíe en ella: el desfase se DETECTA (precio del
servidor o vela en formación) y las conversiones pasan por aquí.

REGLAS PARA EL CÓDIGO NUEVO:
  1. todo timestamp que se compares o agrupes declara su dominio;
  2. el desfase del bróquer se DETECTA (nunca se fija): el 25-10 cambia solo;
  3. las conversiones pasan por aquí (`to_utc` / `to_broker`).
"""
import datetime as dt
import logging
import time
from typing import Optional
from services.error_taxonomy import DB_ERRORS, LOOP_ERRORS

logger = logging.getLogger(__name__)

# Caché corta del desfase: se detecta por el reloj del servidor (una llamada
# al endpoint de precio) y se cachea unos minutos (es una constante del
# servidor, salvo el cambio de horario).
_OFFSET_CACHE: dict = {"offset_h": 0, "ts": 0.0}
OFFSET_TTL_S = 300.0

# P1.1a (2026-10-25): una fuente de reloj solo vale si es CONTEMPORÁNEA.
# · cotización del servidor: ≤5 min (el último tick de una sesión cerrada o de
#   un feed congelado no mide el reloj);
# · vela H1 cerrada: ≤2 h (la vela viva es la hora en curso, así que su sello
#   nunca puede ir por detrás; se usa como frontera superior conservadora).
QUOTE_MAX_AGE_S = 300.0
CANDLE_MAX_AGE_S = 2 * 3600.0

# Desfase CONOCIDO (último detectado). Lo consultan los módulos SÍNCRONOS
# (el guardián de riesgo, que no puede esperar a una llamada de red):
# `current_broker_offset()` devuelve el último valor conocido (0 si nunca se
# detectó ⇒ el comportamiento histórico: el día UTC).
_KNOWN_OFFSET: dict = {
    "offset_h": 0, "source": "sin_detecccion",
    "validated": False, "persisted": False,
}


def set_broker_offset(offset_h: int, source: str = "deteccion") -> None:
    """Guarda el desfase conocido (para los consumidores sincrónicos).

    Defensa en profundidad (T-31.9, 2026-10-20): un valor fuera de ±12 h
    es BASURA (así se coló un −19 h desde la serie D1) ⇒ se RECHAZA y se
    conserva el valor anterior. La caché nunca puede quedar envenenada.
    """
    off = int(offset_h)
    if abs(off) > 12:
        logger.error(
            "[TIMEBASE] Desfase IMPLAUSIBLE rechazado (%+d h, fuente=%s) — "
            "se conserva el conocido (%+d h)", off, source,
            int(_KNOWN_OFFSET["offset_h"]),
        )
        return
    if off != int(_KNOWN_OFFSET["offset_h"]):
        logger.warning(
            "[TIMEBASE] Desfase del bróquer: %+d h (antes %+d h, fuente=%s)",
            off, int(_KNOWN_OFFSET["offset_h"]), source,
        )
    _KNOWN_OFFSET["offset_h"] = off
    _KNOWN_OFFSET["source"] = source
    _KNOWN_OFFSET["validated"] = True
    _KNOWN_OFFSET["persisted"] = source.startswith("persisted:")


def current_broker_offset() -> int:
    """Desfase del bróker CONOCIDO (último detectado). 0 si no se detectó."""
    return int(_KNOWN_OFFSET["offset_h"])


def broker_offset_is_validated() -> bool:
    """Indica si el offset actual procede de una medición o estado persistido."""
    return bool(_KNOWN_OFFSET.get(
        "validated", _KNOWN_OFFSET.get("source") != "sin_detecccion"
    ))


def broker_offset_is_persisted() -> bool:
    """Indica si el último offset validado ya sobrevive a un reinicio."""
    return bool(_KNOWN_OFFSET.get("persisted", False))


def broker_clock_ready() -> bool:
    """Trading solo cuando el offset está validado y persistido."""
    return broker_offset_is_validated() and broker_offset_is_persisted()


def broker_offset_source() -> str:
    return str(_KNOWN_OFFSET["source"])


def mark_broker_offset_persisted(offset_h: int) -> None:
    if (
        broker_offset_is_validated()
        and int(offset_h) == current_broker_offset()
    ):
        _KNOWN_OFFSET["persisted"] = True


async def broker_offset_hours(symbol: str = "EURUSD",
                               market_port: Optional[object] = None) -> int:
    """Desfase del reloj del BRÓKER (horas enteras) respecto al UTC real.

    Contrato (P1.1a, 2026-10-25; B6.2, 2026-10-25):

      · `market_port` (obligatorio en código nuevo) provee `get_price(symbol)`
        y `get_candles(symbol, "H1", 3)`; no se consulta httpx directamente.
      · una cotización de más de `QUOTE_MAX_AGE_S` (precio) o `CANDLE_MAX_AGE_S`
        (vela) no es una medición del reloj;
      · si ambas responden y NO coinciden, el dominio de las dos no es el
        mismo ⇒ no se acepta ninguna (no se elige la más cómoda);
      · el valor solo se publica DESPUÉS de persistirse: un fallo de DB no
        valida ni cachea nada y se conserva el último offset conocido.
    """
    now = time.time()
    cached_offset = int(_OFFSET_CACHE["offset_h"])
    if (now - _OFFSET_CACHE["ts"] < OFFSET_TTL_S
            and abs(cached_offset) <= 12):
        return cached_offset

    quote_offset: Optional[int] = None
    try:
        price_payload = await market_port.get_price(symbol)
        quote_offset = _offset_from_price(price_payload, now)
    except LOOP_ERRORS:  # noqa: BLE001 — el desfase NUNCA tumba una operación
        logger.warning("[TIMEBASE] Sin sello de precio para medir el reloj")
        quote_offset = None

    candles_offset: Optional[int] = None
    try:
        raw_candles = await market_port.get_candles(symbol, "H1", 3)
        now_utc = dt.datetime.now(dt.timezone.utc)
        candles_offset = _offset_from_candles(raw_candles, now=now_utc)
    except LOOP_ERRORS:  # noqa: BLE001
        logger.warning("[TIMEBASE] Sin vela H1 para medir el reloj")
        candles_offset = None
    if candles_offset is not None and abs(candles_offset) > 12:
        logger.warning(
            "[TIMEBASE] Desfase H1 implausible (%+d h); se descarta la fuente",
            candles_offset,
        )
        candles_offset = None

    offset_h: Optional[int] = None
    source = ""
    if quote_offset is not None and candles_offset is not None:
        if quote_offset == candles_offset:
            offset_h, source = quote_offset, "endpoint_precio+h1"
        else:
            logger.error(
                "[TIMEBASE] Fuentes de reloj incompatibles (precio %+d h vs "
                "H1 %+d h): no se acepta ninguna; se conserva el último "
                "conocido (%+d h)",
                quote_offset, candles_offset, current_broker_offset(),
            )
    elif quote_offset is not None:
        offset_h, source = quote_offset, "endpoint_precio"
    elif candles_offset is not None:
        offset_h, source = candles_offset, "velas_h1"

    if offset_h is None:
        return current_broker_offset()

    # Persistir PRIMERO: el reloj no se anuncia validado si no sobrevive a un
    # reinicio. Si la DB falla, `_KNOWN_OFFSET` y la caché quedan intactos.
    try:
        from services.broker_clock_store import persist_broker_offset
        await persist_broker_offset(int(offset_h), source)
    except DB_ERRORS:
        logger.exception(
            "[TIMEBASE] No se pudo persistir el offset medido (%+d h, "
            "fuente=%s); no se valida ni se cachea — se conserva el conocido "
            "(%+d h)", offset_h, source, current_broker_offset(),
        )
        return current_broker_offset()

    set_broker_offset(int(offset_h), source=source)
    mark_broker_offset_persisted(int(offset_h))
    _OFFSET_CACHE["offset_h"] = int(offset_h)
    _OFFSET_CACHE["ts"] = now
    return int(offset_h)


def _offset_from_price(price_payload: dict, now: float) -> Optional[int]:
    """Desfase deducido del sello del endpoint de precio (UTC real vs bróker).

    `price_payload` es el dict devuelto por `MarketDataPort.get_price(symbol)`.
    El desfase son horas ENTERAS ⇒ el resto de la división por 3600 s es la
    edad de la cotización. Si ese resto supera `QUOTE_MAX_AGE_S`, la
    cotización no es contemporánea y no mide el reloj del servidor.
    """
    srv = price_payload.get("time") if price_payload else None
    if not srv:
        return None
    if isinstance(srv, (int, float)):
        srv_ts = float(srv)
    else:
        srv_ts = dt.datetime.fromisoformat(
            str(srv).replace("Z", "+00:00")).timestamp()

    delta = srv_ts - now
    offset_h = int(round(delta / 3600.0))
    if abs(offset_h) > 12:
        logger.warning(
            "[TIMEBASE] Timestamp de precio no contemporáneo o implausible "
            "(%+d h); se descarta la fuente", offset_h,
        )
        return None
    age_s = delta - offset_h * 3600.0
    if abs(age_s) > QUOTE_MAX_AGE_S:
        logger.warning(
            "[TIMEBASE] Cotización no fresca (edad=%+.0f s con %+d h): "
            "se descarta la fuente", age_s, offset_h,
        )
        return None
    return offset_h


def _offset_from_candles(candles: list, now: dt.datetime | None = None) -> Optional[int]:
    """Fallback: deducir el desfase de la vela EN FORMACIÓN (aritmética de
    hora, sin el punto medio ⇒ estable en toda la hora).

    `candles` es la lista cruda devuelta por `MarketDataPort.get_candles()`.
    La vela más reciente está sellada en `floor(hora real) + desfase` ⇒ el
    desfase es la diferencia entre su hora y la hora real. Velas que ya no
    representan la hora viva del bróker no son una medición de offset.
    """
    from services.market_time import broker_offset_hours_from_candles

    if not isinstance(candles, list) or not candles:
        return None

    newest = None
    for candle in candles:
        stamp = candle.get("time") if isinstance(candle, dict) else None
        if stamp is None:
            continue
        try:
            if isinstance(stamp, (int, float)):
                parsed = dt.datetime.fromtimestamp(
                    float(stamp), tz=dt.timezone.utc
                )
            else:
                parsed = dt.datetime.fromisoformat(
                    str(stamp).replace("Z", "+00:00")
                )
        except (OverflowError, OSError, ValueError):
            continue
        if parsed.tzinfo is None:
            continue
        parsed = parsed.astimezone(dt.timezone.utc)
        if newest is None or parsed > newest:
            newest = parsed

    if newest is None:
        return None

    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    age_seconds = (now - newest).total_seconds()
    if age_seconds > CANDLE_MAX_AGE_S or age_seconds < -12 * 3600:
        logger.warning(
            "[TIMEBASE] Se descarta la vela H1 no contemporánea "
            "(edad=%+.1f h)", age_seconds / 3600,
        )
        return None

    return int(broker_offset_hours_from_candles(candles, now=now))


def to_utc(ts_broker: float, offset_h: int) -> float:
    """Sello del bróquer → UTC real."""
    return float(ts_broker) - float(offset_h) * 3600.0


def to_broker(ts_utc: float, offset_h: int) -> float:
    """UTC real → sello del bróker."""
    return float(ts_utc) + float(offset_h) * 3600.0


def broker_day_bounds(now_utc: Optional[dt.datetime] = None,
                       offset_h: int = 0) -> tuple[dt.datetime, dt.datetime]:
    """Límites del DÍA DEL BRÓKER en UTC real: [medianoche del bróker,
    +24 h). El día del bróquer empieza a las 21:00 UTC con un desfase de +3 h
    (a las 22:00 UTC con +2 h, tras el cambio de horario del 25-10-2026).

    Es la ventana que debe usar el estado de cuenta y el límite de pérdida
    diaria: el día del bróquer es el día del extracto, no el día civil UTC.
    """
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    # La medianoche del bróquer, expresada en UTC: el instante en que el
    # reloj del bróquer marca las 00:00 ⇒ (medianoche local − desfase).
    broker_now = now + dt.timedelta(hours=offset_h)
    broker_midnight = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = broker_midnight - dt.timedelta(hours=offset_h)
    return start_utc, start_utc + dt.timedelta(days=1)


def broker_day_key(now_utc: Optional[dt.datetime] = None,
                    offset_h: int = 0) -> str:
    """Clave de día del bróquer (YYYY-MM-DD en el reloj del bróquer)."""
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    broker_now = now + dt.timedelta(hours=offset_h)
    return broker_now.strftime("%Y-%m-%d")


def utc_day_key(now_utc: Optional[dt.datetime] = None) -> str:
    """Clave de día UTC real (para el registro legible y las métricas
    del usuario)."""
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%d")


def broker_today(now_utc: Optional[dt.datetime] = None) -> dt.date:
    """DÍA DEL BRÓKER (el día del extracto de cuenta) como fecha.

    Es la clave que debe usar el estado de riesgo: el límite de pérdida
    diaria y el pico de capital miden un día del BROKER (21:00–21:00 UTC
    con el desfase actual de +3 h; 22:00–22:00 UTC tras el cambio de
    horario del 25-10-2026), no el día civil UTC.

    Si el desfase aún no se ha detectado (MCP caído al arrancar), cae al día
    UTC (comportamiento histórico) — degrada sin romper.
    """
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    off = current_broker_offset()
    return (now + dt.timedelta(hours=off)).date()


def utc_day_bounds(now_utc: Optional[dt.datetime] = None) -> tuple[dt.datetime, dt.datetime]:
    """Límites del día UTC real [00:00, +24 h)."""
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1)


def broker_stamp_to_utc(ts_broker: dt.datetime,
                        offset_h: Optional[int] = None) -> dt.datetime:
    """Sello del bróker (vela, deal) → UTC real.

    `offset_h=None` ⇒ usa el último desfase conocido.
    """
    off = current_broker_offset() if offset_h is None else int(offset_h)
    if ts_broker.tzinfo is None:
        ts_broker = ts_broker.replace(tzinfo=dt.timezone.utc)
    return ts_broker.astimezone(dt.timezone.utc) - dt.timedelta(hours=off)

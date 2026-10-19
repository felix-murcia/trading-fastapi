"""
Conversión de horas de vela al huso verdadero (UTC) para las etiquetas de sesión.

El feed de MT5 devuelve las velas con la hora del SERVIDOR del broker
(GMT+2/GMT+3 según el DST) sellada con sufijo "Z", como si fuera UTC: en
2026-09-29 (DST de verano) la vela formándose marca "12:00:00Z" siendo las
09:00 UTC reales. Clasificar la sesión con esa hora bruta etiqueta "Asia" a
la tarde de Nueva York — las fronteras de sesión son propiedades de UTC, no
del reloj del broker (hallazgo P2-3 de la auditoría: "etiquetas de sesión
erróneas").

`utc_hour_from_candle()` detecta el desfase en cada llamada comparando la
vela formándose con el reloj UTC real: la vela H1 empieza en floor(now) −
offset, así que su marca +30 min cae a offset ±0.5 h de `now` y `round()`
recupera el desfase exacto. Es auto-corrector: si el feed pasa a sellar UTC
real (o una tz correcta) o cambia el DST del broker, la detección lo sigue
sin tocar código.

Sellos sin tz y velas que no son la vela formándose (backtests, fixtures
sintéticas) no tienen reloj de broker a qué comparar: se usa la hora del
sello tal cual (offset 0).
"""

from datetime import datetime, timedelta, timezone

# Horizonte de "vela formándose": más allá, el sello no es el reloj vivo del
# broker y no cabe inferir desfase (datos históricos/sintéticos).
_CONTEMPORANEOUS_H = 36.0


def detect_utc_offset_hours(candle_time: datetime, now: datetime | None = None) -> int:
    """Desfase en horas entre el sello de la vela (interpretado como su propio
    reloj) y UTC verdadero. 0 cuando no aplica (sin tz o no contemporánea).

    El sello se asume YA normalizado a UTC por el llamador: si el feed miente
    (hora broker con "Z"), el desfase detectado es la mentira a corregir; si
    el feed es honesto (UTC o tz correcta), el desfase es ~0.
    """
    if candle_time.tzinfo is None:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    midpoint = candle_time + timedelta(minutes=30)
    delta_h = (midpoint - now).total_seconds() / 3600.0
    if abs(delta_h) > _CONTEMPORANEOUS_H:
        return 0
    return round(delta_h)


def utc_hour_from_candle(candle_time: datetime, now: datetime | None = None) -> int:
    """Hora UTC [0,23] de la vela, corrigiendo el huso del servidor del broker.

    - Sello con tz: se normaliza a UTC y, si es la vela formándose, se corrige
      además el desfase del servidor (sello "Z" con hora broker).
    - Sello sin tz (fixture histórica/sintética): se devuelve tal cual.
    """
    if candle_time.tzinfo is None:
        return int(candle_time.hour) % 24

    ts = candle_time.astimezone(timezone.utc)
    offset = detect_utc_offset_hours(ts, now)
    return (ts.hour - offset) % 24


# ───────────────────────── T-31 (2026-10-19) ─────────────────────────
# El desfase no sólo afectaba a las ETIQUETAS DE SESIÓN: afectaba a los
# SELLOS de todas las velas. Medido en vivo: el servidor MT5 va +3 h
# adelantado (precio 21:56Z vs backend 18:56Z) → las velas estaban selladas
# 3 h en el futuro. Consecuencias medidas:
#   (1) el emparejamiento de los outcomes reales con las velas
#       (ml/trading_env_v2._index_real_outcomes) atribuía el feedback real a
#       la vela ~3 h ANTES de la entrada (2-3 barras desviadas) ⇒ reward del
#       PPO contaminado en el retrain;
#   (2) cualquier medición que mezclara sellos de vela (reloj broker) con
#       marcas del EA/DB (UTC real) quedaba desplazada 3 h (y con huecos de
#       fin de semana, "24 barras" ≠ 24 h).
# El corrector ya existía (P2-3) para las etiquetas de sesión; aquí se
# generaliza a los sellos de vela, con el MISMO mecanismo auto-corrector
# (cero hardcode: si el broker cambia de huso, se sigue solo).


def broker_offset_hours_from_candles(
    candles: list, now: datetime | None = None
) -> int:
    """Desfase del reloj del broker (h) deducido de la vela MÁS NUEVA.

    La vela más reciente es la formándose ⇒ su sello está en el reloj del
    broker ⇒ el desfase se deduce igual que en `detect_utc_offset_hours`.
    Si no hay velas (o ninguna es contemporánea) → 0 (sin corrección).
    """
    if not candles:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    newest = None
    for c in candles:
        t = c.get("time") if isinstance(c, dict) else None
        if t is None:
            continue
        if isinstance(t, (int, float)):
            ts = datetime.fromtimestamp(float(t), tz=timezone.utc)
        else:
            try:
                ts = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            except ValueError:
                continue
        if ts.tzinfo is None:
            continue
        if newest is None or ts > newest:
            newest = ts
    if newest is None:
        return 0
    return detect_utc_offset_hours(newest, now)


def correct_candle_epoch(candle_epoch: float, offset_hours: int) -> float:
    """Pasa un sello de vela (reloj del broker) a UTC verdadero."""
    if not offset_hours:
        return float(candle_epoch)
    return float(candle_epoch) - offset_hours * 3600.0


def shift_candle_times(candles: list, offset_hours: int) -> list:
    """Desplaza los sellos de vela al UTC real CONSERVANDO su formato.

    Las velas del MCP llegan con `time` en ISO-8601 ("...Z") o epoch; el
    consumidor (pandas, el parche del feed, el join H4/D1) depende de ese
    formato, así que la corrección se aplica sobre la marca, no
    re-serializando la vela entera. Sin offset → devuelve la lista intacta.
    """
    if not offset_hours or not candles:
        return candles
    from datetime import timedelta
    shifted = []
    for c in candles:
        if not isinstance(c, dict):
            shifted.append(c)
            continue
        t = c.get("time")
        if isinstance(t, (int, float)):
            out = {**c, "time": correct_candle_epoch(float(t), offset_hours)}
        elif isinstance(t, str):
            try:
                ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                shifted.append(c)
                continue
            if ts.tzinfo is None:
                shifted.append(c)
                continue
            out = {**c, "time": (ts - timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        else:
            out = c
        shifted.append(out)
    return shifted

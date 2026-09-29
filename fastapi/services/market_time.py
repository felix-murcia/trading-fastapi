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

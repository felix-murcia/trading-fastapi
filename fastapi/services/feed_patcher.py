"""Parche del agujero del feed MCP con los CSV de `resultadosBT/`.

Hallazgo 2026-10-12: el feed del MCP corta la historia (agujero medido de
6817h en H1, 2026-01-01→10-11) y sólo entrega el bloque reciente. Tres
consumidores muestrean ese feed:

  - el obs del modelo (routers/ai.py, H1) — parcheado 2026-10-12;
  - el contexto H4/D del prompt (routers/ai.py) — parcheado 2026-10-13
    (antes la guarda de continuidad descartaba la serie a "Unavailable");
  - la ventana PSI del drift monitor (services/drift_monitor.py) — parcheado
    2026-10-13 (sin parche el monitor muestreaba saltos fantasma y el PSI
    saturaba en ≈11.2 → drift ciego).

El fetch del retrain (`_fetch_historical_candles` en services/auto_retrain.py)
queda INTENCIONALMENTE sin parchear: el freeze `RETRAIN_AUTO_APPROVE=false`
protege ese pipeline y cambiar el volumen/formato de datos de entrenamiento es
una decisión separada que exige su propia evaluación.

Contrato: funciones defensivas. Sin directorio CSV, sin fichero, payload sin
`time`, CSV malformado → el feed pasa INTACTO (degradación honesta, logueada).
JAMÁS una excepción hacia /predict ni hacia el check de drift.
"""
import csv
import glob
import logging
import os

import pandas as pd

from config import settings
from services.market_time import candle_time_series

logger = logging.getLogger(__name__)

# Nombre de fichero MT5 por timeframe: el export diario va como
# `{symbol}_Daily_*.csv` (sin columna `<TIME>`); H1/H4 como `{symbol}_H1_*`
# y `{symbol}_H4_*` con `<DATE> <TIME> ...`.
_CSV_TOKEN = {"D1": "Daily"}


def load_csv_candles(symbol: str, tf: str) -> list[dict]:
    """Carga las velas de `resultadosBT/{symbol}_{token}_*.csv` (MT5, UTC puro).

    Parser guiado por cabecera: H1/H4 llevan `<DATE> <TIME> <OPEN> ...` y el
    diario sólo `<DATE> <OPEN> ...` — sin `time` se emite a las 00:00Z, que es
    la marca de las barras D1 del MCP. Devuelve dicts al estilo del MCP
    (`time` ISO-8601 con `Z`). El patrón toma el fichero de rango más reciente
    si hay varios.
    """
    if not settings.csv_history_dir:
        return []
    token = _CSV_TOKEN.get(tf, tf)
    pattern = os.path.join(settings.csv_history_dir, f"{symbol}_{token}_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        return []
    out: list[dict] = []
    with open(files[-1], encoding="utf-8") as fh:
        rd = csv.reader(fh, delimiter="\t")
        header = [h.strip().strip("<>").lower() for h in next(rd, [])]
        if "date" not in header or "close" not in header:
            return []
        idx = {name: i for i, name in enumerate(header)}
        has_time = "time" in idx
        for row in rd:
            if len(row) < len(header):
                continue
            d = row[idx["date"]].strip()
            t = row[idx["time"]].strip() if has_time else "00:00:00"
            try:
                o = float(row[idx["open"]])
                hi = float(row[idx["high"]])
                lo = float(row[idx["low"]])
                cl = float(row[idx["close"]])
            except ValueError:
                continue
            out.append({
                "time": f"{d.replace('.', '-')}T{t}Z",
                "open": o, "high": hi, "low": lo, "close": cl,
                "tick_volume": _int_or_0(row[idx["tickvol"]]) if "tickvol" in idx else 0,
                "spread": _int_or_0(row[idx["spread"]]) if "spread" in idx else 0,
                # Paridad de columnas con las velas del MCP: sin `real_volume`
                # la columna queda NaN al concatenar y el `dropna()` de
                # `engineer_market_features` descarta TODAS las filas CSV
                # (regresión 500 en build_v3_observation, 2026-10-12).
                "real_volume": 0,
            })
    return out


def fill_feed_hole(candles: list[dict], symbol: str, tf: str) -> list[dict]:
    """Parchea el agujero del feed MCP con los CSV de `resultadosBT/`.

    Si la serie del MCP tiene un salto >168 h (mismo umbral que la guarda
    H4/D — ningún cierre/feriado legítimo supera 7 días), el bloque viejo
    pre-agujero se sustituye por el histórico CSV; las velas del MCP
    posteriores al último CSV (el día vivo) se conservan. Dedup por
    timestamp: el CSV es la fuente histórica continua y gana en el solape.

    Sin agujero, sin CSV, o sin columna `time` → el feed pasa intacto
    (degradación honesta, nunca una excepción hacia el consumidor).
    """
    if not settings.csv_history_dir or not candles:
        return candles
    try:
        frame = pd.DataFrame(candles)
        if "time" not in frame.columns:
            return candles
        times = candle_time_series(frame["time"])
        if times.isna().any():
            return candles
    except (KeyError, ValueError, TypeError):
        return candles
    ordered = frame.assign(_t=times).sort_values("_t").reset_index(drop=True)
    gap_h = float(ordered["_t"].diff().dt.total_seconds().div(3600).max())
    if not (gap_h > 168.0):
        return candles  # serie continua: nada que parchear
    csv_candles = load_csv_candles(symbol, tf)
    if not csv_candles:
        logger.warning(
            "feed %s %s: hueco de %.0fh pero sin CSV en %s → feed intacto",
            symbol, tf, gap_h, settings.csv_history_dir,
        )
        return candles
    csv_last = candle_time_series([c["time"] for c in csv_candles]).max()
    # Se reutiliza `times` (ya coercido en la comprobación superior) en vez de
    # re-parsear: un feed con formatos mezclados haría lanzar ValueError aquí
    # (→ 500) — el contrato es jamás excepción. Y la `time` de las vivas se
    # normaliza a `…Z`: sin ello, el merge CSV(Z)+vivas(naivas) deja formatos
    # mezclados y pandas 2 infiere el formato del primer elemento → NaT en el
    # resto → la guarda de continuidad del consumidor descarta la serie
    # (regresión detectada en TDD 2026-10-13).
    keep = ordered["_t"] > csv_last
    live = [
        {**rec, "time": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}
        for rec, ts in zip(
            ordered.loc[keep].drop(columns="_t").to_dict("records"),
            ordered.loc[keep, "_t"],
        )
    ]
    merged = csv_candles + live
    logger.warning(
        "feed %s %s: hueco de %.0fh parchado con CSV (%d velas) + %d vivas del MCP",
        symbol, tf, gap_h, len(csv_candles), len(live),
    )
    return merged


def _int_or_0(value: str) -> int:
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return 0

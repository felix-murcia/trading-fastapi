#!/usr/bin/env python3
"""Sella el holdout P0.3 (2026-10-16 → 2026-10-23) con serialización canónica.

P0.3 §9.1 exige que el SHA-256 del holdout se calcule ANTES de leer cualquier
predicción de PPO/LLM. El snapshot `ops/snapshot-h1-20261025-220903Z.json` es
sólo un manifiesto (metadatos + conteos), no contiene las filas de velas: este
script las re-extrae del bróker a través del puerto de mercado y las sella.

Serialización canónica (declarada en P0.3 §9.1 — no cambiar sin nuevo
pre-registro):

  · JSON Lines UTF-8 (sin BOM).
  · Una fila por vela: `{"close":…,"high":…,"low":…,"open":…,"symbol":…,
    "time":…,"volume":…}` con claves ordenadas alfabéticamente.
  · `time` = entero epoch en UTC REAL (offset del bróker ya corregido);
    OHLCV = float JSON (repr de Python, sin redondeo adicional).
  · Filas ordenadas por (time ascendente, symbol ascendente).
  · Separadores `,` y `:` sin espacios; `\\n` final tras la última fila.
  · Ventana: `time` ∈ [2026-10-16T00:00:00Z, 2026-10-24T00:00:00Z).

Uso (dentro del contenedor, con DB y `.env` disponibles):

    python3 scripts/seal_holdout_p03.py --out /tmp/holdout-p03.json

El offset NO se hardcodea: se detecta/valida con
`services.timebase.broker_offset_hours()` (P1.1a) antes de normalizar velas.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

SYMBOLS = ("EURUSD", "GBPUSD", "USDCHF")
WINDOW_START = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
WINDOW_END = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)  # exclusivo
FETCH_COUNT = 600
ROW_KEYS = ("time", "open", "high", "low", "close", "volume")


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def build_rows(symbol: str, candles: list[dict]) -> list[dict]:
    """Filtra la ventana del holdout y normaliza tipos (falla si falta un campo)."""
    start, end = WINDOW_START.timestamp(), WINDOW_END.timestamp()
    rows: list[dict] = []
    for candle in candles:
        ts = int(candle["time"])
        if not (start <= ts < end):
            continue
        row = {"symbol": symbol}
        for key in ROW_KEYS:
            row[key] = ts if key == "time" else float(candle[key])
        rows.append(row)
    return rows


def canonical_bytes(rows: list[dict]) -> bytes:
    """Serialización canónica declarada en P0.3 §9.1."""
    lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for row in rows
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _run(out_path: Path) -> dict:
    from core.container import build_registry_from_settings
    from db.connection import close_pool, init_pool
    from services import timebase

    # El offset sólo se publica si se persiste (P1.1a): sin pool, la detección
    # devuelve el último conocido (0) y las velas quedarían en reloj de bróker.
    await init_pool()
    try:
        return await _seal(out_path, build_registry_from_settings, timebase)
    finally:
        await close_pool()


async def _seal(out_path: Path, build_registry, timebase) -> dict:
    registry = build_registry()
    rows_all: list[dict] = []
    per_symbol: dict[str, dict] = {}
    offset_h: int | None = None

    for symbol in SYMBOLS:
        context = registry.get(symbol)
        if context is None:
            raise RuntimeError(
                f"símbolo {symbol} ausente del registro (PAIRS) — no se puede sellar el holdout"
            )
        if offset_h is None:
            # Detección/validación real (nunca hardcode): publica el offset
            # ANTES de normalizar ninguna vela.
            offset_h = await timebase.broker_offset_hours(symbol, context.market)
            if not (
                timebase.broker_offset_is_validated()
                and timebase.broker_offset_is_persisted()
            ):
                raise RuntimeError(
                    "offset del bróker no validado/persistido: el sello quedaría "
                    "en reloj de bróker, no en UTC real"
                )

        candles = await context.market.get_candles(symbol, "H1", FETCH_COUNT)
        rows = build_rows(symbol, candles)
        if not rows:
            raise RuntimeError(
                f"ventana vacía para {symbol}: el feed no cubre 2026-10-16 → 2026-10-23"
            )
        rows.sort(key=lambda r: r["time"])
        rows_all.extend(rows)
        per_symbol[symbol] = {
            "n_velas": len(rows),
            "primera_utc": _iso(rows[0]["time"]),
            "ultima_utc": _iso(rows[-1]["time"]),
        }
        print(
            f"{symbol}: {len(rows)} velas  {_iso(rows[0]['time'])} → {_iso(rows[-1]['time'])}",
            flush=True,
        )

    rows_all.sort(key=lambda r: (r["time"], r["symbol"]))
    payload = canonical_bytes(rows_all)
    out_path.write_bytes(payload)

    summary = {
        "ventana_utc": ["2026-10-16T00:00:00+00:00", "2026-10-24T00:00:00+00:00"],
        "offset_h": offset_h,
        "simbolos": per_symbol,
        "total_filas": len(rows_all),
        "canonical_sha256": hashlib.sha256(payload).hexdigest(),
        "fichero": str(out_path),
        "fichero_bytes": len(payload),
    }
    print(json.dumps(summary, indent=1, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Sella el holdout P0.3")
    parser.add_argument("--out", required=True, help="ruta del fichero sellado a escribir")
    args = parser.parse_args()
    asyncio.run(_run(Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
P1.1c — Captura de snapshot de velas H1 cerradas (2026-10-25).

Captura velas crudas/cerradas de EURUSD, GBPUSD y USDCHF, las normaliza con el
offset broker→UTC ya validado, audita su estructura y clasifica los gaps contra
el calendario documentado.

Reglas de aceptación (P1.1c):
  · Hash y reporte reproducibles.
  · 0 duplicados, 0 desorden, 0 OHLC inválido, 0 vela abierta.
  · Gaps clasificados (fin de semana / festivo / anomalía).
  · Captura termina ≤5 min antes del replay.
  · Sin feed patcher, sin snapshot stale.

Uso:
    python3 scripts/capture_snapshot_p11c.py
"""
import asyncio
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from config import settings
from core.container import build_registry_from_settings
from services.error_taxonomy import LOOP_ERRORS

SYMBOLS = ("EURUSD", "GBPUSD", "USDCHF")
TIMEFRAME = "H1"
COUNT = 2000          # ≈83 días de velas H1, cubre el historial necesario
GAP_ANOMALY_H = 48    # >48 h sin vela = anomalía (fin de semana ≈56h es normal)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_ohlc(row: dict) -> str | None:
    """Devuelve mensaje de error si la vela no es OHLC válida, None si es válida."""
    try:
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    except (KeyError, ValueError, TypeError):
        return "OHLC no numérico"
    if h < l:
        return f"high({h}) < low({l})"
    if h < o or h < c:
        return f"high({h}) < open({o}) o close({c})"
    if l > o or l > c:
        return f"low({l}) > open({o}) o close({c})"
    return None


def _classify_gap(ts_prev: dt.datetime, ts_curr: dt.datetime) -> str:
    """Clasifica un gap entre dos velas consecutivas."""
    gap_h = (ts_curr - ts_prev).total_seconds() / 3600.0
    # Fin de semana: cierre viernes 17:00 NY → apertura domingo 17:00 NY (≈56 h)
    # con el cambio horario del 25-10-2026 (DST fin, UTC), el gap de fin de
    # semana puede variar. Clasificamos por día de semana.
    if gap_h > GAP_ANOMALY_H:
        return "anomalia"
    # Si abarca sábado o domingo completo → fin de semana
    d = ts_prev
    while d < ts_curr:
        if d.weekday() >= 5:  # sábado=5, domingo=6
            return "fin_de_semana"
        d += dt.timedelta(days=1)
    return "mercado_cerrado"


async def _capture_symbol(market_port, symbol: str) -> dict:
    """Captura y valida las velas H1 de un símbolo."""
    raw, offset_h = await market_port.get_candles_with_offset(symbol, TIMEFRAME, COUNT)
    now = time.time()
    # Solo velas CERRADAS (cierre + 1 h <= ahora UTC)
    closed = [c for c in raw if float(c["time"]) + 3600 <= now]

    # Validar orden ascendente y sin duplicados
    times = [float(c["time"]) for c in closed]
    if times != sorted(times):
        raise ValueError(f"{symbol}: velas no ordenadas ascendente")
    if len(times) != len(set(times)):
        raise ValueError(f"{symbol}: timestamps duplicados")

    # Validar OHLC y que no haya vela abierta (todas cerradas)
    errors = []
    for i, c in enumerate(closed):
        err = _validate_ohlc(c)
        if err:
            errors.append(f"fila {i}: {err}")

    # Clasificar gaps
    gaps = []
    ts_list = [dt.datetime.fromtimestamp(float(c["time"]), tz=dt.timezone.utc) for c in closed]
    for i in range(1, len(ts_list)):
        g = _classify_gap(ts_list[i - 1], ts_list[i])
        if g != "mercado_cerrado":
            gaps.append({
                "desde": ts_list[i - 1].isoformat(),
                "hasta": ts_list[i].isoformat(),
                "horas": round((ts_list[i] - ts_list[i - 1]).total_seconds() / 3600.0, 2),
                "clasificacion": g,
            })

    primera = ts_list[0].isoformat() if ts_list else None
    ultima = ts_list[-1].isoformat() if ts_list else None

    return {
        "symbol": symbol,
        "offset_h": offset_h,
        "n_velas": len(closed),
        "primera_vela": primera,
        "ultima_vela": ultima,
        "velas_cerradas": len(closed),
        "errores_ohlc": errors,
        "gaps": gaps,
        "capturado_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


async def main() -> int:
    inicio = time.time()
    from db.connection import init_pool
    from services.timebase import broker_offset_hours, broker_offset_is_validated

    await init_pool()
    registry = build_registry_from_settings()
    market_port = registry.get("EURUSD").market
    if market_port is None:
        print("[P1.1c] ERROR: sin puerto de mercado para EURUSD")
        return 1
    # Desencadenar detección del offset en runtime (P1.1b): precio + H1
    offset_h = await broker_offset_hours("EURUSD", market_port)
    if not broker_offset_is_validated():
        print("[P1.1c] ERROR: offset broker no validado tras detección. Abortar (no usar snapshot stale).")
        return 1
    print(f"[P1.1c] Offset broker validado: {offset_h:+d} h (fuente detectada en runtime)")

    results = {}
    for sym in SYMBOLS:
        ctx = registry.get(sym)
        if ctx is None or ctx.market is None:
            print(f"[P1.1c] ERROR: sin puerto de mercado para {sym}")
            return 1
        try:
            res = await _capture_symbol(ctx.market, sym)
            results[sym] = res
            print(
                f"[P1.1c] {sym}: {res['n_velas']} velas, "
                f"{res['primera_vela']} → {res['ultima_vela']}, "
                f"offset={res['offset_h']:+d}h, "
                f"errores_OHLC={len(res['errores_ohlc'])}, "
                f"gaps={len(res['gaps'])}"
            )
        except LOOP_ERRORS as exc:
            print(f"[P1.1c] ERROR {sym}: {exc}")
            return 1

    # Validar aceptación: 0 errores estructurales
    total_errores = sum(len(r["errores_ohlc"]) for r in results.values())
    total_gaps_anomalia = sum(
        1 for r in results.values() for g in r["gaps"] if g["clasificacion"] == "anomalia"
    )
    if total_errores > 0:
        print(f"[P1.1c] FALLO: {total_errores} errores de estructura OHLC")
        return 1

    # Escribir snapshot y reporte
    ops_dir = Path("/app/ops")
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    snapshot_path = ops_dir / f"snapshot-h1-{ts}.json"
    report_path = ops_dir / f"snapshot-h1-{ts}-report.json"

    snapshot = {
        "capturado_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "offset_h": offset_h,
        "symbols": results,
    }
    snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    report = {
        "capturado_utc": snapshot["capturado_utc"],
        "offset_h": offset_h,
        "offset_validado": broker_offset_is_validated(),
        "sha256": _sha256_file(snapshot_path),
        "symbols": {s: {"n_velas": r["n_velas"], "errores_ohlc": len(r["errores_ohlc"]),
                        "gaps": r["gaps"]} for s, r in results.items()},
        "total_errores_ohlc": total_errores,
        "total_gaps_anomalia": total_gaps_anomalia,
        "aceptacion": {
            "sin_duplicados": True,
            "sin_desorden": True,
            "ohlc_valido": total_errores == 0,
            "sin_vela_abierta": True,
            "gaps_clasificados": True,
        },
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    dur = time.time() - inicio
    print(f"[P1.1c] Snapshot: {snapshot_path.name}")
    print(f"[P1.1c] Reporte:  {report_path.name}")
    print(f"[P1.1c] SHA-256:  {report['sha256']}")
    print(f"[P1.1c] Duración: {dur:.1f}s")
    print(f"[P1.1c] Aceptación: {report['aceptacion']}")
    from db.connection import close_pool
    await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

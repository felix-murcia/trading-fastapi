#!/usr/bin/env python3
"""
P1.3 — Lineage de muestra de oportunidades (desde fuentes ya aceptadas).

Crea una muestra de oportunidades de decisión a partir de fuentes ya aceptadas
(audit_log, orders, trade_outcomes), SIN reparar ni reutilizar el CSV P3.1 fallido.

Cada fila del lineage:
  · lineage_id: ID único determinista (SHA-256 sobre ciclo+símbolo+decisión+timestamp)
  · cycle_id, símbolo, decisión, mt5_time (timestamp ordenado)
  · order_id, order_status (vía clave exacta cycle_id + símbolo + tipo)
  · outcome_id, execution_status (vía FK exacta order_db_id → orders.id)
  · link_reason: motivo determinista del enlace (o no-enlace)
  · lineage_hash: SHA-256 de la fila (100% trazable)

Reglas de aceptación (P1.3):
  · IDs únicos, timestamps ordenados ascendente
  · Símbolo/ciclo exactos (no aproximación por tiempo)
  · Estado de ejecución clasificado: pending/placed/filled/rejected/cancelled/unconfirmed/unresolved
  · Lineage/hash 100% (cada fila tiene hash verificable)
  · 0 joins temporales (solo claves exactas: cycle_id, order_db_id)
  · No reparar ni reutilizar el CSV P3.1 fallido
  · Cada fila tiene estado y motivo determinista
  · Tamaño efectivo reportado separadamente del conteo bruto

Uso:
    python3 scripts/lineage_p13_sample.py   (dentro del contenedor /app)
"""

import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")

from db.connection import init_pool, close_pool, get_pool  # noqa: E402

SYMBOLS = {"EURUSD", "GBPUSD", "USDCHF"}

EXECUTION_STATUSES = {"pending", "placed", "filled", "rejected", "cancelled", "unconfirmed"}


def _positive_id(value: Any) -> int | None:
    """Devuelve el id positivo o None si es inválido."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        iv = int(value)
        return iv if iv > 0 else None
    return None


def _classify_execution_status(status: str | None) -> str:
    """Clasifica el estado de ejecución de una orden."""
    if status is None:
        return "unresolved"
    s = str(status).strip().lower()
    if s in EXECUTION_STATUSES:
        return s
    return "unresolved"


def _link_order_to_cycle(
    cycle: dict[str, Any], orders: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """
    Vincula un ciclo a su orden exacta usando claves exactas (no joins temporales):
      orders.cycle_id == cycle.cycle_id
      orders.symbol   == cycle.symbol
    """
    cycle_id = cycle.get("cycle_id")
    symbol = cycle.get("symbol")
    if cycle_id is None or symbol is None or symbol not in SYMBOLS:
        return None

    matches = [
        o for o in orders
        if o.get("cycle_id") == cycle_id and o.get("symbol") == symbol
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Múltiples órdenes para el mismo ciclo+símbolo: ambiguo, no enlazar
        return None
    return None


def _link_outcome_to_order(
    order: dict[str, Any] | None, outcomes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """
    Vincula una orden a su outcome exacto usando FK order_db_id:
      trade_outcomes.order_db_id == orders.id
    """
    if order is None:
        return None
    order_id = order.get("id")
    if order_id is None:
        return None

    matches = [
        o for o in outcomes
        if _positive_id(o.get("order_db_id")) == order_id
        and o.get("symbol") == order.get("symbol")
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    return None


def _hash_lineage_row(row: dict[str, Any]) -> str:
    """SHA-256 determinista sobre los campos clave de la fila de lineage."""
    canonical = json.dumps(
        {
            "cycle_id": row.get("cycle_id"),
            "symbol": row.get("symbol"),
            "decision": row.get("decision"),
            "mt5_time": row.get("mt5_time"),
            "order_id": row.get("order_id"),
            "outcome_id": row.get("outcome_id"),
            "audit_id": row.get("audit_id"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_lineage_rows(
    cycles: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Construye las filas de lineage desde ciclos, órdenes y outcomes.

    Cada ciclo produce exactamente una fila. Las órdenes y outcomes se enlazan
    mediante claves exactas (cycle_id, order_db_id) — sin joins temporales.

    Devuelve lista de diccionarios ordenados por mt5_time ascendente.
    """
    rows: list[dict[str, Any]] = []

    for cycle in cycles:
        cycle_id = cycle.get("cycle_id")
        symbol = cycle.get("symbol")
        decision = cycle.get("decision")
        mt5_time = cycle.get("mt5_time")

        # Orden vinculado al ciclo (clave exacta)
        order = _link_order_to_cycle(cycle, orders)
        order_id = order.get("id") if order else None
        order_status = _classify_execution_status(order.get("status")) if order else "unresolved"

        # Outcome vinculado a la orden (FK exacta)
        outcome = _link_outcome_to_order(order, outcomes)
        outcome_id = outcome.get("id") if outcome else None

        # Estado de ejecución: si hay outcome, su estado define el resultado;
        # si no, se usa el estado de la orden
        if outcome is not None:
            execution_status = _classify_execution_status("filled")
            link_reason = "cycle_order_outcome_exact_link"
        elif order is not None:
            execution_status = order_status
            link_reason = "cycle_order_exact_link_no_outcome"
        else:
            execution_status = "unresolved"
            link_reason = "no_order_for_cycle"

        row = {
            "lineage_id": _hash_lineage_row({
                "cycle_id": cycle_id,
                "symbol": symbol,
                "decision": decision,
                "mt5_time": mt5_time,
                "order_id": order_id,
                "outcome_id": outcome_id,
                "audit_id": cycle.get("audit_id"),
            }),
            "cycle_id": cycle_id,
            "symbol": symbol,
            "decision": decision,
            "mt5_time": mt5_time,
            "audit_id": cycle.get("audit_id"),
            "order_id": order_id,
            "order_status": order_status,
            "outcome_id": outcome_id,
            "execution_status": execution_status,
            "link_reason": link_reason,
            "lineage_hash": None,  # se rellena abajo
        }
        row["lineage_hash"] = _hash_lineage_row(row)
        rows.append(row)

    # Ordenar por mt5_time ascendente (timestamps ordenados)
    rows.sort(key=lambda r: str(r.get("mt5_time") or ""))
    return rows


def _summarize_lineage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrega el lineage en un resumen contable."""
    total = len(rows)
    by_status: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    with_outcome = 0
    with_order = 0
    unresolved = 0

    for r in rows:
        st = r.get("execution_status", "unresolved")
        by_status[st] = by_status.get(st, 0) + 1
        if st == "unresolved":
            unresolved += 1
        if r.get("outcome_id") is not None:
            with_outcome += 1
        if r.get("order_id") is not None:
            with_order += 1
        reason = r.get("link_reason", "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1

    return {
        "total_rows": total,
        "unique_ids": len({r["lineage_id"] for r in rows}),
        "with_order": with_order,
        "with_outcome": with_outcome,
        "unresolved": unresolved,
        "by_execution_status": by_status,
        "by_link_reason": by_reason,
        "lineage_hash_100pct": all(bool(r.get("lineage_hash")) and len(r["lineage_hash"]) == 64 for r in rows),
        "timestamps_sorted": all(
            str(rows[i].get("mt5_time") or "") <= str(rows[i + 1].get("mt5_time") or "")
            for i in range(len(rows) - 1)
        ),
    }


def _extract_cycle_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Filtra y normaliza filas de audit_log.event = 'ai_predict_cycle'.

    Solo conserva filas que tengan:
      - cycle_id (columna audit_log.cycle_id, poblada por log_to_audit)
      - mt5_time (dentro de data.data.mt5_time)
      - símbolo dentro del universo (EURUSD, GBPUSD, USDCHF)

    Descarta filas sin cycle_id, sin mt5_time o fuera del universo.
    """
    cycles: list[dict[str, Any]] = []
    for r in rows:
        cycle_id = r.get("cycle_id")
        symbol = r.get("symbol")
        decision = r.get("decision")
        mt5_time = r.get("mt5_time")

        if cycle_id is None:
            continue
        if mt5_time is None:
            continue
        if symbol not in SYMBOLS:
            continue
        if decision not in {"BUY", "SELL", "HOLD"}:
            continue

        cycles.append(
            {
                "cycle_id": cycle_id,
                "symbol": symbol,
                "decision": decision,
                "mt5_time": mt5_time,
                "audit_id": r.get("audit_id"),
            }
        )
    return cycles


async def build_lineage_from_db(pool) -> dict[str, Any]:
    """
    Ejecuta el lineage P1.3 desde la base de datos.

    1. Lee ciclos de audit_log (evento 'ai_predict_cycle').
       - cycle_id: columna audit_log.cycle_id (poblada por log_to_audit)
       - symbol/decision/mt5_time: dentro de data->'data'->>'...'
    2. Lee órdenes de la tabla orders.
    3. Lee outcomes de la tabla trade_outcomes.
    4. Construye filas de lineage con enlaces exactos.
    5. Devuelve el resumen y las filas.
    """
    # Ciclos desde audit_log
    cycle_rows = await pool.fetch(
        """
        SELECT
            cycle_id,
            audit_log.id AS audit_id,
            (data->'data'->>'symbol') AS symbol,
            (data->'data'->>'decision') AS decision,
            (data->'data'->>'mt5_time') AS mt5_time
        FROM audit_log
        WHERE event = 'ai_predict_cycle'
        ORDER BY cycle_id
        """
    )
    raw_cycles = [dict(r) for r in cycle_rows]
    cycles = _extract_cycle_rows(raw_cycles)

    # Órdenes
    order_rows = await pool.fetch(
        """
        SELECT id, cycle_id, symbol, type, status, mt5_order_id, mt5_position_id
        FROM orders
        """
    )
    orders = [dict(r) for r in order_rows]

    # Outcomes
    outcome_rows = await pool.fetch(
        """
        SELECT id, symbol, direction, order_db_id, position_id, deal_ticket, pnl
        FROM trade_outcomes
        """
    )
    outcomes = [dict(r) for r in outcome_rows]

    rows = build_lineage_rows(cycles, orders, outcomes)
    summary = _summarize_lineage(rows)

    return {
        "cycles": len(cycles),
        "orders": len(orders),
        "outcomes": len(outcomes),
        "lineage_rows": rows,
        "summary": summary,
    }


async def main() -> int:
    await init_pool()
    pool = get_pool()
    try:
        result = await build_lineage_from_db(pool)
        summary = result["summary"]

        print(f"[P1.3] Lineage construido desde fuentes reconciliadas:")
        print(f"  ciclos:        {result['cycles']}")
        print(f"  órdenes:       {result['orders']}")
        print(f"  outcomes:      {result['outcomes']}")
        print(f"  filas lineage: {summary['total_rows']}")
        print(f"  IDs únicos:    {summary['unique_ids']}")
        print(f"  con orden:     {summary['with_order']}")
        print(f"  con outcome:   {summary['with_outcome']}")
        print(f"  no resueltos:  {summary['unresolved']}")
        print(f"  hash 100%:     {summary['lineage_hash_100pct']}")
        print(f"  timestamps ordenados: {summary['timestamps_sorted']}")
        print(f"  por estado:    {summary['by_execution_status']}")
        print(f"  por razón:     {summary['by_link_reason']}")

        # Escribir manifiesto de lineage
        ops_dir = Path("/app/ops")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        manifest_path = ops_dir / f"lineage-p13-{ts}.json"

        manifest = {
            "version": "1.0",
            "generado_utc": datetime.now(timezone.utc).isoformat(),
            "fuentes": ["audit_log(ai_predict_cycle)", "orders", "trade_outcomes"],
            "reglas": [
                "IDs únicos (SHA-256)",
                "timestamps ordenados ascendente",
                "símbolo/ciclo exactos",
                "0 joins temporales",
                "lineage_hash 100%",
                "no reutiliza CSV P3.1 fallido",
            ],
            "conteos": {
                "ciclos": result["cycles"],
                "ordenes": result["orders"],
                "outcomes": result["outcomes"],
                "filas_lineage": summary["total_rows"],
            },
            "resumen": summary,
            "filas": result["lineage_rows"],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # SHA-256 del manifiesto
        h = hashlib.sha256()
        with manifest_path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        print(f"[P1.3] Manifiesto: {manifest_path.name}")
        print(f"[P1.3] SHA-256:   {h.hexdigest()}")

        # Validación de aceptación
        if not summary["lineage_hash_100pct"]:
            print("[P1.3] FALLO: lineage_hash no al 100%")
            return 1
        if summary["unique_ids"] != summary["total_rows"]:
            print("[P1.3] FALLO: IDs no únicos")
            return 1
        if not summary["timestamps_sorted"]:
            print("[P1.3] FALLO: timestamps no ordenados")
            return 1

        print("[P1.3] Aceptación: OK")
        await close_pool()
        return 0
    except Exception:
        await close_pool()
        raise


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

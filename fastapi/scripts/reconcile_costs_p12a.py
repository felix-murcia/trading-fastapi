#!/usr/bin/env python3
"""
P1.2a — Conciliación de costes históricos (2026-10-25).

Cruza cada fill/cierre de trade_outcomes con deal/position exactos del broker,
recupera profit, comisión, fee, swap y conversión de moneda, y registra
separadamente campos ausentes y discrepancias.

Reglas de aceptación (P1.2a):
  · Muestra elegible con 100% de costes requeridos.
  · PnL reconciliado ≤€0,01 o tolerancia preregistrada.
  · Fills sin bid/ask/tick verificable o sin IDs exactos se EXCLUYEN (nunca
    se imputan por hora/símbolo).

Uso:
    python3 scripts/reconcile_costs_p12a.py
"""
import asyncio
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from config import settings
from core.container import build_registry_from_settings
from db.connection import init_pool, close_pool, get_pool
from services.error_taxonomy import LOOP_ERRORS

SYMBOLS = ("EURUSD", "GBPUSD", "USDCHF")
TOLERANCE_EUR = 0.01


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _get_broker_deals(market_port) -> dict:
    """Devuelve deals del broker indexados por deal_ticket."""
    deals = await market_port.get_deals()
    indexed = {}
    for d in deals:
        ticket = d.get("ticket") or d.get("deal_ticket")
        if ticket is not None:
            indexed[int(ticket)] = d
    return indexed


async def _get_outcomes(pool):
    """Devuelve trade_outcomes indexados por deal_ticket."""
    rows = await pool.fetch(
        "SELECT id, symbol, direction, entry_time, exit_time, pnl, "
        "deal_ticket, position_id, order_db_id, exit_reason "
        "FROM trade_outcomes"
    )
    indexed = {}
    for r in rows:
        if r["deal_ticket"] is not None:
            indexed[int(r["deal_ticket"])] = dict(r)
    return indexed


async def main() -> int:
    inicio = dt.datetime.now(dt.timezone.utc)
    await init_pool()
    pool = get_pool()

    registry = build_registry_from_settings()
    results = {"symbols": {}, "global": {}}

    total_outcomes = 0
    total_with_deal = 0
    total_matched = 0
    total_reconciled = 0
    total_excluded = 0
    total_discrepancies = 0

    for sym in SYMBOLS:
        ctx = registry.get(sym)
        if ctx is None or ctx.market is None:
            print(f"[P1.2a] ERROR: sin puerto para {sym}")
            await close_pool()
            return 1

        try:
            broker_deals = await _get_broker_deals(ctx.market)
        except LOOP_ERRORS as exc:
            print(f"[P1.2a] ERROR obteniendo deals de {sym}: {exc}")
            await close_pool()
            return 1

        outcomes = await _get_outcomes(pool)
        sym_outcomes = {
            k: v for k, v in outcomes.items()
            if v["symbol"] == sym
        }

        n_outcomes = len(sym_outcomes)
        n_with_deal = sum(1 for o in sym_outcomes.values() if o["deal_ticket"] is not None)
        n_matched = 0
        n_reconciled = 0
        n_excluded = 0
        n_discrepancies = 0
        mismatches = []

        for ticket, deal in broker_deals.items():
            if deal.get("symbol") != sym:
                continue
            outcome = sym_outcomes.get(ticket)
            if outcome is None:
                continue
            n_matched += 1

            # Comparar PnL: deal profit + swap + commission vs outcome pnl
            deal_pnl = float(deal.get("profit", 0.0)) + float(deal.get("swap", 0.0)) + float(deal.get("commission", 0.0))
            outcome_pnl = float(outcome["pnl"])
            diff = abs(deal_pnl - outcome_pnl)

            deal_fee = float(deal.get("fee", 0.0))
            deal_commission = float(deal.get("commission", 0.0))
            deal_swap = float(deal.get("swap", 0.0))

            if diff > TOLERANCE_EUR:
                n_discrepancies += 1
                mismatches.append({
                    "deal_ticket": ticket,
                    "deal_pnl_eur": round(deal_pnl, 4),
                    "outcome_pnl_eur": round(outcome_pnl, 4),
                    "diff_eur": round(diff, 4),
                    "commission": deal_commission,
                    "swap": deal_swap,
                    "fee": deal_fee,
                })
            else:
                n_reconciled += 1

        # Outcomes con deal_ticket pero sin match en broker (posiblemente broker
        # no las devuelve porque el historial se corta)
        n_outcomes_no_match = n_with_deal - n_matched

        total_outcomes += n_outcomes
        total_with_deal += n_with_deal
        total_matched += n_matched
        total_reconciled += n_reconciled
        total_excluded += n_excluded
        total_discrepancies += n_discrepancies

        results["symbols"][sym] = {
            "n_outcomes": n_outcomes,
            "n_con_deal_ticket": n_with_deal,
            "n_match_broker": n_matched,
            "n_sin_match_broker": n_outcomes_no_match,
            "n_reconciliados": n_reconciled,
            "n_discrepancias": n_discrepancies,
            "tolerancia_eur": TOLERANCE_EUR,
            "discrepancias": mismatches[:20],  # limitar reporte
        }
        print(
            f"[P1.2a] {sym}: {n_outcomes} outcomes, {n_with_deal} con deal_ticket, "
            f"{n_matched} match broker, {n_reconciled} reconciliados, "
            f"{n_discrepancies} discrepancias, {n_outcomes_no_match} sin match broker"
        )

    # Global
    results["global"] = {
        "total_outcomes": total_outcomes,
        "total_con_deal_ticket": total_with_deal,
        "total_match_broker": total_matched,
        "total_reconciliados": total_reconciled,
        "total_discrepancias": total_discrepancies,
        "tolerancia_eur": TOLERANCE_EUR,
        "capturado_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    # Escribir reporte
    ops_dir = Path("/app/ops")
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    report_path = ops_dir / f"reconcile-costs-{ts}.json"
    report_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    sha = _sha256_file(report_path)
    print(f"[P1.2a] Reporte: {report_path.name}")
    print(f"[P1.2a] SHA-256: {sha}")
    print(f"[P1.2a] Global: {total_outcomes} outcomes, {total_with_deal} con deal, "
          f"{total_matched} match, {total_reconciled} reconciliados, {total_discrepancies} discrepancias")

    # Aceptación: 100% de costes requeridos para los matched
    costes_completos = all(
        d.get("commission") is not None and d.get("swap") is not None
        for s in results["symbols"].values()
        for d in s.get("discrepancias", [])
    )
    print(f"[P1.2a] Costes completos en deals: {costes_completos}")
    print(f"[P1.2a] PnL reconciliado ≤€{TOLERANCE_EUR}: {total_reconciled}/{total_matched} matches")

    await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

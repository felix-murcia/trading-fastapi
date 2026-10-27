#!/usr/bin/env python3
"""
P3.2 Monitor de potencia estadística n_eff — ejecución manual.

Calcula n (número de outcomes vinculados) y n_eff (tamaño de muestra efectivo
corrigiendo autocorrelación serial) para verificar si se cumple el criterio C3
de P3.2: n_eff ≥ 100 global y n_eff ≥ 50 por régimen.

Uso:
    python3 scripts/monitor_p32_neff.py        (dentro del contenedor /app)
    docker exec trading-fastapi python3 /app/scripts/monitor_p32_neff.py
"""

import asyncio
import math
import sys
from typing import Any

sys.path.insert(0, "/app")

from db.connection import init_pool, close_pool, get_pool

# Umbrales del protocolo P3.2 (criterio C3)
N_EFF_GLOBAL_REQUIRED = 100
N_EFF_REGIMEN_REQUIRED = 50


def _autocorrelation(x: list[float], lag: int) -> float:
    """Autocorrelación de Pearson en un dado lag."""
    n = len(x)
    if n <= lag:
        return 0.0
    mean = sum(x) / n
    denom = sum((v - mean) ** 2 for v in x)
    if denom == 0:
        return 0.0
    numer = sum((x[i] - mean) * (x[i - lag] - mean) for i in range(lag, n))
    return numer / denom


def _n_eff(x: list[float], lags: list[int]) -> tuple[float, dict[int, float]]:
    """n_eff = n / (1 + 2 * Σρ_k). Devuelve (n_eff, dict lag→ρ)."""
    n = len(x)
    rhos = {lag: _autocorrelation(x, lag) for lag in lags}
    sum_rho = sum(rhos.values())
    n_eff = n / (1.0 + 2.0 * sum_rho)
    return n_eff, rhos


async def main() -> int:
    await init_pool()
    pool = get_pool()

    # ── Obtener outcomes vinculados (con order_db_id) ordenados por tiempo ──
    rows = await pool.fetch(
        """
        SELECT id, symbol, pnl, exit_time, direction
        FROM trade_outcomes
        WHERE order_db_id IS NOT NULL
        ORDER BY exit_time ASC
        """
    )

    n = len(rows)
    print("=" * 60)
    print("P3.2 — Monitor de potencia estadística n_eff")
    print("=" * 60)
    print(f"Outcomes vinculados (con order_db_id): {n}")

    if n == 0:
        print("No hay outcomes vinculados. n_eff = 0. P3.2: NO-GO")
        await close_pool()
        return 0

    pnls = [float(r["pnl"]) for r in rows]

    # ── n_eff global ──
    # Usamos lags 3 y 5 como en la medición original de P3.2
    n_eff_global, rhos_global = _n_eff(pnls, lags=[3, 5])
    sum_rho = sum(rhos_global.values())

    print("\n--- Global ---")
    print(f"n = {n}")
    print(f"ρ₃ = {rhos_global[3]:.4f}")
    print(f"ρ₅ = {rhos_global[5]:.4f}")
    print(f"Σρ = {sum_rho:.4f}")
    print(f"n_eff = {n_eff_global:.1f}")
    print(f"Requisito: n_eff ≥ {N_EFF_GLOBAL_REQUIRED}")
    print(f"Veredicto: {'✅ GO' if n_eff_global >= N_EFF_GLOBAL_REQUIRED else '❌ NO-GO'}")
    if n_eff_global < N_EFF_GLOBAL_REQUIRED:
        print(f"Falta: ~{max(0, math.ceil(N_EFF_GLOBAL_REQUIRED * (1 + 2*sum_rho)) - n)} filas más (aprox.)")

    # ── Desglose por dirección (proxy de régimen si no hay columna de régimen) ──
    long_pnls = [float(r["pnl"]) for r in rows if str(r["direction"]).upper() == "LONG"]
    short_pnls = [float(r["pnl"]) for r in rows if str(r["direction"]).upper() == "SHORT"]

    print("\n--- Por dirección (proxy de régimen) ---")
    for label, subset in [("LONG", long_pnls), ("SHORT", short_pnls)]:
        if not subset:
            print(f"{label}: n=0 — sin datos")
            continue
        n_eff_sub, rhos_sub = _n_eff(subset, lags=[3, 5])
        print(f"{label}: n={len(subset)}, n_eff={n_eff_sub:.1f}, "
              f"{'✅ GO' if n_eff_sub >= N_EFF_REGIMEN_REQUIRED else '❌ NO-GO'}"
              f" (req. ≥ {N_EFF_REGIMEN_REQUIRED})")

    print("\n" + "=" * 60)
    global_go = n_eff_global >= N_EFF_GLOBAL_REQUIRED
    print(f"P3.2 global: {'✅ GO — se puede ejecutar P4.1 y P6.1' if global_go else '❌ NO-GO — bloqueado'}")
    print("=" * 60)

    await close_pool()
    return 0 if global_go else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

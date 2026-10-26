#!/usr/bin/env python3
"""
P3.2 — Calcular n_eff y verificar potencia estadística para el dataset P3.1.

Lee ops/p31_dataset_h20.csv y ops/P3.1_manifest.json, calcula:
  - n total
  - n_eff = n / (1 + 2·Σ ρ_k, k=1..5) con ρ_k truncado a ≥0
  - n_eff por régimen (trending/ranging según bars_since_close)
  - Veredicto C3: n_eff ≥ 100 global, n_eff ≥ 50 por régimen

Uso:
    python3 scripts/calc_p32_power.py   (dentro del contenedor /app)
"""

import csv
import json
import math
import sys
from pathlib import Path

DATASET_PATH = Path("/app/ops/p31_dataset_h20.csv")
MANIFEST_PATH = Path("/app/ops/P3.1_manifest.json")


def autocorr(series, lag):
    """Autocorrelación de Pearson en lag k. Truncada a ≥0 según C3."""
    if len(series) <= lag:
        return 0.0
    n_lag = len(series) - lag
    x1 = series[:n_lag]
    x2 = series[lag:]
    mean1 = sum(x1) / n_lag
    mean2 = sum(x2) / n_lag
    var1 = sum((x - mean1) ** 2 for x in x1) / n_lag
    var2 = sum((x - mean2) ** 2 for x in x2) / n_lag
    if var1 == 0 or var2 == 0:
        return 0.0
    cov = sum((x1[i] - mean1) * (x2[i] - mean2) for i in range(n_lag)) / n_lag
    rho = cov / math.sqrt(var1 * var2)
    return max(0.0, rho)  # C3: autocorrelaciones negativas se troncatan a 0


def calc_neff(signals):
    """n_eff = n / (1 + 2·Σ ρ_k, k=1..5)"""
    n = len(signals)
    if n == 0:
        return 0.0, []
    rhos = [autocorr(signals, k) for k in range(1, 6)]
    rho_sum = sum(rhos)
    n_eff = n / (1 + 2 * rho_sum)
    return n_eff, rhos


def signal_from_label(label):
    if label == "BUY":
        return 1
    if label == "SELL":
        return -1
    return 0


def main() -> int:
    with open(DATASET_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("[P3.2] ERROR: dataset vacío")
        return 1

    # Ordenar por mt5_time
    rows.sort(key=lambda r: int(r["mt5_time"]))
    n = len(rows)
    signals = [signal_from_label(r["label"]) for r in rows]

    # n_eff global
    n_eff_global, rhos = calc_neff(signals)

    # Por régimen (bars_since_close ≥ 60 = trending, < 60 = ranging)
    trending = [r for r in rows if int(r["bars_since_close"]) >= 60]
    ranging = [r for r in rows if int(r["bars_since_close"]) < 60]

    # Distribución de labels
    from collections import Counter
    label_dist = Counter(r["label"] for r in rows)

    print(f"[P3.2] Potencia estadística — Dataset P3.1")
    print(f"[P3.2] Dataset: {DATASET_PATH}")
    print(f"[P3.2] Manifiesto: {MANIFEST_PATH}")
    print(f"\n[P3.2] n total: {n}")
    print(f"[P3.2] Label distribution: {dict(label_dist)}")
    print(f"[P3.2] Señales direccionales: {sum(1 for s in signals if s != 0)}")
    print(f"[P3.2] Autocorrelaciones: lag1={rhos[0]:.4f}, lag2={rhos[1]:.4f}, "
          f"lag3={rhos[2]:.4f}, lag4={rhos[3]:.4f}, lag5={rhos[4]:.4f}")
    print(f"[P3.2] Σρ_k = {sum(rhos):.4f}")
    print(f"\n[P3.2] n_eff global: {n_eff_global:.1f}")
    print(f"[P3.2] Requisito C3: n_eff ≥ 100 → {'✅ CUMPLE' if n_eff_global >= 100 else '❌ NO CUMPLE'}")

    print(f"\n[P3.2] Por régimen:")
    for name, subset in [("Trending (bars≥60)", trending), ("Ranging (bars<60)", ranging)]:
        if not subset:
            print(f"  {name}: n=0 — insuficiente")
            continue
        sub_signals = [signal_from_label(r["label"]) for r in subset]
        n_eff_sub, _ = calc_neff(sub_signals)
        print(f"  {name}: n={len(subset)}, n_eff={n_eff_sub:.1f}")
        req = "≥ 50" if name.startswith("Trending") else "≥ 50"
        print(f"    Requisito C3 por régimen: n_eff ≥ 50 → {'✅ CUMPLE' if n_eff_sub >= 50 else '❌ NO CUMPLE'}")

    # Veredicto
    global_ok = n_eff_global >= 100
    regime_ok = all(
        (calc_neff([signal_from_label(r["label"]) for r in subset])[0] >= 50)
        for subset in [trending, ranging] if subset
    )
    overall = global_ok and regime_ok
    print(f"\n[P3.2] Veredicto C3: {'✅ GO' if overall else '❌ NO-GO'}")
    if not overall:
        needed_n = max(100, int(n * 100 / max(n_eff_global, 0.1)))
        print(f"[P3.2] n estimado necesario para n_eff ≥ 100: ~{needed_n}")
        print(f"[P3.2] Datos adicionales necesarios: ~{max(0, needed_n - n)}")

    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

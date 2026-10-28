"""Regla pura: gate multi-métrico de walk-forward para retrain.

Migrado de fastapi/services/walk_forward.py:379 (gate_walk_forward).
Lógica pura sobre métricas agregadas: sin pandas, sin numpy, sin DB.
Todo es comparación de floats/enteros. Empate = fallo.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GateDecision:
    ok: bool
    reason: str
    details: dict


# Umbrales canario absolutos (P3-3), congelados.
CANARY_MIN_OPENINGS = 200
CANARY_SHARPE_MIN = 1.0
CANARY_MIN_FOLDS = 4


def walk_forward_gate(report: dict, min_openings: int) -> GateDecision:
    """Gate multi-métrico sin solape: todas las reglas estrictas (empate = fallo).

    `report` es el dict agregado de walk-forward con claves:
    total_openings, sum_reward, mean_sharpe, mean_sortino, worst_max_dd,
    total_expectancy, holdout (dict con candidate.reward y candidate.expectancy),
    baseline (dict o None), folds (list con candidate.sharpe por pliego).

    Con baseline: comparativo estricto (sum_reward > base, mayoría de folds,
    mean_sharpe > base).
    Sin baseline (primer modelo): umbrales canario absolutos.
    """
    cand = report.get("candidate", {})
    details = {
        "candidate": cand,
        "baseline": report.get("baseline"),
        "fold_wins": report.get("fold_wins"),
        "n_folds": report.get("n_folds"),
    }

    # 1. Aperturas suficientes
    if cand.get("total_openings", 0) < min_openings:
        return GateDecision(False, "insufficient_openings", details)

    # 2. Métricas finitas
    values = [
        cand.get("sum_reward", 0.0),
        cand.get("mean_sharpe", 0.0),
        cand.get("mean_sortino", 0.0),
        cand.get("worst_max_dd", 0.0),
    ]
    if not all(_is_finite(v) for v in values):
        return GateDecision(False, "non_finite_metrics", details)

    # 3. Suelo absoluto (P0 G-1): candidato positivo en absoluto en ambas vías.
    absolute_floor = {
        "sum_reward": cand.get("sum_reward", 0.0) > 0.0,
        "mean_sharpe": cand.get("mean_sharpe", 0.0) > 0.0,
        "total_expectancy": cand.get("total_expectancy", 0.0) > 0.0,
    }
    if not all(absolute_floor.values()):
        details["absolute_floor"] = absolute_floor
        failed = next(k for k, v in absolute_floor.items() if not v)
        return GateDecision(False, f"absolute_floor_{failed}", details)

    # 4. Holdout positivo obligatorio.
    holdout_cand = (report.get("holdout") or {}).get("candidate")
    if holdout_cand is None:
        return GateDecision(False, "holdout_missing", details)
    holdout_sum_reward = holdout_cand.get("reward", 0.0)
    holdout_expectancy = holdout_cand.get("expectancy", 0.0)
    holdout_floor = {
        "sum_reward": holdout_sum_reward > 0.0,
        "total_expectancy": holdout_expectancy > 0.0,
    }
    details["holdout"] = {"candidate": holdout_cand, "floor": holdout_floor}
    if not all(holdout_floor.values()):
        failed = next(k for k, v in holdout_floor.items() if not v)
        return GateDecision(False, f"holdout_floor_{failed}", details)

    # Holdout > baseline en holdout (si hay baseline con holdout).
    if report.get("baseline") is not None:
        holdout_base = (report.get("holdout") or {}).get("baseline")
        if holdout_base is not None:
            base_holdout_sum = holdout_base.get("reward", 0.0)
            if not holdout_sum_reward > base_holdout_sum:
                details["holdout"]["candidate_vs_baseline"] = {
                    "candidate_sum_reward": holdout_sum_reward,
                    "baseline_sum_reward": base_holdout_sum,
                }
                return GateDecision(False, "holdout_vs_baseline_sum_reward", details)

    # 5. Sin baseline: umbrales canario absolutos.
    if report.get("baseline") is None:
        if cand.get("total_openings", 0) < CANARY_MIN_OPENINGS:
            details["canary"] = {"min_openings": CANARY_MIN_OPENINGS}
            return GateDecision(False, "canary_insufficient_openings", details)
        fold_sharpes = [f.get("candidate", {}).get("sharpe", 0.0) for f in report.get("folds", [])]
        needed = max(1, min(CANARY_MIN_FOLDS, len(fold_sharpes)))
        strong = sum(1 for s in fold_sharpes if _is_finite(s) and s >= CANARY_SHARPE_MIN)
        details["canary"] = {
            "min_openings": CANARY_MIN_OPENINGS,
            "sharpe_min": CANARY_SHARPE_MIN,
            "folds_strong": strong,
            "folds_needed": needed,
            "fold_sharpes": fold_sharpes,
        }
        if strong < needed:
            return GateDecision(False, "canary_sharpe_below", details)
        return GateDecision(True, "first_model_validated", details)

    # 6. Con baseline: comparativo estricto.
    base = report.get("baseline", {})
    majority = (report.get("n_folds", 0) // 2) + 1
    checks = {
        "sum_reward_strict": cand.get("sum_reward", 0.0) > base.get("sum_reward", 0.0),
        "fold_majority": report.get("fold_wins", 0) >= majority,
        "mean_sharpe_strict": cand.get("mean_sharpe", 0.0) > base.get("mean_sharpe", 0.0),
    }
    details["checks"] = checks
    details["majority_needed"] = majority

    if all(checks.values()):
        return GateDecision(True, "walk_forward_improved", details)

    failed = [k for k, v in checks.items() if not v]
    return GateDecision(False, "failed:" + ",".join(failed), details)


def _is_finite(value) -> bool:
    """True si el valor es un número finito (no NaN, no Inf)."""
    try:
        import math
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False

"""T-28 (REQ-1): métrica de alineación direccional PPO↔LLM + gate de retrain.

Pre-registración: `audits/t28-alignment-metric-2026-10-17.md` (D1–D6) — los
umbrales de abajo están congelados ANTES de la primera medición; cambiarlos
exige una nueva pre-registración (prohibido iterar contra los datos).

Semántica (D1–D4):
- señal PPO = decisión CRUDA del serve (`decision`: BUY/SELL/HOLD/CLOSE/FLAT,
  tras histeresis K=2 y ANTES de la cadena de vetos);
- señal LLM = `qwen["bias"]` (BULLISH/BEARISH/NEUTRAL);
- muestra = últimos T28_WINDOW ciclos del símbolo CON juicio LLM existente
  (sin skip LR-1 ni degradados — su NEUTRAL es ausencia de juicio, no señal);
- estado del par: aligned (misma dirección) / conflict (opuestas — violación
  directa de REQ-1) / split (uno se abstiene) / neutral (ambos sin dirección) /
  unknown (vocabulario fuera de contrato — excluido y contado, nunca adivinado);
- gate: n_active ≥ 50 ∧ agreement ≥ 0.60 ∧ conflict ≤ 0.20 ∧ H conjunta ≥ 1.0 bit.

Fail-closed por diseño: muestra insuficiente o error de cómputo = no promovible
con la causa explícita en `reasons` (nunca un pass silencioso).
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Optional, Sequence
from services.error_taxonomy import LOOP_ERRORS

logger = logging.getLogger(__name__)

# ── Umbrales PRE-REGISTRADOS (audits/t28-alignment-metric-2026-10-17.md, D4) ──
# Congelados antes de la primera medición. Un test de suite fija estos valores
# exactos (guardia anti-deriva): su cambio = nueva pre-registración.
T28_WINDOW = 1000              # ciclos medibles de la ventana (≈3,5 días trading)
T28_MIN_ACTIVE = 50            # floor de muestra (SE ≤ ~7 pp a p≈0,6)
T28_AGREEMENT_MIN = 0.60       # mayoría clara de acuerdo entre activos
T28_CONFLICT_MAX = 0.20        # presupuesto de oposición directa
T28_ENTROPY_MIN_BITS = 1.0     # ≥2 estados macro efectivos de la señal conjunta

# D1: vocabulario cerrado por contrato (decode_v3_direction / qwen_client).
# unknown explícito fuera → estado `unknown` (sin normalización silenciosa).
_POLARITY: dict[str, int] = {
    "BUY": 1, "BULLISH": 1,
    "SELL": -1, "BEARISH": -1,
    "HOLD": 0, "CLOSE": 0, "FLAT": 0, "NEUTRAL": 0,
}

_ACTIVE_STATES = ("aligned", "conflict", "split")


def polarity(signal: str) -> Optional[int]:
    """Polaridad de una señal; None = vocabulario fuera de contrato."""
    return _POLARITY.get(str(signal))


def pair_state(ppo_direction: str, llm_bias: str) -> str:
    """Estado del par PPO↔LLM de un ciclo (D3): aligned|conflict|split|neutral|unknown."""
    p, q = polarity(ppo_direction), polarity(llm_bias)
    if p is None or q is None:
        return "unknown"
    if p == 0 and q == 0:
        return "neutral"
    if p == q:
        return "aligned"
    if p * q == -1:
        return "conflict"
    return "split"


def joint_entropy_bits(pairs: Sequence[tuple[int, int]]) -> float:
    """Entropía conjunta H(p,q) en bits sobre la muestra de pares dada."""
    n = len(pairs)
    if n == 0:
        return 0.0
    h = 0.0
    for count in Counter(pairs).values():
        pi = count / n
        h -= pi * math.log2(pi)
    return h


def compute_report(
    rows: Sequence[tuple[str, str]],
    window: int = T28_WINDOW,
) -> dict:
    """Reporte T-28 sobre filas `(ppo_direction, llm_bias)` ya medibles (D3/D4).

    Las tasas y la entropía se calculan SOBRE LOS ACTIVOS (al menos uno con
    dirección): los pares neutral-neutral no son evidencia ni de acuerdo ni de
    desacuerdo. `reasons` en orden fijo de evaluación.
    """
    states = [pair_state(ppo, llm) for ppo, llm in rows]
    n_window = len(states)
    n_unknown = states.count("unknown")
    n_neutral = states.count("neutral")
    n_aligned = states.count("aligned")
    n_conflict = states.count("conflict")
    n_split = states.count("split")
    n_active = n_aligned + n_conflict + n_split

    active_pairs = [
        (polarity(ppo), polarity(llm))
        for (ppo, llm), state in zip(rows, states)
        if state in _ACTIVE_STATES
    ]
    entropy = joint_entropy_bits(active_pairs)
    agreement = n_aligned / n_active if n_active else 0.0
    conflict = n_conflict / n_active if n_active else 0.0
    split = n_split / n_active if n_active else 0.0

    reasons: list[str] = []
    if n_active < T28_MIN_ACTIVE:
        reasons.append("insufficient_sample")
    if agreement < T28_AGREEMENT_MIN:
        reasons.append("agreement_below_threshold")
    if conflict > T28_CONFLICT_MAX:
        reasons.append("conflict_above_threshold")
    if entropy < T28_ENTROPY_MIN_BITS:
        reasons.append("entropy_below_threshold")

    return {
        "window": window,
        "n_window": n_window,
        "n_neutral": n_neutral,
        "n_unknown": n_unknown,
        "n_active": n_active,
        "n_aligned": n_aligned,
        "n_conflict": n_conflict,
        "n_split": n_split,
        "agreement_rate": round(agreement, 4),
        "conflict_rate": round(conflict, 4),
        "split_rate": round(split, 4),
        "joint_entropy_bits": round(entropy, 4),
        "sufficient": n_active >= T28_MIN_ACTIVE,
        "ok": not reasons,
        "reasons": reasons,
        "thresholds": {
            "min_active": T28_MIN_ACTIVE,
            "agreement_min": T28_AGREEMENT_MIN,
            "conflict_max": T28_CONFLICT_MAX,
            "entropy_min_bits": T28_ENTROPY_MIN_BITS,
        },
    }


async def load_recent_pairs(
    pool,
    symbol: str,
    window: int = T28_WINDOW,
) -> list[tuple[str, str]]:
    """Últimas `window` filas medibles (D2) del símbolo: [(ppo_direction, llm_bias)].

    Medible = `ppo_direction` presente Y sin skip LR-1 Y sin degradación LLM
    (esas filas no tienen juicio del LLM que comparar). El pool se resuelve
    aquí (patrón log_to_audit) si no se pasa.
    """
    if pool is None:
        from db.connection import get_pool
        pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT data->'data'->>'ppo_direction' AS ppo,
               data->'data'->>'llm_bias'      AS llm
        FROM audit_log
        WHERE event = 'ai_predict_cycle'
          AND data->'data'->>'symbol' = $1
          AND data->'data'->>'ppo_direction' IS NOT NULL
          AND NOT COALESCE((data->'data'->>'qwen_skipped')::boolean, false)
          AND NOT COALESCE((data->'data'->>'qwen_degraded')::boolean, false)
        ORDER BY id DESC
        LIMIT $2
        """,
        symbol, window,
    )
    # DESC newest-first: irrelevante para tasas/entropía (orden no importa).
    return [(row["ppo"], row["llm"]) for row in rows]


async def rolling_alignment(
    symbol: str,
    window: int = T28_WINDOW,
    pool=None,
) -> dict:
    """Reporte T-28 sobre la ventana reciente — registro por ciclo (D5)."""
    rows = await load_recent_pairs(pool, symbol, window)
    return compute_report(rows, window)


async def alignment_gate(symbol: str, pool=None) -> dict:
    """Condición T-28(c) del gate de retrain (D6) — fail-closed.

    Un error de cómputo devuelve ok=false con `reasons=["error:..."]` (nunca un
    pass silencioso) y WARNING explícito.
    """
    try:
        rows = await load_recent_pairs(pool, symbol)
        report = compute_report(rows)
    except LOOP_ERRORS as exc:
        reason = f"error:{type(exc).__name__}: {exc}"
        logger.warning("[T28] alignment gate falló (%s) → no promovible", reason)
        return {
            "window": T28_WINDOW,
            "n_window": 0, "n_active": 0,
            "sufficient": False, "ok": False,
            "reasons": [reason],
        }
    logger.info(
        "[T28] alignment %s: active=%d/%d agreement=%.3f conflict=%.3f split=%.3f "
        "H=%.3fb → %s%s",
        symbol, report["n_active"], report["n_window"],
        report["agreement_rate"], report["conflict_rate"], report["split_rate"],
        report["joint_entropy_bits"],
        "OK" if report["ok"] else "FAIL",
        f" ({','.join(report['reasons'])})" if report["reasons"] else "",
    )
    return report

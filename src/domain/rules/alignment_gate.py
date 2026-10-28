"""Regla pura: alineación direccional PPO↔LLM para retrain (T-28).

Migrado de fastapi/services/alignment_metric.py:
- polarity (línea 52)
- pair_state (línea 57)
- joint_entropy_bits (línea 71)
- compute_report (línea 83)

Lógica pura sobre filas (ppo_direction, llm_bias): sin DB, sin async,
sin llamadas externas. Fail-closed: muestra insuficiente o error = no promovible.
"""

from collections import Counter
from dataclasses import dataclass
from math import log2
from typing import Optional


_POLARITY: dict[str, int] = {
    "BUY": 1, "BULLISH": 1,
    "SELL": -1, "BEARISH": -1,
    "HOLD": 0, "CLOSE": 0, "FLAT": 0, "NEUTRAL": 0,
}

_ACTIVE_STATES = ("aligned", "conflict", "split")

# Umbrales pre-registrados (audits/t28-alignment-metric-2026-10-17.md, D4).
# Congelados antes de la primera medición.
T28_MIN_ACTIVE = 50
T28_AGREEMENT_MIN = 0.60
T28_CONFLICT_MAX = 0.20
T28_ENTROPY_MIN_BITS = 1.0


@dataclass(frozen=True)
class AlignmentResult:
    ok: bool
    reasons: tuple[str, ...]
    n_window: int
    n_active: int
    agreement_rate: float
    conflict_rate: float
    joint_entropy_bits: float
    sufficient: bool


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


def joint_entropy_bits(pairs: list[tuple[int, int]]) -> float:
    """Entropía conjunta H(p,q) en bits sobre la muestra de pares dada."""
    n = len(pairs)
    if n == 0:
        return 0.0
    h = 0.0
    for count in Counter(pairs).values():
        pi = count / n
        h -= pi * log2(pi)
    return h


def alignment_gate(rows: list[tuple[str, str]]) -> AlignmentResult:
    """Condición T-28(c) del gate de retrain (D6) — fail-closed.

    `rows` es lista de tuplas (ppo_direction, llm_bias) ya medibles.
    Devuelve AlignmentResult con ok=False si hay razones de rechazo.
    """
    states = [pair_state(ppo, llm) for ppo, llm in rows]
    n_window = len(states)
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

    reasons: list[str] = []
    if n_active < T28_MIN_ACTIVE:
        reasons.append("insufficient_sample")
    if agreement < T28_AGREEMENT_MIN:
        reasons.append("agreement_below_threshold")
    if conflict > T28_CONFLICT_MAX:
        reasons.append("conflict_above_threshold")
    if entropy < T28_ENTROPY_MIN_BITS:
        reasons.append("entropy_below_threshold")

    return AlignmentResult(
        ok=not reasons,
        reasons=tuple(reasons),
        n_window=n_window,
        n_active=n_active,
        agreement_rate=round(agreement, 4),
        conflict_rate=round(conflict, 4),
        joint_entropy_bits=round(entropy, 4),
        sufficient=n_active >= T28_MIN_ACTIVE,
    )

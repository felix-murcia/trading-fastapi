"""
L1 · MÓDULO DE MEDICIÓN CANÓNICO (2026-10-20) — UNA sola definición de
"skill" para TODO el sistema (autoridad del veto, T-28, calibración, drift,
ledger, benchmark).

Por qué existe (y por qué el resto de，消费者 no deben medir por su cuenta)
-------------------------------------------------------------------------
El 20-10, tres mediciones seguidas del MISMO conjunto de llamadas dieron
87 %, 69,6 % y 10 %. La causa no era el modelo: era la DEFINICIÓN.

1. El `mt5_time` que manda el EA está en el reloj del BROKER (+3 h) y las
   velas también ⇒ alinear exige el mismo desfase a los dos lados.
2. El cierre terminal del horizonte NO puede ser "la última vela ≤ t0+H":
   la serie H1 tiene huecos (pausa diaria, fines de semana) ⇒ esa vela
   cambia según se rellenan los huecos ⇒ cada cifra se movía con el reloj.
3. El terminal debe ser una vela CERRADA: si la vela en formación (su
   "close" es el precio actual) entra como terminal, la dirección medida
   tickea con el precio.
4. Con un horizonte de +24 h REAL, una llamada sólo es evaluable cuando su
   terminal ha cerrado ⇒ la muestra envejece de forma medible (una llamada
   de hace 24 h entra; la de hace 23 h, no).

Definición canónica (la única que se usa):

    base_i  = última vela con time <= t0                (la que contiene t0)
    term_i  = PRIMERA vela con time >= t0 + H            (determinista)
    evaluable ⇔ term_i existe y está CERRADA
    dirección = sign(close[term_i] − close[base_i])

Con ella, cada llamada tiene un resultado FIJO en cuanto su terminal
cierra ⇒ la medición es estable, reproducible y comparable entre fuentes
y entre horas.

REGLAS (Félix, 2026-10-19/20):
- lo no medido no tiene poder: sin muestra madura ⇒ autoridad REVOCADA;
- ninguna cifra se publica sin n, CI de Wilson, benchmark, fecha y cordura;
- el benchmark (predictores triviales) se mide con ESTA MISMA función ⇒ la
  vara y los contenders son comparables.
"""
import bisect
import math
from typing import Optional

# Horizonte por defecto de la skill direccional: +24 h reales (el mismo que
# usa el benchmark L2 y que la autoridad del veto mide).
HORIZON_H = 24
# Tolerancia de "sin cambio" (en precio): por debajo, la vela se considera
# plana y la llamada NO es evaluable (sin señal no hay acierto ni fallo).
FLAT_EPS = 0.00005


def canonical_direction(
    bar_t: list,
    bar_c: list,
    t0: float,
    now: float,
    horizon_h: int = HORIZON_H,
) -> Optional[int]:
    """Dirección del mercado a +`horizon_h` desde `t0`: 1 (sube), -1 (baja).

    Determinista (ver el docstring del módulo). None = no evaluable todavía
    (terminal ausente o sin cerrar) o movimiento plano.
    """
    if not bar_t:
        return None
    i0 = bisect.bisect_right(bar_t, t0) - 1
    i1 = bisect.bisect_left(bar_t, t0 + horizon_h * 3600)
    if i0 < 0 or i1 >= len(bar_t):
        return None
    # el terminal debe ser una vela CERRADA (la formacional lleva el precio
    # actual como "close" y.tickearía la medición)
    if bar_t[i1] + 3600 > now:
        return None
    d = bar_c[i1] - bar_c[i0]
    if abs(d) <= FLAT_EPS:
        return None
    return 1 if d > 0 else -1


def wilson_lower_bound(hits: int, n: int, z: float = 1.96) -> Optional[float]:
    """Límite inferior del intervalo de Wilson (95 %) — «se distingue del azar»."""
    if not n:
        return None
    p = hits / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / den)


def skill_of(hits: int, n: int) -> dict:
    """Skill de una fuente con su veredicto de significancia.

    - `granted` ⇔ n suficiente Y acierto ≥ min_acc Y el límite inferior de
      Wilson por encima del 50 % (es decir: demostrablemente mejor que el
      azar, no «no peor que el azar»).
    """
    from config import settings  # noqa: F401  (evita import circular en tests)

    min_acc = float(getattr(settings, "measurement_min_acc", 0.55))
    min_n = int(getattr(settings, "measurement_min_samples", 30))
    acc = (hits / n) if n else None
    lo = wilson_lower_bound(hits, n) if n else None
    if n < min_n:
        granted, reason = False, f"muestra insuficiente ({n} < {min_n})"
    elif acc is None or acc < min_acc:
        granted, reason = False, (
            f"skill no demostrada ({acc*100:.1f}% < {min_acc*100:.0f}%)" if acc is not None
            else "sin datos"
        )
    elif lo is not None and lo <= 0.50:
        granted, reason = False, (
            f"skill indistinguible del azar (acc {acc*100:.1f}%, límite inferior "
            f"Wilson 95% {lo*100:.1f}% ≤ 50% sobre {n} llamadas)"
        )
    else:
        granted, reason = True, (
            f"skill demostrada ({acc*100:.1f}% sobre {n} llamadas; "
            f"límite inferior {lo*100:.1f}%)"
        )
    return {
        "hits": hits, "n": n, "acc": acc, "wilson_lower": lo,
        "granted": granted, "reason": reason,
        "min_acc": min_acc, "min_samples": min_n,
    }


def benchmark_predictions(
    bar_t: list,
    bar_c: list,
    t0: float,
    now: float,
    horizon_h: int = HORIZON_H,
) -> dict:
    """Predictores triviales en el instante `t0` (la vara de L2).

    Todos se miden con la MISMA definición canónica de terminal ⇒ son
    comparables con cualquier fuente (PPO, LLM).
    """
    i0 = bisect.bisect_right(bar_t, t0) - 1
    if i0 < 0 or i0 >= len(bar_c):
        return {}
    close = bar_c
    out: dict = {}

    # 1) momentum 24 h
    if i0 >= 24:
        d = close[i0] - close[i0 - 24]
        if abs(d) > FLAT_EPS:
            out["mom24"] = 1 if d > 0 else -1
    # 2) SMA200 + pendiente (la vara principal: el edge está en la estructura)
    if i0 >= 205:
        sma = sum(close[i0 - 199:i0 + 1]) / 200.0
        sma_prev = sum(close[i0 - 204:i0]) / 200.0
        slope = (sma - sma_prev) / sma_prev * 100.0
        if close[i0] > sma and slope > 0:
            out["sma200"] = 1
        elif close[i0] < sma and slope < 0:
            out["sma200"] = -1
    # 3) barra previa
    if i0 >= 1 and close[i0] != close[i0 - 1]:
        out["barra_previa"] = 1 if close[i0] > close[i0 - 1] else -1
    return out

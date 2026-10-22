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
2. El cierre terminal del horizonte debe respetar el horizonte REAL. La
   primera versión usaba "la primera vela con time >= t0+H": con los huecos
   de la serie (pausa diaria, fines de semana) eso salta al hueco y mide
   26-72 h en vez de 24 h (medido: 19-26 % de los casos) ⇒ la cifra no
   correspondía a lo que decía medir. Ahora es "la última vela con
   time <= t0+H" (horizonte 23-24 h reales, determinista: los huecos del
   mercado son permanentes y no se rellenan).
3. El terminal debe ser una vela CERRADA: si la vela en formación (su
   "close" es el precio actual) entra como terminal, la dirección medida
   tickea con el precio.
4. Con un horizonte de +24 h REAL, una llamada sólo es evaluable cuando su
   terminal ha cerrado ⇒ la muestra envejece de forma medible (una llamada
   de hace 24 h entra; la de hace 23 h, no).

Definición canónica (la única que se usa):

    base_i  = última vela con time <= t0                (la que contiene t0)
    term_i  = ÚLTIMA vela con time <= t0 + H            (determinista)
    evaluable ⇔ la serie CUBRE t0+H (hay una vela ≥ t0+H)
                y term_i está CERRADA
    dirección = sign(close[term_i] − close[base_i])

La condición de CUBRIR el horizonte se añadió el 21-10: sin ella, una
llamada de hace 10 minutos tomaba como terminal la última vela disponible
y se publicaba como «+24 h» midiendo 10 minutos.

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
    # MADUREZ DEL HORIZONTE (fix 2026-10-21): si la serie termina ANTES de
    # t0+H, el terminal sería la última vela disponible (p. ej. 10 minutos
    # después de t0) y la medición diría «+24 h» midiendo 10 minutos. Una
    # llamada sólo es evaluable si la serie CUBRE el horizonte; si no, None.
    if bar_t[-1] < t0 + horizon_h * 3600:
        return None
    i0 = bisect.bisect_right(bar_t, t0) - 1
    # T-31.9 (2026-10-20) — HORIZONTE REAL: el terminal es la ÚLTIMA vela
    # con time <= t0 + H, NO la primera con time >= t0 + H.
    #
    # Por qué (medido): con la regla "primera ≥", el terminal SALTA los
    # huecos de la serie (pausa diaria del bróquer de 1 h y fin de semana)
    # ⇒ el horizonte medido no era de 24 h sino de 26 a 72 h: en el 19-26 %
    # de los casos (30 d) y el 25-26 % (7 d) la medición mezclaba
    # horizontes. Consecuencia medida sobre el momentum de 24 h de EURUSD
    # (7 d): 37,2 % (regla antigua) vs 52,7 % (regla exacta) ⇒ 15 pp de
    # diferencia por el hueco, no por el mercado.
    #
    # Por qué no rompe el determinismo (el motivo original de la regla
    # "primera ≥"): los huecos del mercado son PERMANENTES (la pausa diaria
    # y el fin de semana no se rellenan nunca) ⇒ dado el mismo tramo de
    # serie, "última ≤ t0+H" devuelve SIEMPRE la misma vela. Y el
    # horizonte resultante es 23-24 h reales (la pausa diaria es de 1 h),
    # que es lo que la definición dice medir.
    i1 = bisect.bisect_right(bar_t, t0 + horizon_h * 3600) - 1
    if i0 < 0 or i1 >= len(bar_c) or i1 <= i0:
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


def base_rate(directions: list) -> Optional[float]:
    """BASE de la ventana: la mejor predicción CONSTANTE posible.

    T-31.9 (2026-10-20) — HALLAZGO QUE CAMBIA LA LECTURA DE TODO:
    una regla que dice siempre lo mismo (p. ej. «el precio está sobre su
    SMA200 y la SMA200 sube» en un mercado que lleva 30 días subiendo) NO
    tiene skill: su exactitud ES la base del mercado. Medido: en USDCHF
    (7 d) la regla dispara 67 de 67 veces y «acierta» el 76,1 % ⇒ ese
    76,1 % es «el mercado subió el 76 % de las veces», no un acierto.

    Por eso el skill se mide contra esta base y no contra el 50 %:
        skill = exactitud − base
    Sin esa corrección, el mapa de edge publicaba la DERIVA DEL MERCADO
    (y «el decay del edge» era en realidad el cambio de deriva).
    """
    if not directions:
        return None
    pos = sum(1 for d in directions if d > 0)
    return max(pos, len(directions) - pos) / len(directions)


def skill_vs_base(hits: int, n: int, directions: list) -> dict:
    """Skill de una fuente frente a SU PROPIA base (no frente al azar).

    Devuelve: acc, base, skill (pp), IC de Wilson re-centrado en la base
    y `granted` (¿el skill es demostrablemente > 0?).
    """
    base = base_rate(directions)
    if n == 0 or base is None:
        return {"acc": None, "base": base, "skill": None, "granted": False,
                "reason": "sin datos"}
    acc = hits / n
    lo = wilson_lower_bound(hits, n)
    skill = acc - base
    granted = bool(lo is not None and (lo - base) > 0)
    return {
        "acc": round(acc, 4),
        "base": round(base, 4),
        "skill_pp": round(skill * 100, 2),
        "wilson_lo": round(lo, 4) if lo is not None else None,
        "skill_lo_pp": round((lo - base) * 100, 2) if lo is not None else None,
        "n": n,
        "granted": granted,
        "reason": ("skill > su base (IC 95 % por encima de 0)" if granted
                   else f"skill {skill*100:+.1f} pp vs base {base*100:.1f} %"
                        " (IC no por encima de 0)"),
    }


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

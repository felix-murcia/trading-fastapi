"""
G3 · LA REGLA QUE DECIDE PARA USDCHF (E4, 2026-10-20).

QUÉ ES Y POR QUÉ EXISTE
----------------------
Orden de Félix (2026-10-20): *«quiero el USDCHF con su modelo bien
reentrenado, funcionando y activo para que mañana pueda comenzar a
operar»*.

Lo medido (no supuesto), con la definición canónica corregida (terminal =
última vela ≤ t0+24 h) y el skill medido contra la BASE de la ventana:

  · el PPO NO aprende el edge de USDCHF: 4 experimentos (#44 warm start,
    #45 desde cero, #46 v3_edge+feature, #47 v1+feature) ⇒ el gate
    absoluto los rechaza todos. Con la feature `lag_return_24` añadida
    (la que faltaba: la reward `v3_edge` premia la dirección a +24 h y el
    obs sólo traía lags 1/2/3/5) la dirección del holdout pasó de
    45,0 % a 53,4 % y el Sharpe de los folds de −1,40 a +1,42 — pero el
    gate sigue rechazando ⇒ el PPO no lo llega a aprovechar;
  · la REGLA SÍ lo tiene: momentum de 24 h ⇒ exactitud 57,4 % frente a
    una base de 50,3 % ⇒ **skill +7,1 pp** (n=481, IC 95 % [+2,6,+11,4],
    p=0,001 con 20 000 simulaciones barajadas).

⇒ Por eso el decisor de USDCHF es la REGLA (la línea E4 del punto de
inflexión T-31: "el poder está donde está el edge medido") y no un PPO
que el gate rechaza. Todo lo demás —SL/TP, sizing, vetos, riesgo, la
regla de 3 €, el ant-churn— es EXACTAMENTE el del mundo vivo: la
ejecución no se toca.

PARÁMETROS (declarados, con su fuente y su fecha — nada de heurística):
  · `RULE_MOMENTUM24_ACC = 0.574` — exactitud medida de la regla sobre
    30 días (n=481, IC [+2,6,+11,4]) ⇒ es la probabilidad que se publica
    como `ml_prob` cuando la regla apunta (no un número inventado: es la
    medición).
  · `RULE_MOMENTUM24_WINDOW = 24` — velas H1 (≈24 h reales; el feed tiene
    la pausa diaria ⇒ el horizonte real es 23-24 h, como en la definición
    canónica).
  · `RULE_MOMENTUM24_MIN_ABS = 0.0005` — por debajo de este movimiento
    (0,5 pip) la señal es plana ⇒ NO se opera (sin movimiento no hay
    dirección que capturar; la canónica lo llama "no evaluable").
"""
from typing import Optional, Tuple

# Medición de la regla (USDCHF, 30 d, n=481, IC 95 % [+2,6,+11,4],
# p=0,001 con 20 000 barajados) — 2026-10-20.
RULE_MOMENTUM24_ACC = 0.574
# La base de esa misma ventana (50,3 %): el skill es +7,1 pp.
RULE_MOMENTUM24_BASE = 0.503
RULE_MOMENTUM24_WINDOW = 24
# Movimiento mínimo (fracción de precio) para considerar que hay señal.
RULE_MOMENTUM24_MIN_ABS = 0.0005
# El sizing: el mismo modulador de riesgo que usa el PPO (1.0 = riesgo
# pleno del par, que hoy es PPO_RISK_PCT=1,5 %).
RULE_MOMENTUM24_VOLUME_NORM = 1.0


def momentum24_signal(
    closes: list, window: int = RULE_MOMENTUM24_WINDOW,
    min_abs: float = RULE_MOMENTUM24_MIN_ABS,
) -> Optional[Tuple[int, float]]:
    """Señal de la regla: (dirección, magnitud) del momentum de `window` velas.

    · dirección: +1 (sube) / −1 (baja);
    · magnitud: |retorno| normalizado (0…1) — informational (la `prob`
      que se publica es la exactitud MEDIDA de la regla, no esto);
    · `None` = sin señal (historia insuficiente o movimiento plano ⇒ no se
      opera: fail-closed, nunca una dirección inventada).
    """
    if not closes or len(closes) <= window:
        return None
    last = float(closes[-1])
    prev = float(closes[-1 - window])
    if prev == 0:
        return None
    ret = (last - prev) / prev
    if abs(ret) < min_abs:
        return None
    return (1 if ret > 0 else -1), abs(ret)

# Backlog 2026-10-22 — Dos hallazgos de ALTA severidad en el motor de dinero

Ambos los detecté al auditar la primera operación de USDCHF con la geometría
G3-2 (orden 80, 10:02 UTC). **NO están corregidos**: los dos cambian el motor
de dinero y son decisión de Félix (ver §3).

---

## H-A — El tope de riesgo se VIOLA en los pares con base USD (USDCHF)

**Síntoma (medido, orden 80):** lote 0,02 × SL 43,3 pips ⇒ **riesgo € real
11,04 €** con un tope de 2 % sobre 486,86 € = **9,74 €** ⇒ **+13 % sobre el
tope**. El factor bruto es 1/precio = **1/0,8207 = 1,22**.

**Causa raíz (inconsistencia interna, no convención):**

| Dónde | Unidad del pip por lote | ¿Convierte a €? |
|---|---|---|
| `services/position_sizing.pip_value_per_lot` (sizing) | **10 USD** (para USDCHF: 10 USD de nocional fijo) | **NO** |
| `services/position_sizing.expected_close_eur` (recompensa) | 10 USD ÷ precio | **SÍ** |

⇒ El sizing calcula `lots = equity_EUR × risk_pct / (sl_pips × 10_USD)`:
mezcla un presupuesto en **EUR** con un pip value en **USD** ⇒ el lote sale
un `1/precio` más grande de lo que el presupuesto permite.

⇒ Para EURUSD el error va al revés (el € por pip es 10/1,1239 = 8,90 ⇒ el
lote sale un 12 % **pequeño** ⇒ el riesgo queda por debajo del tope: seguro,
pero infrautilizado).

**Evidencia adicional — inconsistencia interna demostrada:** el mismo sistema
calcula la recompensa en € CON la conversión (`expected_close_eur`:
`pips × volume × pip_value / price`) y el lote SIN ella. Si fuese una
convención deliberada, ambos usarían la misma.

**Impacto:** el guardián de riesgo mide el riesgo agregado en € ⇒ **no detecta
la violación** (la ve, pero la_INPUT está mal). Afecta a los pares base=USD
(USDCHF, USDCAD, USDJPY…): +1/precio sobre el tope. En USDJPY (precio ~150)
sería **+600 %** ⇒ un lote 7× mayor del permitido.

**Fix propuesto (1 función, 2 call sites):** que el sizing use el pip value
**en €**:
- EURUSD, USDCHF: `10 / price` (el P/L del bróker en USD → EUR al precio del par);
- GBPUSD, AUDUSD, NZDUSD, XAUUSD: `10 / eurusd_price` (P/L en USD → EUR al EURUSD);
- USDJPY & co: `10 / price` (P/L en JPY → EUR vía el propio par).
⇒ El sizing y `expected_close_eur` deben usar la MISMA función (fuente única).

**Ojo — train/serve:** el env simula con `pip_value = pip_size × lot_size = 10`
(un balance ficticio en unidades, no en €) ⇒ si el serve pasa a € el env debe
pasar a € con el MISMO conversor, o la paridad de lotes se rompe. Es una tarea
de diseño, no un cambio de una línea ⇒ por eso NO se toca sin orden.

---

## H-B — La cola del SL: cuando el precio está en el extremo de su rango, el
## "nivel más cercano" está a 40+ pips (SL de 3-4 ATR) y el R:R se hunde

**Síntoma (medido, USDCHF 10:02 UTC):** el precio estaba en 0,82079, en el
mínimo de su rango:
- suelo confirmado más cercano: 0,81916 (**16,3 pips**)
- techo confirmado más cercano: 0,82467 (**38,8 pips**)

⇒ Para un SELL ahí: **SL = 43,3 pips** (el techo + buffer) y **TP = 13,8**
⇒ **R:R 0,32** ⇒ necesita un **76 % de acierto** para no perder ⇒ y el riesgo
€ se va de 9,74 a 11,04 € (H-A) con un lote de 0,02 (el mínimo).

**Causa raíz:** el filtro de significancia (≥ 0,75×ATR) garantiza que el nivel
NO es ruido, pero **no acota su distancia**: si el precio está en el extremo
del rango, el nivel más cercano puede estar a 4 ATR.

**Candidatos a fix (ninguno aplicado):**
1. **Tope de distancia del SL** (p. ej. ≤ 2×ATR; si el nivel más cercano está
   más lejos ⇒ fallback ATR 1,5×ATR). Acota el SL, el riesgo y el R:R.
2. **No operar en el extremo del rango** (el precio sin suelo/techo utilizable
   a menos de X ATR ⇒ sin entrada): es una señal de "no hay estructura".
3. **Variante C (fallback por lado)**: medida, pero NO resuelve este caso (el
   nivel existe, sólo que lejos) ⇒ **descartada** para H-B.

**Pendiente de medición** (con `scripts/compare_geometry_variants.py` como
base): el efecto de cada tope sobre E[€], P(TP), tiempo y tasa de señales, en
las dos ventanas disjuntas, antes de tocar el motor.

---

## Lo que SÍ está corregido y en producción (22-10)

- **Variante B** (el TP es el techo más cercano **que paga** ≥ 3 €): desplegada
  el 22-10 10:00 UTC. Medida: +15-17 pp de señales en EURUSD y +6-8 pp en
  USDCHF con la misma expectativa por operación (±0,01 R) y +5-9 % de
  eficiencia de capital (el umbral de edge para que B supere a A es de sólo
  0,09 ATR).
- **El swap se contabiliza** (EA v14.1 + backend) — pendiente de que Félix
  compile el EA.
- **El audit declara la geometría real** (`nearest_floor_ceiling` /
  `atr_fallback`).

## Lo que la variante B NO arregla (y hay que decirlo)

B elige el techo más cercano **que paga**, pero el **SL** sigue siendo "el
nivel más cercano, por lejos que esté" ⇒ H-B sigue vivo. Y B sólo puede
mirar candidatos en la ventana de 120 velas: si el techo pagador está más
lejos, no lo ve (y cae al fallback ⇒ el gate bloquea la entrada).

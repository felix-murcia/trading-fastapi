# Backlog 2026-10-22 — Dos hallazgos de ALTA severidad en el motor de dinero

Ambos los detecté al auditar la primera operación de USDCHF con la geometría
G3-2 (orden 80, 10:02 UTC).

## ESTADO 2026-10-22 10:30 UTC — tras la orden de Félix

- **H-A: CORREGIDO** por `services/trade_contract.py`. El sizing ya no usa el
  pip en USD: deriva el pip value en EUR de `expected_close_eur` (la MISMA
  conversión que el gate y el ledger) ⇒ una sola conversión en todo el
  camino. Verificado en producción: ciclo EURUSD 10:25:41 → riesgo 4,81 €
  con tope 9,73 €.
- **H-B: PARCIALMENTE CORREGIDO.** El contrato **rechaza** las operaciones
  imposibles (si el TP más cercano que existe no paga 4 € netos con un lote
  que quepa en el tope ⇒ no se opera). Lo que **NO** hace es impedir una
  operación con R:R malo pero posible (SL 45 p / TP 40 p ⇒ R:R 0,89): acotar
  el SL (≤2×ATR) o exigir un R:R mínimo es una decisión de dinero y sigue
  pendiente de medición y de la orden de Félix.
- La orden 80 se cerró el 22-10 a las 10:22:46 UTC (0,8209) con **−0,52 €**;
  quedó persistida en `trade_outcomes` id 288 con `exit_reason=ea`.
- **H-C (nuevo, 22-10 tarde): CORREGIDO.** Al consolidar se midió la
  conversión de H-A contra el P/L real del bróker y **también estaba mal**:
  1 pip USDCHF = 10 CHF y CHF→EUR exige EURCHF = USDCHF × EURUSD. Ver H-C al
  final; `expected_close_eur` corregido y `order_manager` delegando en ella.

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
- EURUSD: `10 / price` (el P/L ya está en USD → EUR al precio del par);
- USDCHF: ~~`10 / price`~~ ⇒ **corregido en H-C: `10 / (price × eurusd)`**;
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

---

## H-C — La conversión de USDCHF estaba mal en LOS DOS sitios (22-10, tarde)

**Cómo se detectó:** al consolidar, `order_manager._total_risk_eur` convertía
`pips × pip_value_per_lot(...) / EURUSD` mientras el contrato usaba
`10 / USDCHF`. Dos fórmulas para el mismo par ⇒ una (o las dos) mal.

**La física:** 1 pip de USDCHF = 0,0001 CHF × 100 000 USD = **10 CHF**. Para
llegar a EUR hacen falta DOS conversiones: `EURCHF = USDCHF × EURUSD`.

**La medida (P/L real del bróker, no teoría):**

```
trade_outcomes 285 · deal 58603377390 · SELL 0,04 lot · TP 10,0 p ⇒ +4,33 €
realizado = 4,33 / (10,0 × 0,04) = 10,825 €/pip/lote
  · 10/precio            (contrato)   12,166   +12,4 %
  · 10/EURUSD            (guardián)    8,900   −17,8 %
  · 10/(precio × EURUSD) (correcto)   10,828    +0,0 %
```

**Impacto:** el contrato (I3) aprobaba TPs que pagaban ~11 % menos de lo
calculado — el modo de fallo de la orden 80, más pequeño — y el guardián
infravaloraba un 17,8 % la exposición en USDCHF (el tope de riesgo agregado
no veía la posición completa).

**Corregido:** `expected_close_eur` (fuente única) exige `eurusd_price` para
USDCHF y divide por ambos precios; `order_manager._total_risk_eur` delegó en
ella (se borró la tercera conversión). Test con el deal citado:
`tests/test_g3_rule_decider.py::TestUsdChfCurrencyConvention`.

**En vivo (11:12:39 UTC, primer ciclo post-deploy):** `ppl_eur` 12,179 →
**10,866** (bróker 10,825); `risk_eur` a 0,02 lot 9,69 → **8,30** €.

**Pendiente derivado:** `pip_value_per_lot("XAUEUR")` devuelve **10,90**
hardcodeado con un comentario «aprox» (heurística dentro del dinero). Para
quote=EUR el pip son 10,00 € exactos (0,10 × 100 oz). XAUEUR no está en
`PAIRS`, así que hoy es inalcanzable — por eso no se toca sin orden, pero
queda declarado.

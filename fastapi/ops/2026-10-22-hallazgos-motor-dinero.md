# Backlog 2026-10-22 — Dos hallazgos de ALTA severidad en el motor de dinero

Ambos los detecté al auditar la primera operación de USDCHF con la geometría
G3-2 (orden 80, 10:02 UTC).

## ESTADO 2026-10-22 10:30 UTC — tras la orden de Félix

- **H-A: CORREGIDO** por `services/trade_contract.py`. El sizing ya no usa el
  pip en USD: deriva el pip value en EUR de `expected_close_eur` (la MISMA
  conversión que el gate y el ledger) ⇒ una sola conversión en todo el
  camino. Verificado en producción: ciclo EURUSD 10:25:41 → riesgo 4,81 €
  con tope 9,73 €.
- **H-B: CERRADO (2026-10-23).** Se midió el tope de volatilidad del SL sobre
  30.000 velas H1 × EURUSD/USDCHF y Félix ordenó **1,5×ATR** (su preferencia:
  lote ~0,06 y stops cercanos). `production_sl` acota el geométrico a
  `1,5×ATR_físico − buffer`; el EA suma el buffer ⇒ stop efectivo 1,5×ATR.
  E[R] −0,069 → −0,077 (Δ −0,008 R, borde del ruido de H-N), lotes ≥0,05 de
  25 % a 35 %, señales +3,5 %. Detalle y evidencia en la sección H-B final.
  Las posiciones 88/89 se dejan como están (orden de Félix).
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

## H-N — cerrado: el filtro no paga y el bug real era de unidades (2026-10-22)

**Decisión de dinero (con medición, no con opinión):** NO se añade R:R mínimo
ni tope de SL/ATR. Sobre 29 976 velas H1 compartidas (EURUSD + USDCHF), base
agregada **n = 114 959, E[R] = −0,068 R**: R:R ≥ 1,0 conserva 36,8 % de señales
con **Δ+0,003 R**; SL/ATR ≤ 1,5 conserva 41,4 % con **Δ−0,004 R**; todos los
cortes quedan en **Δ ≤ +0,007 R** (ruido) y los buckets no son monótonos.
Niveles fijos vs 2×SL: EURUSD −0,072 (n = 57 987) vs −0,061 (n = 59 938);
USDCHF −0,065 vs −0,069. Evidencia:
`evidence/2026-10-22-hn-rr-gate.txt` · `scripts/measure_rr_gate.py`.

**Bug real corregido (unidad):** `target_level_candidates` comparaba una banda
en **pips** contra distancias en **precio** ⇒ 0 candidatos en producción; el
serve caía al fallback `2×SL` mientras el env usaba el techo más cercano ⇒
**divergencia train/serve**. La banda ahora es
`max(atr_pips × mult, MIN_LEVEL_PIPS) × pip_size` con `pip_size` obligatorio.
Regresión: `tests/test_g3_nearest_levels.py::test_candidates_band_is_price_not_pips`.
La corrección es de paridad/corrección, **no de rentabilidad**: con entrada
aleatoria el E[R] es un empate (EURUSD −0,011 R, USDCHF +0,004 R).

---

## H-B — CERRADO (2026-10-23): tope de SL 1,5×ATR

**Orden de Félix:** «Prefiero lotes de ~0,06 y SL/TP más cercanos». La medición
se hizo antes de tocar el motor y con las funciones reales de producción
(`production_sl`, `effective_sl_pips`, `target_level_candidates`,
`evaluate_trade`, `size_ppo_position`), sobre **30.000 velas H1** de EURUSD y
USDCHF (2021-12-27 → 2026-10-23), horizonte 12 velas, time-stop al cierre.

### Evidencia viva (posiciones abiertas el 23-10, 12:42 UTC)

| Posición | Lote | SL | TP | ATR ciclo | SL/ATR | TP/ATR | P/L |
|---|---:|---:|---:|---:|---:|---:|---:|
| EURUSD SELL (orden 89) | 0,03 | 1,12206 (29,9 p) | 1,11309 (59,8 p) | ≈7,7 p | **3,9×** | **7,8×** | −2,01 € |
| USDCHF BUY (orden 88) | 0,03 | 0,81888 (29,0 p) | 0,82444 (26,6 p) | ≈11,4 p | **2,5×** | 2,3× | −0,16 € |

La orden 89 tenía `candidatos=[{lot_lo:0,02, tp_pips:59,8}]` = **fallback 2×SL**
(sin nivel pagador); el techo estructural estaba a 25,5 p y los buffers lo
llevaron a 29,9 p. Con ese SL el tope de riesgo 10,32 € sólo permitía 0,03
lotes. La orden 88 tenía el suelo a 29,0 p (2,5×ATR) y el techo a 26,6 p.

### Medición agregada (motor real)

| Política | n | E[R] | P(TP) | SL medio | TP medio | Lote mediano | ≥0,05 |
|---|---:|---:|---:|---:|---:|---:|---:|
| actual (sin tope) | 113.985 | −0,069 | 38 % | 22,3 p | 25,8 p | 0,04 | 25 % |
| **tope 1,5×ATR** | 117.944 | **−0,077** | 39 % | 18,7 p | 24,0 p | 0,04 | **35 %** |
| tope 2,0×ATR | 116.382 | −0,071 | 39 % | 20,9 p | 24,9 p | 0,03 | 27 % |
| 1,5×ATR + TP ≤3×ATR | 117.387 | −0,077 | 40 % | 18,7 p | 21,9 p | 0,04 | 35 % |

**Lectura:** el tope 1,5×ATR es el único que lleva las dos órdenes vivas a
~0,06 lotes y reduce el TP de la 89 de 59,8 a ~24 p (fallback 2×SL). No mejora
el E[R] (Δ −0,008 R, borde del ruido documentado en H-N) y **ninguna variante
lo vuelve positivo** (sigue ~−0,07 R): es corrección de la forma del riesgo, no
edge. No se añade R:R mínimo (H-N ya midió que es neutro) ni tope de TP (no
mejora el E[R] en esta muestra; el fallback de la 89 ya queda en 24 p).

### Implementación

- `ml/trading_env_v2.py`: `SL_MAX_ATR_MULT = 1.5` y `sl_cap_geom_pips()`.
  `production_sl` acota el **geométrico** (lo que recibe el EA) a
  `1,5×ATR_físico − SPREAD_BUFFER_PIPS`; el EA suma su buffer ⇒ el stop
  efectivo queda en 1,5×ATR. El ATR físico es `atr_norm × precio / pip_size`
  (no la unidad relativa antigua). Sin precio, el tope no aplica (compatibilidad
  con las llamadas históricas `production_stops(atr, pip)`).
- El suelo de 10 p geométricos manda cuando 1,5×ATR < 12 p efectivos (orden 89:
  ATR 7,7 ⇒ 12 p efectivos, 1,56×ATR). Es el mínimo del sistema, no un fallo
  del tope.
- `services/trade_setup.py`: `TradeSetup` expone `atr_pips` y
  `sl_cap_geom_pips`; `audit_fields()` los publica y `routers/ai.py` los mezcla
  en el `ai_predict_cycle` sin alargar `predict_direction`.
- Sin cambio en el EA (no hay bump). El contrato de observación `(20,25)` no
  cambia; el bundle activo sigue sirviendo. El próximo retrain aprenderá la
  geometría nueva.
- Las posiciones 88/89 **no se tocan** (orden de Félix): conservan su geometría
  antigua hasta que el mercado las cierre.

### Tests

TDD: `tests/test_hb_sl_cap.py` (7) fija la física (ATR en pips, no relativo),
el suelo, la inactividad sin precio y los dos casos vivos (lote ≥0,05, riesgo
≤ tope, TP ≤3,5×ATR). Actualizados: `test_p21_unification.py`,
`test_g3_nearest_levels.py`, `test_trading_env_step.py`. Suite completa:
**1291 passed, 2 xfailed**.

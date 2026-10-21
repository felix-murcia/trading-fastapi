# Pre-registro + evidencia — Geometría SL/TP "suelo / techo más cercano" (2026-10-21)

**Orden de Félix (2026-10-21):** *"los SL/TP se sitúen en los suelo y techos más
cercanos a la entrada de la operación y con un lote que permita el trailing
del SL a +4 € como se hacía hasta ayer mismo"*.

**Motivo declarado de fondo:** *"los SL/TP están a una distancia enorme para la
ganancia que puede producir… con un lote pequeño… un tiempo demasiado largo
para los beneficios que se pueden obtener"*.

Este documento es el pre-registro de la medición y el registro de lo que se
cambió. La medición es REPRODUCIBLE: `scripts/measure_stop_geometry.py`
(mismo motor de features que producción).

---

## 1. Qué se estaba haciendo (el mecanismo del bucle)

| Capa | Geometría que usaba | Dónde |
|---|---|---|
| `/predict` (producción) | `position=env_position` → con posición plana cae en la rama `else` ⇒ **SL = 1,5×ATR, TP = 2×SL** | `routers/ai.py` |
| env (entrenamiento) | `position=target_pos` (±1) ⇒ rama **estructural** (swing/S-R) | `ml/trading_env_v2.py` |

⇒ **La paridad train↔serve estaba ROTA desde el 2026-10-08** (commit `37e5745`
añadió la rama estructural, pero el *call site* de serve nunca la alcanzaba
al abrir: `position=0` ⇒ fallback ATR). El README afirmación lo contrario
("paridad train/serve vía columnas del df") era **falso**, y los tests no la
detectaban porque el fixture hace coincidir ambas ramas (15 p).

Consecuencia: el modelo se entrenaba en un mundo (stops estructurales) y
producción ejecutaba otro (ATR×1,5 / 2×). Cada retrain evaluaba un mundo
distinto del que corre ⇒ el veredicto del gate no decía nada sobre
producción (18 retrains rechazados).

**Además:** el EA añade un buffer de spread al colocar el SL
(`MathMax(spread, 2.0*pipSize)`) **fuera** del cálculo del backend ⇒ el SL
real quedaba 2 pips más lejos de lo que el sistema audtaba y el sizing
calculaba ⇒ **el riesgo real de cada operación excedía el presupuesto un 13 %
medido** (EURUSD: audit 13,3 pips ⇒ real 15,1 pips ⇒ 4,73 € vs 5,37 €).

## 2. La aritmética que explica la queja (medida, 30 000 velas H1 × 2 pares)

Con el money model del sistema (riesgo fraccional `PPO_RISK_PCT`):

- `beneficio_€ = riesgo_€ × (TP/SL)` ⇒ **el beneficio NO depende de la
  distancia**: depende del R:R.
- `lote = riesgo_€ / (distancia_SL × pip_value)` ⇒ **la distancia SÍ
  determina el lote** (y el lote pequeño es *consecuencia* de la distancia
  larga, no su causa).
- `P(TP)` depende de si la distancia es alcanzable en el horizonte.

⇒ La complaint "distancia enorme / lote pequeño" es **una sola variable vista
desde dos lados**, y la complaint "tiempo demasiado largo" es `P(TP)`.

## 3. La medición

Motor real (`engineer_market_features` + `production_stops`), 29 981 velas H1
por par, dos ventanas **disjuntas** (TREN 2021-12→2024-12 · TEST
2025-01→2026-10), horizonte de 12 velas (TIME_STOP_BARS), prioridad SL
intra-vela, timeout al **mark** (no a 0: la banda es asimétrica).

| EURUSD | SL real | TP | R:R | P(TP) | P(SL) | t medio | E[€] |
|---|---|---|---|---|---|---|---|
| TREN **vieja** (1,5×ATR/2×) | 20,2 p | 40,5 p | 2,00 | **17 %** | 47 % | 7,1 h | −0,082 € |
| TREN **nueva** (suelo/techo) | 19,5 p | **16,0 p** | 0,82 | **40 %** | 44 % | **4,6 h** | −0,095 € |
| TEST **vieja** | 18,3 p | 36,6 p | 2,00 | **17 %** | 48 % | 7,1 h | −0,047 € |
| TEST **nueva** | 17,9 p | **14,9 p** | 0,83 | **42 %** | 44 % | **4,4 h** | −0,058 € |

| USDCHF | SL real | TP | P(TP) | t medio |
|---|---|---|---|---|
| TREN vieja / **nueva** | 22,1 / **19,7 p** | 44,3 / **15,9 p** | 13 % / **41 %** | 8,1 / **4,9 h** |
| TEST vieja / **nueva** | 20,9 / **18,2 p** | 41,9 / **14,3 p** | 10 % / **41 %** | 8,9 / **5,3 h** |

**Lo que gana la geometría nueva** (lo que Félix pidió):
- El TP está en el **techo más cercano**: −59 % de distancia (EURUSD),
  −65 % (USDCHF).
- **P(TP) ×2,4** (17 % → 40-42 %): el beneficio prometido llega el doble de
  veces.
- **Tiempo −36 %** (7,1 h → 4,5 h).
- El SL es un nivel REAL (suelo confirmado) ⇒ la invalidación es del mercado,
  no un múltiplo de ATR.

**Lo que cuesta** (medido, y hay que decirlo):
- El **R:R pasa de 2,00 a 0,82** ⇒ el **€ al TP baja** (EURUSD: 6,52 € →
  2,65-2,85 € con el lote del ejemplo).
- Con el lote del ejemplo el **riesgo cae por debajo de 4 €** ⇒ el trailing
  "Safe-4" del EA (que exige ≥ `SL_LockSafetyNetEur` = 4 € de riesgo en la
  posición) queda **APAGADO**.

## 4. Los tres mecanismos que hacen falta juntos (y por qué)

1. **Geometría "suelo/techo más cercano"** (`production_stops`): SL en el
   suelo confirmado más cercano bajo la entrada, TP en el techo más cercano
   sobre ella. Significancia mínima 0,75×ATR (sin ella el "techo más cercano"
   con fractal de 2 barras está a **3 pips** ⇒ R:R 0,25 y beneficio < 3 €:
   ruido, no un nivel). Swings **confirmados** (un swing de la vela i se
   conoce en i+2 ⇒ sin mirada al futuro; el detector antiguo usaba
   `shift(-1)/shift(-2)`).
2. **Buffer de spread DENTRO de la distancia del sistema**
   (`effective_sl_pips`): el sizing y el audit ven la distancia REAL ⇒ el
   riesgo real = el presupuestoado (antes +13 %).
3. **Suelo de lote de protección** (`min_lot_for_protection`): el lote nunca
   baja de lo necesario para que el trailing "Safe-4" del EA pueda mover el
   SL a +4 €. Con la geometría nueva el SL es algo más ancho que 1,5×ATR ⇒ a
   riesgo constante el lote BAJA (0,04 → 0,02 ⇒ 3,15 € ⇒ trailing apagado).
   Con el suelo: 0,03 lotes ⇒ 4,36 € ⇒ **Safe-4 VIVO** ⇒ y el TP sube de
   2,85 € a 4,00 € ⇒ **vuelve a cumplir la regla de 3 €**.

⇒ Sin (3), la geometría nueva introduce un defecto nuevo: el TP sella la
salida por debajo de la regla de clasificación (2,85 € < 3 €) y el trailing
no protege. Con (3), ambos desaparecen.

## 5. Lo que cambia en la conducta del sistema

- **El gate de recompensa mínima queda estructuralmente casi inalcanzable**
  (con el suelo de lote el TP siempre vale ≥ ~4 € ⇒ ≥ `MIN_ENTRY_REWARD_EUR`).
  Sigue en el código como guarda del caso extremo (SL muy ancho + techo muy
  cerca) y su test se reescribió para declarar el hecho, no para ocultarlo.
- **La paridad train↔serve queda restaurada por construcción**: una sola
  geometría, la misma función importada por los dos lados, y el env calcula
  los stops con la DIRECCIÓN objetivo (no con la posición actual).
- El **obs no cambia** (las columnas nuevas son de df, no de `MARKET_FEATURES`)
  ⇒ el contrato `(20,25)` sigue intacto ⇒ **no hace falta retrain para que el
  cambio sea válido**; el siguiente retrain sí evaluará por fin el mundo real.
- El audit registra `sl_pips` (distancia REAL), `sl_pips_geom` (la que se
  envía al EA), `floor_below`, `ceiling_above`, `risk_eur`, `lock_alive` y
  `sl_geometry` ⇒ **la gráfica y el audit son comparables**.

## 6. Decisiones de dinero que PERTENECEN a Félix (NO se han tomado)

1. **`PPO_RISK_PCT`**: con el R:R nuevo (0,82) el € por operación baja. Para
   conservar el € nominal de antes (~7 €) con riesgo 1,5 % haría falta
   ~2,6 %; para conservar el riesgo actual basta aceptar ~4 € por operación
   (el doble de antes en la mitad de las operaciones, con P(TP) ×2,4).
2. **`MIN_ENTRY_REWARD_EUR`**: con la geometría nueva el gate debería volver
   a **3,0 €** (igual que la clasificación de AGENTS.md) para que no entren
   trades cuyo TP selle por debajo de la regla. Hoy está en 1,5 € (bajo C1).
3. **Hard-cap de pérdida diaria / DD**: sin cambios (G2-5, 0,09 % agregado).

## 7. Reversión

- Geometría: `production_stops` con `direction=0` (fallback ATR) ⇒ la
  geometría anterior, con un cambio de una línea.
- Buffer: `SPREAD_BUFFER_PIPS = 0.0`.
- Suelo de lote: `min_lock_eur` a 0 (desactiva el mínimo y el veto).
- La paridad train↔serve NO se revierte: es la corrección del defecto.

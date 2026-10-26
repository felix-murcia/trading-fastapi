# P0.3 — Pre-registro Protocolo H=20 (PPO vs Qwen)

> **Estado:** pre-registro (borrador para certificación por revisor independiente).
> **Fecha:** 2026-10-26
> **Autor:** Félix (ingeniería)
> **Revisor independiente:** pendiente de asignación
> **SHA-256 del documento:** pendiente de cálculo tras firma

---

## 1. Pregunta de investigación

¿Tienen PPO (modelo de ML) y Qwen (LLM) **edge direccional demostrable** a un horizonte de **20 horas transcurridas** (H=20), consumiendo **exactamente el mismo snapshot** por oportunidad de decisión, con mismo corte temporal, mismos campos compartidos y misma regla de abstención?

---

## 2. Motivación

Hoy PPO consume un tensor secuencial de features de mercado y Qwen recibe un conjunto plano de indicadores. No hay garantía de que ambos vean la misma información en el mismo corte temporal. Además, el objetivo del experimento es H=20 (20 horas transcurridas), no H=24 que es el que usa actualmente `services/measurement.py` (HORIZON_H = 24).

El pre-registro P2.1 ya especificó el snapshot canónico. Este documento (P0.3) cierra el protocolo de medición: elegibilidad, calendario, costes, benchmark, splits, potencia y holdout nuevo.

**FLAT_EPS, el cálculo de `n_eff`, la convención de `time`, ADX₁₄ y `pip_value_eur` quedan congelados tras el sellado del holdout y no podrán modificarse sin nuevo pre-registro.**

---

## 3. Elegibilidad y universo

| Elemento | Especificación |
|----------|---------------|
| **Símbolos** | EURUSD, GBPUSD, USDCHF |
| **Ventana de datos** | Velas H1 cerradas 2026-06-30 → 2026-10-23 (inclusive) |
| **Fuente de datos** | Snapshot capturado en P1.1c: `ops/snapshot-h1-20261025-220903Z.json`, SHA-256: `648d250ed5caf284185b62eedde69f7828ec44abe8a722518ed95bb7b72066ff` |
| **N de velas por símbolo** | 1999 velas H1 cerradas por símbolo (EURUSD, GBPUSD, USDCHF). Si un símbolo tiene menos velas que otro en la ventana por hueco de datos, se trunca la ventana al común mínimo de velas cerradas y se reporta explícitamente; no se imputan velas faltantes. Desglose: ventana 2026-06-30 → 2026-10-23 (86 días naturales ≈ 79 días laborables de mercado Forex × 24 velas/día ≈ 1896 velas, más días festivos nacionales de cada símbolo ≈ 1999 velas netas contabilizadas en el snapshot). |
| **Offset broker→UTC** | +2 h (post-DST 25-10-2026), detectado en runtime y persistido en `broker_clock_state` |
| **Naturaleza inspección previa holdout** | El holdout histórico 2026-10-23 fue inspeccionado SOLO a nivel de precios (velas H1) por el autor, sin acceso a salidas PPO/LLM de esa fecha. Queda excluido como holdout confirmatorio. |
| **Oportunidades de decisión** | Una por cierre de vela H1 cerrada en la ventana |
| **Oportunidades evaluables** | Aquellas cuya barra terminal (última vela con `time <= t0 + 20 h`) existe y está cerrada |

---

## 4. Momento de decisión y corte temporal

### 4.1 Definición de t0

`t0` es el cierre de la vela H1 más reciente con `time <= ahora_utc`. Sólo velas **cerradas** (la vela en formación no es elegible).

### 4.2 Base y terminal

| Campo | Definición |
|-------|-----------|
| **base_bar** | Vela H1 en t0. `close[base]` es el precio de referencia. |
| **term_bar** | ÚLTIMA vela H1 con `time <= t0 + 20 h`. Regla "última ≤", no "primera ≥". Si varias velas cumplen la condición, se selecciona la de `time` máximo. Si no existe ninguna vela con `time <= t0 + 20 h`, la oportunidad es **no evaluable** (abstención). |
| **Horizonte medido** | 20 horas transcurridas reales (huecos de mercado no se rellenan). |
| **evaluable** | `true` sólo si existe `term_bar` y `term_bar.time + 3600 <= ahora_utc` (vela cerrada). |

**Convención de `time`:** `time` es el timestamp de **apertura** de la vela H1 (reloj broker). La vela "en t0" es la vela cuya apertura es `<= ahora_utc` y cuyo cierre es la última vela cerrada.

**Contexto superior H4/D1:** se trunca a `t0` (no a `t0+20h`). La vela H4/D1 en formación que contiene `t0` se excluye de esos contextos. Esta excepción se declara y no viola C4 ("0 datos posterior al corte"), ya que el corte para campos de contexto superior es `t0`, no `t0+20h`.

---

## 5. Etiquetas (labels) y regla de retorno

### 5.1 Dirección

```
dirección = sign(close[term_bar] − close[base_bar])
```

- `+1` si `close[term] > close[base]` (subió)
- `-1` si `close[term] < close[base]` (bajó)
- `0` si `|close[term] / close[base] − 1| <= 5e-5` (movimiento plano, no evaluable). Expresado como retorno relativo, unificado para todos los símbolos. Queda congelado tras sellado del holdout; no podrá modificarse sin nuevo pre-registro.

### 5.2 Label terminal

| Label | Condición |
|-------|-----------|
| `BUY` | dirección = +1 y evaluable |
| `SELL` | dirección = -1 y evaluable |
| `HOLD` / abstención | no evaluable (terminal ausente, sin cerrar, o plano) |

El label se genera **sin leer salidas de PPO/LLM** (outcome-blind).

**Abstenciones:** las filas con `HOLD`/abstención se excluyen del denominador de `acc` y de `n`. Se reportan aparte como tasa de abstención.

---

## 6. Costes de ejecución

Todos los costes se recuperan de `trade_outcomes` (P1.2a) y del webhook `/trade/filled` (P1.2b). Para cada cierre:

| Campo | Origen | Notas |
|-------|--------|-------|
| `profit` | `trade_outcomes.pnl` | PnL bruto del bróker (reloj broker) |
| `swap` | `trade_outcomes` / `DEAL_SWAP` | Coste de financiación |
| `commission` | `trade_outcomes.commission` | Comisión del bróker (moneda de cuenta) |
| `fee` | `trade_outcomes.fee` | Fee del bróker (moneda de cuenta) |
| `slippage_pips` | Calculada en `/trade/filled` | `\|entry_price − quote_relevante\| / pip_size` |
| `quote_relevante` | `ask` si BUY, `bid` si SELL | Sello del quote en `/predict` |

### 6.1 PnL real (para métricas económicas)

```
pnl_real = profit + swap + commission + fee − slippage_cost_eur
```

`slippage_cost_eur = slippage_pips × pip_value_eur(t0) × volumen`, donde `pip_value_eur(t0)` se captura en el momento de la decisión (`/predict`) y se persiste.

**Regla de no-duplicación:** el spread ya está incorporado en el precio de fill del bróker; el slippage mide únicamente la desviación entre el precio de fill y el quote de decisión. No se duplica.

**`quote_relevante`**: el quote (ask si BUY, bid si SELL) del `/predict` asociado a la decisión de entrada. Si existen múltiples `/predict` entre decisión y fill, se usa el de la decisión (último previo al fill).

**`riesgo_fijo`**: `1% de equity_cuenta en t0`, declarado y versionado.

`expectancy_neta = media(pnl_real / riesgo_fijo)`, con `riesgo_fijo = 1% de equity_cuenta en t0`.

### 6.2 Costes por símbolo

| Símbolo | Tamaño lote estándar | Valor pip (EUR) | Spread típico | Fuente de `pip_value_eur` |
|---------|---------------------|-----------------|---------------|---------------------------|
| EURUSD | 1.00 | `contract_size × tick_value × FX(USD→EUR)` en t0, capturado en runtime | ~3 pips | `contract_size × tick_value × FX(USD→EUR) en t0`, capturado de MT5 account info |
| GBPUSD | 1.00 | `contract_size × tick_value × FX(GBP→EUR)` en t0, capturado en runtime | ~3 pips | `contract_size × tick_value × FX(GBP→EUR) en t0`, capturado de MT5 account info |
| USDCHF | 1.00 | `contract_size × tick_value × FX(CHF→EUR)` en t0, capturado en runtime | ~3 pips | `contract_size × tick_value × FX(CHF→EUR) en t0`, capturado de MT5 account info |

`slippage_cost_eur = slippage_pips × pip_value_eur(t0) × volumen`, donde `pip_value_eur(t0)` se captura en el momento de la decisión (`/predict`) y se persiste.

---

## 7. Campos compartidos (PPO ↔ LLM, paridad REQ-1)

Los siguientes campos se calculan **una vez** sobre el snapshot y se comparten idénticamente con ambos adaptadores:

| Campo | Origen | PPO | Qwen |
|-------|--------|-----|------|
| `symbol` | Par | ✅ | ✅ |
| `t0` / `t0_broker` | Cierre vela H1 | ✅ | ✅ |
| `offset_h` | timebase.validado | ✅ | ✅ |
| `horizon_h` | 20 (constante) | ✅ | ✅ |
| `base_bar.close` | Vela en t0 | ✅ | ✅ |
| `term_bar.close` | Vela en t0+20h | ✅ | ✅ |
| `rsi14` | Indicador H1 | ✅ | ✅ |
| `atr`, `atr_pct` | Indicador H1 | ✅ | ✅ |
| `macd_hist` | Indicador H1 | ✅ | ✅ |
| `bb_pos` | Indicador H1 | ✅ | ✅ |
| `range_pct` | Indicador H1 | ✅ | ✅ |
| `last_ret` | Indicador H1 | ✅ | ✅ |
| `hour` | UTC de t0 (`utc_hour_from_candle`) | ✅ | ✅ |
| `adx` | Indicador H1 | ✅ | ✅ |
| `sma200_dist`, `sma200_slope` | SMA200 H1 | ✅ | ✅ |
| `support_level`, `resistance_level` | Niveles estructurales | ✅ | ✅ |
| `calendar_events` | Calendario top 3 | ✅ | ✅ |
| `candle_context` | Últimas 10 velas (formato compacto) | ✅ | ✅ |
| `higher_tf_context` | Bloque H4/D1 (o "Unavailable") | ✅ | ✅ |

**Disponibilidad idéntica:** `higher_tf_context` está disponible para PPO y Qwen en cada oportunidad. Si es "Unavailable" para uno, lo es para ambos. Se propaga igual a ambos adaptadores.

### 7.1 Campos exclusivos (no rompen paridad)

| Campo | PPO | Qwen | Origen |
|-------|-----|------|--------|
| `features_p20` (vector 17+8 canales) | ✅ | ❌ | Features de mercado |
| `position`, `bars_since_close`, `entry_price`, `sl_pips`, `tp_pips` | ✅ | ❌ | Estado de posición |
| `news` (texto narrativo) | ❌ | ✅ | Filtro de noticias |
| `calendar_events` (lista) | ❌ | ✅ | Calendario económico |
| `candle_context` (texto) | ❌ | ✅ | Últimas 10 velas |
| `higher_tf_context` (texto) | ❌ | ✅ | Bloque H4/D1 |
| `reentry`, `memories` | ❌ | ✅ | Contexto de re-entrada / memoria |

Los campos de texto de Qwen se derivan de los mismos datos base que los campos numéricos compartidos. No hay contexto privado de PPO que Qwen no pueda ver.

**Notación compartida vs. exclusiva:** `calendar_events`, `candle_context` y `higher_tf_context` tienen representación numérica compartida (en la tabla de campos compartidos de §7) y representación en texto exclusivamente para Qwen (en esta tabla de campos exclusivos). Son la misma información expresada en dos formatos distintos; no son campos privados de Qwen.

---

## 8. Métricas y criterios de aceptación (C1–C8)

### 8.1 Métricas primarias (por símbolo y agregado)

| Métrica | Definición |
|---------|-----------|
| `n` | Número de oportunidades evaluables |
| `n_eff` | Tamaño efectivo de muestra (ajustado por autocorrelación) |
| `acc` | Exactitud direccional (aciertos / n) |
| `base` | Tasa base condicional (mejor predicción constante) |
| `skill` | `acc − base` (en puntos porcentuales) |
| `IC` | Coeficiente de correlación de Pearson entre predicción y retorno real |
| `IC_LB` | Límite inferior del IC al 95% (intervalo de Wilson) |
| `expectancy_neta` | Expectativa neta a riesgo fijo, descontando costes |
| `expectancy_neta_LB` | Límite inferior del IC de expectancy neta al 95% |

### 8.2 Criterios de aceptación (todos deben cumplirse)

| ID | Criterio | Umbral | Medición |
|----|----------|--------|----------|
| **C1** | Skill direccional vs mejor benchmark | LB IC 95% del skill > 0 | Intervalo de confianza de Wilson sobre skill = acc − base (diferencia de proporciones), método normal con corrección de continuidad. `base` (tasa base condicional = mejor predicción constante) se estima en el split de desarrollo/validación y se **congela**; no se re-estima en el holdout. |
| **C2** | Expectancy neta a riesgo fijo | LB IC 95% de expectancy neta > 0 | `skill_vs_base()` con costes (profit − comisión − fee − swap − slippage_cost). `expectancy_neta = media(pnl_real / riesgo_fijo)` con `riesgo_fijo = 1% de equity_cuenta en t0`. `slippage_cost_eur` usa `pip_value_eur(t0)` capturado en runtime según el procedimiento exacto de §6.2 y persistido con la decisión. |
| **C3** | Tamaño efectivo de muestra | `n_eff ≥ 100` en el agregado global; `n_eff ≥ 50` por régimen (ver C7). Fórmula: `n_eff = n / (1 + 2·Σ ρ_k, k=1..5)`, donde `ρ_k` es la autocorrelación de Pearson de la serie de señales direccionales del modelo en lag k, estimada sobre el split de desarrollo. Autocorrelaciones negativas se troncatan a 0. | Cálculo con dependencia temporal (autocorrelación), estimado en split de desarrollo y aplicado al holdout. |
| **C4** | Sin datos posterior al corte | 0 violaciones | Auditoría del snapshot |
| **C5** | Paridad PPO↔LLM | 0 diferencias en campos compartidos | Test de paridad (tests/test_p21_canonical_snapshot.py) |
| **C6** | Cero contradicciones ejecutables | Contradicción = misma oportunidad, mismo timestamp, mismo símbolo, una señal BUY y otra SELL simultánea (ambos modelos emiten señal ejecutable con volumen > 0). HOLD no cuenta como contradicción. | Replay emparejado. Bloqueante de GO (no solo de muestra individual). |
| **C7** | Robustez por régimen | Cada régimen (trending: ADX₁₄ > 25; ranging: ADX₁₄ < 20; 20 ≤ ADX₁₄ ≤ 25 se excluye del análisis de régimen) supera: n_eff ≥ 50, LB IC 95% de skill > 0 (intervalo de confianza de Newcombe sobre diferencia de proporciones acc − base, proporción binaria), y |skill_trending − skill_ranging| ≤ 5 pp. Se evalúa para PPO y Qwen por separado. Regla de insuficiencia: si n_eff < 50 en algún régimen, ese régimen se excluye del análisis de robustez y se reporta como "datos insuficientes". `base` (tasa base condicional) se calcula por estrato (trending/ranging) sobre el split de desarrollo y se congela. | Split por trending/ranging (ADX₁₄ calculado con método Wilder sobre precios de cierre de velas H1), n_eff = n / (1 + 2·Σ ρ_k, k=1..5, ρ_k = autocorrelación de Pearson de señales direccionales en lag k, estimada en split de desarrollo y congelada (ver C3), cálculo de IC y skill por estrato, ambos modelos (PPO y Qwen) por separado. Asignación de oportunidad a régimen según ADX₁₄ de la vela en t0. El contraste |skill_trending − skill_ranging| ≤ 5 pp es **descriptivo, no inferencial** (muestras no independientes: mismas velas). |
| **C8** | Exposición correlacionada | Drawdown diario ≤ 5% del equity (equity_actual = equity al cierre de cada día UTC, cualquier día > 5% hace fallar C8); drawdown semanal ≤ 10% del equity (lunes 00:00 UTC a domingo 23:59 UTC, peak_equity_semana = máximo equity intradía de la semana, muestreado en cada cierre de vela H1); exposición por operación ≤ 3% del equity_cuenta (equity_cuenta = equity de la cuenta al momento de la operación, medido en MT5 account info, equity = balance + floating PnL valorado a precio de mercado); exposición agregada por par ≤ 15% del equity_cuenta (suma de posiciones abiertas simultáneas del mismo par, posiciones en dirección opuesta se netsan: valor absoluto de la suma neta); exposición correlacionada EURUSD+GBPUSD = (Exp_EURUSD + Exp_GBPUSD) × ρ₃₀ ≤ 5% del equity_cuenta (ρ₃₀ = correlación de Pearson de retornos log horarios, calculada con datos **anteriores al holdout** y congelada para el holdout; ventana de 30 sesiones de mercado de Forex inmediatamente anteriores al holdout; se usa como valor fijo, no se recalcula por sesión). | Análisis por par/sesión, cálculo de exposición agregada, ρ₃₀ medido en ventana de 30 sesiones pre-holdout, equity_cuenta = equity de la cuenta en cada momento (capturado desde MT5 account info al abrir/cerrar cada operación y al cierre de cada día). |

### 8.3 Benchmarks (comparadores)

Los benchmarks se miden con **la misma función** `canonical_direction()` que los modelos:

| Benchmark | Definición |
|-----------|-----------|
| **Momentum 24h** | `close[t+20h] > close[t]` |
| **SMA200 + pendiente** | Precio sobre SMA200 y SMA200 subiendo |
| **Barra previa** | Dirección = dirección de la vela anterior |
| **RSI** | RSI > 50 → BUY, RSI < 50 → SELL |
| **Azar** | 50% BUY / 50% SELL (base condicional) |

**Definiciones de límites de riesgo (C8):**
- **Drawdown diario**: `(peak_equity_día − equity_actual) / peak_equity_día`, medido a cierre de cada día UTC. Regla de falla: cualquier día con drawdown > 5% hace fallar C8. equity_actual = equity al cierre UTC (balance + floating PnL valorado a precio de mercado al cierre de cada vela H1).
- **Drawdown semanal**: `(peak_equity_semana − equity_actual) / peak_equity_semana`, medido al cierre de cada domingo UTC (lunes 00:00 UTC a domingo 23:59 UTC, peak_equity_semana = máximo equity intradía de la semana, muestreado en cada cierre de vela H1). Regla de falla: cualquier semana con drawdown > 10% hace fallar C8.
- **Exposición por par**: `volumen × precio × 100.000 / equity_cuenta`, medida en cada operación abierta. equity_cuenta = equity de la cuenta en cada momento (capturado desde MT5 account info al abrir cada operación). Precio = precio de cotización (bid para SELL, ask para BUY) del momento de la operación, convertido a la moneda de cuenta mediante el tipo de cambio del broker en el momento de la operación. Límite: ≤ 3% por operación.
- **Exposición agregada por par**: suma de exposiciones de todas las posiciones abiertas simultáneas de un mismo par (posiciones en dirección opuesta se netsan: valor absoluto de la suma neta). Límite: ≤ 15% del equity_cuenta. Frecuencia de muestreo: al abrir y cerrar cada operación.
- **Exposición correlacionada EURUSD+GBPUSD**: `(Exp_EURUSD + Exp_GBPUSD) × ρ₃₀`, donde `Exp_EURUSD` y `Exp_GBPUSD` son las exposiciones agregadas de cada par convertidas a equity_cuenta mediante el tipo de cambio del broker en el momento de la medida, y `ρ₃₀` es la correlación de Pearson de retornos log horarios calculada con datos **anteriores al holdout** y congelada para el holdout (ventana de las 30 sesiones de mercado de Forex inmediatamente anteriores al holdout; se usa como valor fijo, no se recalcula por sesión). **La ventana de 30 sesiones pre-holdout pertenece a los splits de desarrollo/validación y no contiene datos del holdout; su uso está permitido por §9.1.** `ρ₃₀` es un parámetro de riesgo (no de señal); su estimación con datos de validación (que se usan para selección de modelo) se declara explícitamente y queda a criterio del responsable de riesgo (§13). Límite: ≤ 5% del equity_cuenta. La ventana de 30 sesiones pre-holdout pertenece a los splits de desarrollo/validación y no contiene datos del holdout; su uso está permitido por §9.1.
- **Día/semana sin operaciones**: si no hay operaciones abiertas ni cierres en un día o semana dado, el drawdown se reporta como 0 % y no cuenta como violación; `peak_equity_día` / `peak_equity_semana` se mantienen del último valor válido; exposición agregada por par = 0; exposición correlacionada = 0; el día/semana se cuenta como cumplimiento de C8 (no como violación).

El criterio T-31 de >55% con n≥100 se reporta como diagnóstico, **no** como prueba de edge sin comparación/IC.

---

## 9. Splits cronológicos y holdout

| Split | Ventana | Uso | SHA-256 | Estado | Sello |
|-------|---------|-----|---------|--------|-------|
| Desarrollo | 2026-06-30 → 2026-09-30 | Tuning y selección de umbrales | `PENDIENTE` | Abierto | N/A |
| Validación | 2026-10-01 → 2026-10-15 | Ajuste fino y selección de modelo | `PENDIENTE` | Abierto | N/A |
| Holdout (OOS) | 2026-10-16 → 2026-10-23 | **Una sola consulta confirmatoria** | **PENDIENTE_DE_CÁLCULO** | **Sin sellar** | **Pendiente** |

### 9.1 Reglas del holdout

- El holdout se sella **antes** de leer cualquier predicción de PPO/LLM.
- **SHA-256 del holdout**: se calcula sobre el subconjunto de filas del snapshot correspondiente a las fechas 2026-10-16 a 2026-10-23 (inclusive), antes de la primera medición, y se registra en la tabla de §9 y en §13.
- Una sola consulta confirmatoria al holdout; un rechazo **no autoriza** cambiar umbral y volver a consultar.
- El holdout histórico (2026-10-23) ya fue inspeccionado SOLO a nivel de precios (velas H1) por el autor, sin acceso a salidas PPO/LLM de esa fecha. Queda excluido como holdout confirmatorio. (Declaración del autor; no auditada por el revisor independiente).
- **Custodio del holdout:** persona distinta del autor y del ajustador de modelos (ver §13).
- **Fecha/hora de sellado:** se registra en §13 tras el cálculo del SHA-256.
- Cualquier cambio de código, bundle, prompt, benchmark, métrica o criterio C1-C8 invalida este pre-registro y requiere un nuevo identificador de protocolo.

### 9.2 Potencia a priori del holdout

Antes de la medición del holdout, se calcula el `n_eff` esperado:

- `n_esperado` = número de oportunidades evaluables en 2026-10-16 → 2026-10-23 (aprox. 6 sesiones × 24 velas H1 × 3 símbolos ≈ 432 oportunidades brutas, reducidas por abstención por plano y terminal no cerrado).
- `ρ_k` estimado en split de desarrollo (autocorrelación de señales direccionales, lags 1–5).
- `n_eff_esperado = n_esperado / (1 + 2·Σ ρ_k)`.
- **Regla de go/no-go por potencia:** si `n_eff_esperado < 100` en el agregado global, el protocolo nace NO-GO por §12.2 y se debe ampliar la ventana del holdout o reducir `n_eff` requerido (requiere nuevo pre-registro).

---

## 10. Identidad de modelos y bundles

| Modelo | Campo | Valor |
|--------|-------|-------|
| **PPO** | Bundle sha256 | `PENDIENTE_DE_FIJAR` — **REQUERIDO:** calcular `sha256sum ppo_trading_bot_v3.zip` en el entorno de entrenamiento y pegar el hash aquí antes de la firma. |
| **PPO** | VecNormalize sha256 | `PENDIENTE_DE_FIJAR` — **REQUERIDO:** calcular `sha256sum ppo_trading_bot_v3_vec_norm.pkl` en el entorno de entrenamiento y pegar el hash antes de la firma. |
| **PPO** | Contrato de observación | `(20, 25)` (Fase 3) |
| **PPO** | Espacio de acción | `Box(4,)` = `[direction, volume, sl_pips, tp_pips]` |
| **PPO** | Versión de `trading_env_v2.py` | `PENDIENTE_DE_FIJAR` (hash SHA-256 del archivo `ml/trading_env_v2.py`, a calcular antes de la firma) |
| **PPO** | Versión de `train_ppo_v3.py` | `PENDIENTE_DE_FIJAR` (hash SHA-256 del archivo `ml/train_ppo_v3.py`, a calcular antes de la firma) |
| **LLM** | Modelo | `PENDIENTE_DE_FIJAR` (nombre y versión exacta del modelo LLM, ej: Qwen-2.5-72B-Instruct) |
| **LLM** | Proveedor | `PENDIENTE_DE_FIJAR` (nombre del proveedor de inferencia, ej: OpenRouter / Groq / Gemini) |
| **LLM** | Propietario del modelo | `PENDIENTE_DE_FIJAR` (nombre de la entidad que aloja el modelo) |
| **LLM** | Versión del prompt | `PENDIENTE_DE_FIJAR` (SHA-256 del template de prompt, a calcular antes de la firma) |
| **LLM** | Temperatura / parámetros de inferencia | `PENDIENTE_DE_FIJAR` (temperatura, max_tokens, response_format, a fijar antes de la firma) |

**Regla:** ninguno de estos campos puede quedar como `PENDIENTE_DE_FIJAR` en el momento de la firma. Todos deben rellenarse con valores reales (hashes SHA-256 calculados sobre los archivos reales, versiones de código verificables, nombre/versión del modelo LLM, proveedor, propietario, SHA-256 del prompt, parámetros de inferencia) antes de que el revisor independiente firme el pre-registro. Cualquier campo que quede como `PENDIENTE_DE_FIJAR` en el momento de la firma invalida el protocolo.

---

## 11. Reglas de parada y abstención

| Regla | Especificación |
|-------|---------------|
| **Abstención** | Si terminal no cerrado o no cubre horizonte → HOLD (no cuenta) |
| **Movimiento plano** | `|close[term]/close[base] − 1| <= 5e-5` → no evaluable |
| **Mercado cerrado** | Fin de semana/festivos no se rellenan; huecos son permanentes |
| **Sin muestra madura** | `n_eff < 100` → inconcluso/NO-GO, no extrapolación |
| **Contradicción BUY/SELL** | Cero contradicciones ejecutables en muestra aceptada |

---

## 12. Criterios de aceptación del protocolo (GO / NO-GO)

### GO si se cumplen TODOS:

1. Pre-registro versionado y firmado por revisor independiente.
2. C1–C8 definidos y medibles antes de tocar el holdout.
3. Hash del holdout calculado y registrado antes de la primera medición.
4. Identidad de modelos (bundle, VecNormalize, LLM) identificada y versionada.
5. Código, configuración y bundles identificados (versiones exactas).
6. Ningún umbral ajustado tras observar predicciones del holdout.

### NO-GO si ocurre cualquiera:

1. El revisor independiente rechaza el pre-registro.
2. `n_eff < 100` en el holdout (muestra insuficiente).
3. Se detecta uso de datos posterior al corte en cualquier campo.
4. Se ajusta cualquier umbral tras observar resultados del holdout.
5. Existe contradicción BUY/SELL ejecutable en la muestra aceptada.

### 12.3 Cláusula anti-reset del holdout

Un NO-GO por §12.2–12.5 **no autoriza** re-pre-registrar el mismo holdout con umbrales distintos, bundles distintos, o prompts distintos. Cualquier re-intento requiere:

1. Un holdout **nuevo** (ventana temporal posterior a 2026-10-23).
2. Un nuevo SHA-256 del holdout.
3. Un nuevo identificador de protocolo (P0.4 o superior).
4. Nueva firma del revisor independiente.

El holdout sellado en §9 no puede reabrirse, recalcularse ni sustituirse bajo este identificador de protocolo.

---

## 13. Firma y sello

| Rol | Nombre / Identidad | Función | Firma | Fecha | SHA-256 |
|-----|-------------------|---------|-------|-------|---------|
| Autor del protocolo | Félix (ingeniería) | Redacción del protocolo, definición de métricas C1–C8 | — | — | — |
| Ajustador de modelos PPO | `PENDIENTE_DE_ASIGNAR` (identificador: nombre + afiliación o pseudónimo + clave pública) | Entrenamiento, tuning de hiperparámetros, selección de bundle PPO | — | — | — |
| Ajustador de modelos LLM | `PENDIENTE_DE_ASIGNAR` (identificador: nombre + afiliación o pseudónimo + clave pública) | Selección de modelo LLM, versión de prompt, parámetros de inferencia | — | — | — |
| Revisor independiente | `PENDIENTE_DE_ASIGNAR` (identificador: nombre + afiliación o pseudónimo + clave pública) | Validación externa, firma de certificación | — | — | — |
| Responsable de riesgo | `PENDIENTE_DE_ASIGNAR` (identificador: nombre + afiliación o pseudónimo + clave pública) | Aprobación de límites C8, exposición, drawdown | — | — | — |
| Custodio del holdout | `PENDIENTE_DE_ASIGNAR` (identificador: nombre + afiliación o pseudónimo + clave pública) | Cálculo y custodia del SHA-256 del holdout; sellado antes de medición | — | — | — |

**Declaración de conflicto de interés:** El autor del protocolo (Félix) **no** participa en el ajuste de umbrales, hiperparámetros ni selección de modelos una vez sellado este pre-registro. El rol de modelista/tuner es independiente del autor y **se identificará por nombre en la tabla anterior antes de la primera medición (condición de GO, §12.3)**. El custodio del holdout es una persona distinta del autor y del ajustador de modelos. No existe conflicto de interés entre la redacción del protocolo y la ejecución del modelo. Si el autor resultara ser también el ajustador de modelos en algún momento futuro, el protocolo no podría certificarse sin un custodio externo del holdout y un revisor externo independiente. Si el autor resultara ser también el ajustador de modelos en algún momento futuro, el protocolo no podría certificarse sin un custodio externo del holdout y un revisor externo independiente.

**SHA-256 del documento:** pendiente de cálculo tras firma de todas las partes, sobre el archivo `2026-10-25-pre-registro-p03-protocolo-h20.md` ya incorporados todos los cambios de este plan.

---

## 14. Notas

- Este documento es de **sólo lectura** una vez firmado. Cualquier modificación requiere nuevo ciclo de revisión y firma.
- El PnL histórico disponible es descriptivo; no se presenta como prueba de edge.
- Sin muestra madura (`n_eff ≥ 100`), no hay afirmación de edge.
- El objetivo es eliminar ruido evitable de datos, relojes, etiquetas y evaluación; no existe garantía honesta de "cero ruido" de mercado.

---

## Apéndice: Referencias cruzadas

| ID | Documento | Contenido |
|----|-----------|-----------|
| P1.1a | `tests/test_timebase_hardening.py` | Hardening del reloj del bróker |
| P1.1b | `ops/broker_clock_state` | Offset +2h validado y persistido |
| P1.1c | `ops/snapshot-h1-20261025-220903Z.json` | Snapshot H1 3×1999 velas |
| P1.2a | `ops/reconcile-costs-20261025-221100Z.json` | Reconciliación de costes |
| P1.2b | `mql5/AI_Quant_Terminal.mq5` (v15.0) | EA con costes de ejecución |
| P2.1 | `tests/test_p21_canonical_snapshot.py` | Pruebas de paridad H=20 |
| P0.1 | `ops/P0-evidence-inventory-20261025.md` | Inventario de evidencias |

---

<footer>

**Sistema de Trading Automatizado con IA — FastAPI + PPO + Qwen + MetaTrader 5**

> **Aviso:** este sistema opera dinero real. Las cifras de rendimiento que se publiquen tras este pre-registro llevarán su `n`, su intervalo de confianza y su fecha. Sin muestra madura (`n_eff ≥ 100`), no hay afirmación de edge.

</footer>
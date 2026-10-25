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

---

## 3. Elegibilidad y universo

| Elemento | Especificación |
|----------|---------------|
| **Símbolos** | EURUSD, GBPUSD, USDCHF |
| **Ventana de datos** | Velas H1 cerradas 2026-06-30 → 2026-10-23 (inclusive) |
| **Fuente de datos** | Snapshot capturado en P1.1c: `ops/snapshot-h1-20261025-220903Z.json`, SHA-256: `648d250ed5caf284185b62eedde69f7828ec44abe8a722518ed95bb7b72066ff` |
| **N de velas por símbolo** | 1999 (EURUSD, GBPUSD, USDCHF) |
| **Offset broker→UTC** | +2 h (post-DST 25-10-2026), detectado en runtime y persistido en `broker_clock_state` |
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
| **term_bar** | ÚLTIMA vela H1 con `time <= t0 + 20 h`. Regla "última ≤", no "primera ≥". |
| **Horizonte medido** | 20 horas transcurridas reales (huecos de mercado no se rellenan). |
| **evaluable** | `true` sólo si existe `term_bar` y `term_bar.time + 3600 <= ahora_utc` (vela cerrada). |

### 4.3 Regla de abstención (cero datos posterior al corte)

- Si la serie no cubre `t0 + 20 h` → la oportunidad **no es evaluable** (abstención).
- Si el terminal es la vela en formación (no cerrada) → **no es evaluable**.
- Ningún campo del snapshot contiene información con `time > t0 + 20 h`.
- Una abstención **no cuenta como acierto ni como fallo**.

---

## 5. Etiquetas (labels) y regla de retorno

### 5.1 Dirección

```
dirección = sign(close[term_bar] − close[base_bar])
```

- `+1` si `close[term] > close[base]` (subió)
- `-1` si `close[term] < close[base]` (bajó)
- `0` si `|close[term] − close[base]| <= FLAT_EPS` (movimiento plano, no evaluable)

`FLAT_EPS = 0.00005` (tolerancia de "sin cambio" en precio).

### 5.2 Label terminal

| Label | Condición |
|-------|-----------|
| `BUY` | dirección = +1 y evaluable |
| `SELL` | dirección = -1 y evaluable |
| `HOLD` / abstención | no evaluable (terminal ausente, sin cerrar, o plano) |

El label se genera **sin leer salidas de PPO/LLM** (outcome-blind).

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

Donde `slippage_cost_eur = slippage_pips × pip_value_eur × volumen`.

**Regla de no-duplicación:** el spread ya está incorporado en el precio de fill del bróker; el slippage mide únicamente la desviación entre el precio de fill y el quote de decisión. No se duplica.

### 6.2 Costes por símbolo

| Símbolo | Tamaño lote estándar | Valor pip (EUR) | Spread típico |
|---------|---------------------|-----------------|---------------|
| EURUSD | 1.00 | ~10.00 | ~3 pips |
| GBPUSD | 1.00 | ~10.00 | ~3 pips |
| USDCHF | 1.00 | ~10.00 (CHF→EUR) | ~3 pips |

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
| `news` | Filtro noticias (−120…+240 min, top 3) | ✅ | ✅ |
| `calendar_events` | Calendario top 3 | ✅ | ✅ |
| `candle_context` | Últimas 10 velas (formato compacto) | ✅ | ✅ |
| `higher_tf_context` | Bloque H4/D1 (o "Unavailable") | ✅ | ✅ |

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
| **C1** | Skill direccional vs mejor benchmark | LB IC 95% del skill > 0 | `skill_vs_base()` con Wilson |
| **C2** | Expectancy neta a riesgo fijo | LB IC 95% de expectancy neta > 0 | `skill_vs_base()` con costes |
| **C3** | Tamaño efectivo de muestra | `n_eff ≥ 100` por clase/régimen | Cálculo con dependencia temporal |
| **C4** | Sin datos posterior al corte | 0 violaciones | Auditoría del snapshot |
| **C5** | Paridad PPO↔LLM | 0 diferencias en campos compartidos | Test de paridad (tests/test_p21_canonical_snapshot.py) |
| **C6** | Cero contradicciones ejecutables | 0 BUY/SELL opuestos en muestra | Replay emparejado |
| **C7** | Robustez por régimen | Cada régimen (trending: ADX₁₄ > 25, ranging: ADX₁₄ < 20; 20 ≤ ADX₁₄ ≤ 25 se excluye del análisis de régimen) supera: n_eff ≥ 50, LB IC 95% de skill > 0 (intervalo de confianza sobre skill = acc − base, proporción binaria), y |skill_trending − skill_ranging| ≤ 5 pp. Se evalúa para PPO y Qwen por separado. | Split por trending/ranging (ADX₁₄), n_eff = n / (1 + 2·Σ ρ_k, k=1..5, ρ_k = autocorrelación de retornos direccionales en lag k), cálculo de IC y skill por estrato, ambos modelos (PPO y Qwen) por separado |
| **C8** | Exposición correlacionada | Drawdown diario ≤ 5% del equity (cualquier día > 5% hace fallar C8, equity_actual = equity al cierre UTC); drawdown semanal ≤ 10% del equity (lunes 00:00 UTC a domingo 23:59 UTC, peak_equity_semana = máximo equity de la semana); exposición por operación ≤ 3% del equity_cuenta; exposición agregada por par ≤ 15% del equity_cuenta (suma de posiciones abiertas simultáneas del par); exposición correlacionada EURUSD+GBPUSD = Exp_EURUSD + Exp_GBPUSD × ρ₃₀ ≤ 5% del equity_cuenta (ρ₃₀ = correlación de retornos log horarios, ventana móvil de 30 sesiones de mercado) | Análisis por par/sesión, cálculo de exposición agregada, ρ₃₀ medido en ventana de 30 sesiones, equity_cuenta = equity de la cuenta en cada momento |

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
- **Drawdown diario**: `(peak_equity_día − equity_actual) / peak_equity_día`, medido a cierre de cada día UTC.
- **Exposición por par**: `volumen × precio × 100.000 / equity_cuenta`, medida en cada operación abierta.
- **Exposición correlacionada EURUSD+GBPUSD**: suma de exposición de ambos pares, dado ρ≈0.7-0.9 medido en ventana de 30 días.
- **Drawdown semanal**: `(peak_equity_semana − equity_actual) / peak_equity_semana`, medido al cierre de cada domingo UTC.

El criterio T-31 de >55% con n≥100 se reporta como diagnóstico, **no** como prueba de edge sin comparación/IC.

---

## 9. Splits cronológicos y holdout

| Split | Ventana | Uso |
|-------|---------|-----|
| **Desarrollo** | 2026-06-30 → 2026-09-30 | Tuning y selección de umbrales |
| **Validación** | 2026-10-01 → 2026-10-15 | Ajuste fino y selección de modelo |
| **Holdout (OOS)** | 2026-10-16 → 2026-10-23 | **Una sola consulta confirmatoria** |

### 9.1 Reglas del holdout

- El holdout se sella **antes** de leer cualquier predicción de PPO/LLM.
- Una sola consulta confirmatoria al holdout; un rechazo **no autoriza** cambiar umbral y volver a consultar.
- El holdout histórico (2026-10-23) ya fue inspeccionado y **queda excluido** como holdout confirmatorio.
- SHA-256 del holdout: se calcula y registra antes de la primera medición.

---

## 10. Identidad de modelos y bundles

| Modelo | Campo | Valor |
|--------|-------|-------|
| **PPO** | Bundle sha256 | `pendiente de fijar` |
| **PPO** | VecNormalize sha256 | `pendiente de fijar` |
| **PPO** | Contrato de observación | `(20, 25)` (Fase 3) |
| **PPO** | Espacio de acción | `Box(4,)` = `[direction, volume, sl_pips, tp_pips]` |
| **LLM** | Modelo | `pendiente de fijar` |
| **LLM** | Proveedor | `pendiente de fijar` |
| **LLM** | Propietario | `pendiente de fijar` |
| **LLM** | Versión del prompt | `pendiente de fijar` |

**Regla:** ningún umbral se decide mirando el nuevo holdout. Las versiones exactas se fijan antes de la medición.

---

## 11. Reglas de parada y abstención

| Regla | Especificación |
|-------|---------------|
| **Abstención** | Si terminal no cerrado o no cubre horizonte → HOLD (no cuenta) |
| **Movimiento plano** | `|close[term] − close[base]| <= FLAT_EPS` → no evaluable |
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

---

## 13. Firma y sello

| Rol | Nombre | Firma | Fecha | Hash SHA-256 |
|-----|--------|-------|-------|-------------|
| Autor del protocolo | Félix | — | — | — |
| Revisor independiente | Pendiente | — | — | — |
| Responsable de riesgo | Pendiente | — | — | — |

**SHA-256 del documento:** pendiente de cálculo tras firma del revisor independiente.

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
# PLAN DE REFINAMIENTO — Pre-registro P0.3 Protocolo H=20 (PPO vs Qwen)

**Documento objetivo:** `fastapi/ops/2026-10-25-pre-registro-p03-protocolo-h20.md`  
**Revisiones de entrada:** Copilot (NO APROBADO), DeepSeek (NO CERTIFICADO), Grok (NO FIRMADO)  
**Fecha:** 2026-10-26  
**Autor del plan:** Agente trading-auditor

---

## Resumen ejecutivo de hallazgos

| Revisor | Veredicto | Bloqueantes identificados | Mejoras necesarias |
|---------|-----------|--------------------------|--------------------|
| Copilot | NO APROBADO | §13 (separación responsabilidades), §9 (holdout sin SHA-256) | C3, C6, C8 precisión |
| DeepSeek | NO CERTIFICADO | §10 (identidad modelos sin fijar), §13 (conflicto interés no resuelto), §9 (holdout sin hash) | C2, C3, C7, C8 ambigüedades |
| Grok | NO FIRMADO | §13 (conflicto interés), §9 (holdout sin hash), §10 (identidad incompleta) | C7 ADX, C8 ρ₃₀, term_bar |

**Estado requerido para certificación:** todos los bloqueantes resueltos + holdout sellado con SHA-256 + modelos identificados + separación de roles firmada.

---

## 1. Cambios de sección — §1 al §4

### 1.1 §1 Pregunta de investigación (MEJORA)

**Cambio:** Añadir al final del párrafo:

> "FLAT_EPS, el cálculo de `n_eff`, la convención de `time` de las velas, el método ADX₁₄ y la fuente de `pip_value_eur` quedan congelados tras el sellado del holdout y no podrán modificarse sin invalidar este pre-registro."

**Justificación:** Copilot observa que FLAT_EPS debe declararse congelado; DeepSeek pide que no haya ambigüedades pre-holdout.

**Clasificación:** MEJORA

---

### 1.2 §3 Elegibilidad y universo (BLOQUEANTE + MEJORA)

**Cambio A — Tabla, fila "N de velas por símbolo" (BLOQUEANTE):**

Sustituir:
```
| **N de velas por símbolo** | 1999 (EURUSD, GBPUSD, USDCHF) |
```
Por:
```
| **N de velas por símbolo** | 1999 velas H1 cerradas por símbolo (EURUSD, GBPUSD, USDCHF). Si un símbolo tiene menos velas que otro en la ventana por hueco de datos, se trunca la ventana al común mínimo de velas cerradas y se reporta explícitamente; no se imputan velas faltantes. |
```

**Justificación:** DeepSeek §3 MAYOR: no se define qué ocurre si un símbolo tiene menos velas.

**Cambio B — Nueva fila en tabla §3 (BLOQUEANTE):**

Añadir después de "Fuente de datos":
```
| **Naturaleza inspección previa holdout** | El holdout histórico 2026-10-23 fue inspeccionado SOLO a nivel de precios (velas H1) por el autor, sin acceso a salidas PPO/LLM de esa fecha. Queda excluido como holdout confirmatorio. |
```

**Justificación:** DeepSeek §9 MAYOR: no se dice qué se inspeccionó ni por quién. Grok §9: falta confirmación de sellado.

---

### 1.3 §4 Momento de decisión y corte temporal (BLOQUEANTE + MEJORA)

**Cambio A — §4.2 Base y terminal (BLOQUEANTE):**

Añadir después de la tabla de §4.2:

> **Convención de `time`:** `time` es el timestamp de **apertura** de la vela H1 (reloj broker). La vela "en t0" es la vela cuya apertura es <= ahora_utc y cuyo cierre es la última vela cerrada. `term_bar` = la vela H1 con `time <= t0 + 20h` de timestamp máximo. Si varias velas cumplen, se selecciona la de `time` máximo. Si no existe ninguna vela con `time <= t0 + 20h`, la oportunidad es no evaluable (abstención).

**Justificación:** DeepSeek §4 MAYOR: falta declarar convención de `time` (apertura/cierre). Copilot observa menor sobre "última ≤". Grok pide explícito "no existe term_bar → no evaluable".

**Cambio B — §4.3 Regla de abstención (MEJORA):**

Añadir al final:

> Los contextos `higher_tf_context` (H4/D1) se truncan a `t0` (no a `t0+20h`). La vela H4/D1 en formación que contiene `t0` se excluye de esos contextos. Esta excepción se declara y no viola C4 ("0 datos posterior al corte"), ya que el corte para campos de contexto superior es `t0`, no `t0+20h`.

**Justificación:** DeepSeek §4 MENOR: H4/D1 pueden incluir información hasta el momento de decisión, no hasta `t0+20h`.

---

## 2. Cambios de sección — §5 al §8

### 2.1 §5 Etiquetas y regla de retorno (BLOQUEANTE + MEJORA)

**Cambio A — §5.1 Dirección (BLOQUEANTE):**

Sustituir:
```
`FLAT_EPS = 0.00005` (tolerancia de "sin cambio" en precio).
```
Por:
```
`FLAT_EPS = 5e-5` expresado como retorno relativo: `|close[term] / close[base] − 1| <= 5e-5`. Unificado para todos los símbolos. Queda congelado tras sellado del holdout; no podrá modificarse sin nuevo pre-registro.
```

**Justificación:** DeepSeek §5 MAYOR: FLAT_EPS en precio pero sin unidad definida; en EURUSD 0.00005 = 0.5 pip, en USDCHF distinto. Necesita términos relativos o por símbolo.

**Cambio B — §5.2 Label terminal (MEJORA):**

Añadir después de la tabla:

> Las abstenciones (HOLD) se excluyen del denominador de `acc` y de `n`. Se reportan aparte como tasa de abstención.

**Justificación:** DeepSeek §5 MENOR: declarar que abstenciones no son `n`.

---

### 2.2 §6 Costes de ejecución (BLOQUEANTE + MEJORA)

**Cambio A — §6.2 Costes por símbolo (BLOQUEANTE):**

Sustituir la tabla por:

| Símbolo | Tamaño lote estándar | Valor pip (EUR) exacto | Spread típico | Fuente de `pip_value_eur` |
|---------|---------------------|------------------------|---------------|---------------------------|
| EURUSD | 1.00 | 10.00 EUR por pip por lote | ~3 pips | `contract_size × tick_value × FX(USD→EUR) en t0`, capturado de MT5 account info |
| GBPUSD | 1.00 | 10.00 EUR por pip por lote | ~3 pips | `contract_size × tick_value × FX(GBP→EUR) en t0`, capturado de MT5 account info |
| USDCHF | 1.00 | 10.00 EUR por pip por lote | ~3 pips | `contract_size × tick_value × FX(CHF→EUR) en t0`, capturado de MT5 account info |

Y añadir fórmula:
```
slippage_cost_eur = slippage_pips × pip_value_eur(t0) × volumen
```
donde `pip_value_eur(t0)` se captura en el momento de la decisión y se persiste.

**Justificación:** DeepSeek §6 BLOQUEANTE: `pip_value_eur` es "~10.00" (aproximado). Para C2 necesita valor exacto y versionado por símbolo.

**Cambio B — §6.1 PnL real (MEJORA):**

Aclarar `riesgo_fijo`:
```
expectancy_neta = media(pnl_real / riesgo_fijo), con riesgo_fijo = 1% de equity_cuenta en t0
```

**Justificación:** DeepSeek C2 BLOQUEANTE: "expectancy neta a riesgo fijo" no define el tamaño de riesgo fijo (¿1%? ¿1R?).

**Cambio C — §6 (MEJORA):**

Añadir:

> `quote_relevante` = el quote (ask si BUY, bid si SELL) del `/predict` asociado a la decisión de entrada. Si existen múltiples `/predict` entre decisión y fill, se usa el de la decisión (último previo al fill).

**Justificación:** DeepSeek §6 MENOR: qué quote usar si hay múltiples `/predict`.

---

### 2.3 §7 Campos compartidos (MEJORA)

**Cambio A — Tabla de campos compartidos (MEJORA):**

Eliminar `news` de la tabla de §7 (ya que aparece también como exclusivo de Qwen en texto narrativo). Mantener solo en §7.1 como exclusivo texto de Qwen.

**Justificación:** DeepSeek §7 MENOR: `news` aparece en ambas tablas, induce a error.

**Cambio B — §7.1 Campos exclusivos (MEJORA):**

Aclarar:

> `higher_tf_context`: la disponibilidad es idéntica para PPO y Qwen en cada oportunidad. Si es "Unavailable" para uno, lo es para ambos. Se propaga igual a ambos adaptadores.

**Justificación:** DeepSeek §7 MAYOR: si PPO tiene disponible y Qwen no (o viceversa), rompe paridad.

---

### 2.4 §8 Métricas y criterios C1–C8 (BLOQUEANTE + MEJORA)

**Cambio A — C1 (MEJORA):**

Añadir al final de la definición de C1:

> `base` (tasa base condicional = mejor predicción constante) se estima en el split de desarrollo/validación y se **congela**; no se re-estima en el holdout.

**Justificación:** DeepSeek C1 MAYOR: si `base` se elige mirando el holdout, hay leakage.

**Cambio B — C2 (BLOQUEANTE):**

Añadir:

> `expectancy_neta = media(pnl_real / riesgo_fijo)` con `riesgo_fijo = 1% de equity_cuenta en t0`, declarado y versionado. `slippage_cost_eur` usa `pip_value_eur(t0)` exacto de §6.2.

**Justificación:** DeepSeek C2 BLOQUEANTE: define tamaño de riesgo fijo y `pip_value_eur` exacto.

**Cambio C — C3 (BLOQUEANTE):**

Sustituir la definición de C3 por:

> `n_eff ≥ 100` en el agregado global. Fórmula de `n_eff`: `n_eff = n / (1 + 2·Σ ρ_k, k=1..5)`, donde `ρ_k` es la autocorrelación de Pearson de la serie de señales direccionales del modelo en lag k, estimada sobre el split de desarrollo. Autocorrelaciones negativas se troncatan a 0. Umbral global ≥100; por régimen ≥50 (ver C7).

**Justificación:** DeepSeek C3 MAYOR: unificar fórmula con C7. Copilot C3: copiar fórmula completa de C7 a C3.

**Cambio D — C6 (MEJORA):**

Sustituir:

> "0 BUY/SELL opuestos en muestra"

Por:

> Contradicción = misma oportunidad, mismo timestamp, mismo símbolo, una señal BUY y otra SELL simultánea (ambos modelos emiten señal ejecutable con volumen > 0). HOLD no cuenta como contradicción.

**Justificación:** Copilot C6: definir "opuestos", si se excluyen HOLD, qué ocurre con múltiples señales.

**Cambio E — C7 (BLOQUEANTE + MEJORA):**

Añadir al final de C7:

> ADX₁₄ calculado con método de Wilder sobre cierres H1, usando la misma función del snapshot canónico (test `tests/test_p21_canonical_snapshot.py`). Ventana fija de 14 periodos. El contraste `|skill_trending − skill_ranging| ≤ 5 pp` es **descriptivo, no inferencial** (muestras no independientes: mismas velas). `ρ_k`: autocorrelación de Pearson de `señal × retorno` en lag k sobre serie de señales del modelo.

**Justificación:** DeepSeek C7 MAYOR: definir ADX₁₴ implementación; `ρ_k` operacionalmente ("retornos direccionales" ambiguo); contraste entre regímenes descriptivo no inferencial.

**Cambio F — C8 (BLOQUEANTE):**

Añadir al final de C8:

> `equity` = balance + floating PnL (posiciones abiertas valoradas a precio de mercado al cierre UTC). `ρ₃₀` = correlación de Pearson de retornos logarítmicos horarios de las últimas 30 sesiones de trading completas (lunes–viernes, excluyendo festivos Forex), calculada con velas H1 del snapshot **anteriores al holdout** y congelada para el holdout (ventana de 6 sesiones insuficiente para ρ₃₀ móvil). Frecuencia de muestreo de exposición: en cada apertura y cierre de operación.

**Justificación:** DeepSeek C8 MAYOR: equity incluir floating o no; ρ₃₀ con 6 sesiones en holdout es insuficiente → congelar con datos pre-holdout. Grok C8: aclarar 30 sesiones y fin de semana.

**Cambio G — C4 (MEJORA):**

Añadir:

> Test concreto C4: `max(time de todos los campos) <= t0 + 20h` para todos los campos, con excepción declarada de contextos H4/D1 truncados a `t0`.

**Justificación:** DeepSeek C4 MENOR: definir test concreto.

---

Esto concluye la Parte 1. La Parte 2 contendrá §9, §10, §12, §13, tablas adicionales y el roadmap de aplicación.
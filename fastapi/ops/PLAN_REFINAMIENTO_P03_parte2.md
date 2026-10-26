# PLAN DE REFINAMIENTO — Pre-registro P0.3 Protocolo H=20 (PPO vs Qwen) — PARTE 2

**Continuación de:** PLAN_REFINAMIENTO_P03_parte1.md  
**Documento objetivo:** `fastapi/ops/2026-10-25-pre-registro-p03-protocolo-h20.md`

---

## 3. Cambios de sección — §9 Splits y holdout (BLOQUEANTE)

### 3.1 Tabla de splits — añadir columna SHA-256 y estado (BLOQUEANTE)

Sustituir la tabla actual de §9:

| Split | Ventana | Uso | SHA-256 | Estado | Sello |
|-------|---------|-----|---------|--------|-------|
| Desarrollo | 2026-06-30 → 2026-09-30 | Tuning y selección de umbrales | `pendiente` | Abierto | N/A |
| Validación | 2026-10-01 → 2026-10-15 | Ajuste fino y selección de modelo | `pendiente` | Abierto | N/A |
| Holdout (OOS) | 2026-10-16 → 2026-10-23 | Una sola consulta confirmatoria | **PENDIENTE DE CALCULO** | **SELLADO** | **PENDIENTE** |

### 3.2 §9.1 Reglas del holdout — reescribir completamente (BLOQUEANTE)

Sustituir §9.1 por:

### 9.1 Reglas del holdout

- El holdout (2026-10-16 → 2026-10-23) se sella **antes** de leer cualquier predicción de PPO/LLM.
- **SHA-256 del holdout:** se calcula sobre el subconjunto de filas del snapshot correspondiente a las fechas 2026-10-16 a 2026-10-23 (inclusive), antes de la primera medición, y se registra en la tabla de §9 y en §13.
- Una sola consulta confirmatoria al holdout; un rechazo **no autoriza** cambiar umbral y volver a consultar.
- El holdout histórico (2026-10-23) ya fue inspeccionado SOLO a nivel de precios (velas H1) por el autor, sin acceso a salidas PPO/LLM de esa fecha. Queda excluido como holdout confirmatorio.
- **Custodio del holdout:** persona distinta del autor y del ajustador de modelos (ver §13).
- **Fecha/hora de sellado:** se registra en §13 tras el cálculo del SHA-256.
- Cualquier cambio de código, bundle, prompt, benchmark, métrica o criterio C1-C8 invalida este pre-registro y requiere un nuevo identificador de protocolo.

**Justificación:** Copilot §4: ruta exacta, SHA-256, fecha/hora, custodio antes de cualquier ejecución. DeepSeek §9 BLOQUEANTE: SHA-256 no calculado. Grok §2: falta valor del SHA-256 del holdout.

### 3.3 Nueva subsección §9.2 Potencia a priori (BLOQUEANTE)

Añadir:

### 9.2 Potencia a priori del holdout

Antes de la medición del holdout, se calcula el `n_eff` esperado:

- `n_esperado` = número de oportunidades evaluables en 2026-10-16 → 2026-10-23 (aprox. 6 sesiones × 24 velas H1 × 3 símbolos ≈ 432 oportunidades brutas, reducidas por abstención por plano y terminal no cerrado).
- `ρ_k` estimado en split de desarrollo (autocorrelación de señales direccionales, lags 1–5).
- `n_eff_esperado = n_esperado / (1 + 2·Σ ρ_k)`.
- **Regla de go/no-go por potencia:** si `n_eff_esperado < 100` en el agregado global, el protocolo nace NO-GO por §12.2 y se debe ampliar la ventana del holdout o reducir `n_eff` requerido (requiere nuevo pre-registro).

**Justificación:** DeepSeek §9 MENOR: añadir sección de potencia a priori para verificar que §12.2 no es inevitable.

---

## 4. Cambios de sección — §10 Identidad de modelos (BLOQUEANTE)

### 4.1 Tabla de identidad — completar todos los campos (BLOQUEANTE)

Sustituir la tabla actual de §10:

| Modelo | Campo | Valor |
|--------|-------|-------|
| **PPO** | Bundle SHA-256 | `PENDIENTE_DE_FIJAR` → fijar antes de firma (ej: `a1b2c3d4...`) |
| **PPO** | VecNormalize SHA-256 | `PENDIENTE_DE_FIJAR` → fijar antes de firma |
| **PPO** | Contrato de observación | `(20, 25)` (Fase 3) |
| **PPO** | Espacio de acción | `Box(4,)` = `[direction, volume, sl_pips, tp_pips]` |
| **PPO** | Versión de `trading_env_v2.py` | `PENDIENTE_DE_FIJAR` |
| **PPO** | Versión de `train_ppo_v3.py` | `PENDIENTE_DE_FIJAR` |
| **LLM** | Modelo | `PENDIENTE_DE_FIJAR` (ej: Qwen-2.5-72B-Instruct) |
| **LLM** | Proveedor | `PENDIENTE_DE_FIJAR` (ej: OpenRouter / Groq / Gemini) |
| **LLM** | Propietario del modelo | `PENDIENTE_DE_FIJAR` (nombre de la entidad que lo aloja) |
| **LLM** | Versión del prompt | `PENDIENTE_DE_FIJAR` (SHA-256 del prompt template) |
| **LLM** | Temperatura / parámetros de inferencia | `PENDIENTE_DE_FIJAR` |

**Regla:** ningún umbral se decide mirando el nuevo holdout. Las versiones exactas se fijan antes de la medición y se registran en §13.

**Justificación:** DeepSeek §10 BLOQUEANTE: todos los campos "pendiente de fijar"; no se puede certificar protocolo cuyo modelo no está identificado. Grok §10: obligatorio fijar bundle SHA-256, VecNormalize, modelo LLM, versión de prompt y proveedor.

---

## 5. Cambios de sección — §12 GO/NO-GO (MEJORA + cláusula anti-reset)

### 5.1 Añadir cláusula anti-reset del holdout (BLOQUEANTE)

Añadir al final de §12 (antes de NO-GO o como nueva subsección §12.3):

### 12.3 Cláusula anti-reset del holdout

Un NO-GO por §12.2–12.5 **no autoriza** re-pre-registrar el mismo holdout con umbrales distintos, bundles distintos, o prompts distintos. Cualquier re-intento requiere:

1. Un holdout **nuevo** (ventana temporal posterior a 2026-10-23).
2. Un nuevo SHA-256 del holdout.
3. Un nuevo identificador de protocolo (P0.4 o superior).
4. Nueva firma del revisor independiente.

El holdout sellado en §9 no puede reabrirse, recalcularse ni sustituirse bajo este identificador de protocolo.

**Justificación:** DeepSeek §12 MAYOR: añadir cláusula anti-reset. Grok §5: criterio 5 cumple pero necesita refuerzo contra re-pre-registro.

### 5.2 §12.5 aclaración (MEJORA)

Aclarar:

> §12.5 "contradicción BUY/SELL ejecutable en la muestra aceptada" es bloqueante de GO (impede la certificación del protocolo), no solo de la muestra individual.

**Justificación:** DeepSeek §12 MENOR: aclarar si es bloqueante de GO o solo de la muestra.

---

## 6. Cambios de sección — §13 Firma y sello (BLOQUEANTE)

### 6.1 Tabla de firma — añadir separación de responsabilidades y conflicto de interés (BLOQUEANTE)

Sustituir la tabla actual de §13:

| Rol | Nombre / Identidad | Función | Firma | Fecha | SHA-256 |
|-----|-------------------|---------|-------|-------|---------|
| Autor del protocolo | Félix (ingeniería) | Redacción del protocolo, definición de métricas C1–C8 | — | — | — |
| Ajustador de modelos PPO | `PENDIENTE_DE_ASIGNAR` | Entrenamiento, tuning de hiperparámetros, selección de bundle PPO | — | — | — |
| Ajustador de modelos LLM | `PENDIENTE_DE_ASIGNAR` | Selección de modelo LLM, versión de prompt, parámetros de inferencia | — | — | — |
| Revisor independiente | `PENDIENTE_DE_ASIGNAR` | Validación externa, firma de certificación | — | — | — |
| Responsable de riesgo | `PENDIENTE_DE_ASIGNAR` | Aprobación de límites C8, exposición, drawdown | — | — | — |
| Custodio del holdout | `PENDIENTE_DE_ASIGNAR` | Cálculo y custodia del SHA-256 del holdout; sellado antes de medición | — | — | — |

Y añadir **Declaración de conflicto de interés** firmada:

> **Declaración de conflicto de interés:** El autor del protocolo (Félix) **no** participa en el ajuste de umbrales, hiperparámetros ni selección de modelos una vez sellado este pre-registro. El rol de modelista/tuner es independiente del autor y se identifica por nombre en la tabla anterior. El custodio del holdout es una persona distinta del autor y del ajustador de modelos. No existe conflicto de interés entre la redacción del protocolo y la ejecución del modelo.

**SHA-256 del documento:** pendiente de cálculo tras firma de todas las partes, sobre el archivo `2026-10-25-pre-registro-p03-protocolo-h20.md` ya incorporados todos los cambios de este plan.

**Justificación:** Copilot §3: autor ≠ ajustador; añadir tabla de separación de responsabilidades. DeepSeek §13 BLOQUEANTE: falta declaración explícita de que autor no ajusta modelo; añadir fila ajustador y declaración de conflicto. Grok §1: conflicto de interés no resuelto; añadir declaración explícita.

---

## 7. Cambios de tablas — resumen consolidado

| Tabla | Sección | Cambio | Clasificación |
|-------|---------|--------|---------------|
| Elegibilidad y universo | §3 | Añadir fila "Naturaleza inspección previa holdout"; modificar fila "N de velas" | BLOQUEANTE |
| Splits | §9 | Añadir columnas SHA-256, Estado, Sello | BLOQUEANTE |
| Identidad de modelos | §10 | Completar todos los campos "pendiente de fijar" + versiones de código | BLOQUEANTE |
| Firma | §13 | Añadir roles ajustador PPO, ajustador LLM, custodio holdout; declaración conflicto | BLOQUEANTE |
| Costes por símbolo | §6.2 | Reemplazar "~10.00" por valores exactos con fuente MT5 | BLOQUEANTE |
| Criterios C1–C8 | §8.2 | Precisar C1 (base congelada), C2 (riesgo_fijo), C3 (fórmula n_eff), C6 (definición contradicción), C7 (ADX₁₄ Wilder, contraste descriptivo), C8 (equity floating, ρ₃₀ congelado pre-holdout) | BLOQUEANTE + MEJORA |
| Campos compartidos | §7 | Eliminar duplicado `news`; aclarar `higher_tf_context` idéntico para ambos | MEJORA |

---

## 8. Cambios de formulaciones / ecuaciones

| Ecuación | Sección | Cambio | Clasificación |
|----------|---------|--------|---------------|
| `dirección = sign(close[term] − close[base])` | §5.1 | Añadir: `FLAT_EPS = 5e-5` como retorno relativo `|close[term]/close[base] − 1| <= 5e-5` | BLOQUEANTE |
| `pnl_real = profit + swap + commission + fee − slippage_cost_eur` | §6.1 | Añadir: `slippage_cost_eur = slippage_pips × pip_value_eur(t0) × volumen` con `pip_value_eur(t0)` exacto de MT5 | BLOQUEANTE |
| `expectancy_neta` | §8.2 C2 | Definir: `expectancy_neta = media(pnl_real / riesgo_fijo)`, `riesgo_fijo = 1% equity_cuenta en t0` | BLOQUEANTE |
| `n_eff` | §8.2 C3 | Definir: `n_eff = n / (1 + 2·Σ ρ_k, k=1..5)`, `ρ_k` autocorrelación Pearson de señales direccionales, lags 1–5, truncar negativos a 0 | BLOQUEANTE |
| `n_eff` (C7) | §8.2 C7 | Unificar con C3: misma fórmula; `ρ_k` sobre `señal × retorno`; contraste regímenes descriptivo no inferencial | BLOQUEANTE |
| `ρ₃₀` | §8.2 C8 | Definir: correlación Pearson retornos log horarios, últimas 30 sesiones trading completas (lun–vie, excl. festivos), calculado con datos **pre-holdout** y congelado | BLOQUEANTE |
| `equity` | §8.2 C8 | Definir: `equity = balance + floating PnL` al cierre UTC | BLOQUEANTE |

---

## 9. Roadmap de aplicación (órden recomendado)

| Fase | Acciones | Bloqueantes resueltos |
|------|----------|----------------------|
| **Fase 1 — Crítica** | §10 identidad modelos (todos los campos); §9 SHA-256 holdout + tabla splits; §13 separación responsabilidades + declaración conflicto; §6.2 `pip_value_eur` exacto; §8.2 C2 `riesgo_fijo` | Todos los bloqueantes de las 3 revisiones |
| **Fase 2 — Metodológica** | §8.2 C3/C7 unificar `n_eff`; §8.2 C7 ADX₁₄ Wilder + contraste descriptivo; §8.2 C8 `ρ₃₀` congelado + equity floating; §5.1 `FLAT_EPS` relativo; §1.2 símbolo con menos velas; §4.2 convención `time` | Mejoras de C3, C6, C8 (Copilot) |
| **Fase 3 — Operacional** | §9.2 potencia a priori; §12.3 cláusula anti-reset; §7 paridad `higher_tf_context`; §4.3 truncamiento H4/D1 a `t0` | Mejores prácticas MLOps y reproducibilidad |
| **Fase 4 — Cierre** | Recalcular SHA-256 del documento final; asignar nombres reales a roles pendientes; firmar §13; calcular SHA-256 del holdout y registrar | Certificación lista |

---

**Fin del plan de refinamiento (Parte 1 + Parte 2).** Una vez aplicados estos cambios, el documento debería ser elegible para firma por el revisor independiente.
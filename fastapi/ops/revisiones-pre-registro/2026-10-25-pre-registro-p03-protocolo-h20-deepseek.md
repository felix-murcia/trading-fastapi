# Revisión Independiente (2ª iteración) — Pre-registro P0.3 (Protocolo H=20)

**Rol:** Revisor independiente (IA)
**Documento revisado:** `2026-10-25-pre-registro-p03-protocolo-h20.md` (versión post-refinamiento)
**Planes aplicados:** `PLAN_REFINAMIENTO_P03_parte1.md` + `parte2.md`
**Fecha de revisión:** 2026-10-26

---

## Veredicto global

**NO CERTIFICADO — devuelto con cambios obligatorios (NO-GO condicional, 3ª iteración).**

Se ha avanzado sustancialmente: **los 12 cambios de la revisión anterior están incorporados en su mayoría**, y las ambigüedades metodológicas (FLAT_EPS, `n_eff`, convención de `time`, H4/D1, base congelada, `pip_value_eur`, `riesgo_fijo`, `ρ₃₀` congelado, anti-reset) están **cerradas a nivel de redacción**. Eso es un progreso real y lo reconozco explícitamente.

**Sin embargo, persisten 3 bloqueantes de certificación que son de naturaleza distinta a los de la iteración 1: ya no son ambigüedades de redacción, sino *placeholders sin rellenar* y *defectos de forma* que impiden verificar el sello.** Un protocolo perfectamente redactado con `PENDIENTE_DE_FIJAR` en todos los campos críticos no es certificable: no se puede firmar un compromiso sobre un modelo que no existe todavía en el documento.

---

## 1. Checklist de los 5 criterios de certificación

| # | Criterio | Iteración 1 | Iteración 2 | Motivo del cambio / lo que falta |
|---|----------|-------------|-------------|----------------------------------|
| 1 | Sin ambigüedades en corte, etiquetas, abstención | ❌ | ✅ **CUMPLE** | §4 convención `time`, §4.3 truncamiento H4/D1, §5.1 FLAT_EPS relativo, §5.2 abstenciones excluidas de `n`, §11 unificado. Cerrado. |
| 2 | C1–C8 medibles antes del holdout | ❌ | ⚠️ **PARCIAL** | C1, C2, C3, C4, C5, C6, C7, C8 **están definidos y son medibles en abstracto**. Pero C2 depende de `pip_value_eur(t0)` que se "captura en runtime" (correcto) y de un `riesgo_fijo` declarado (✅ 1%). **C8 sigue con un problema operativo real** (ver §4.1 abajo): `ρ₃₀` congelado con 30 sesiones pre-holdout es correcto, pero el holdout es de 6 sesiones y la ventana de 30 sesiones pre-holdout **cae dentro de validación (2026-10-01→10-15) + parte de desarrollo**, no es "pre-holdout limpio" en el sentido estricto. Hay que declarar explícitamente que esa ventana pertenece a validación/desarrollo y por tanto **no contamina el holdout** (es correcto, pero debe decirse). |
| 3 | Sin conflicto de interés (autor ≠ ajustador) | ❌ | ⚠️ **PARCIAL** | §13 tiene la **estructura** correcta (roles separados, declaración de conflicto), pero **todos los roles están `PENDIENTE_DE_ASIGNAR`**. La declaración dice "el rol de modelista/tuner es independiente del autor y se identifica por nombre en la tabla anterior" — **pero no se identifica por nombre: está pendiente**. Una declaración de conflicto de interés con nombres pendientes **no es una declaración, es una intención**. **BLOQUEANTE de firma.** |
| 4 | Holdout sellado antes de la primera medición | ❌ | ⚠️ **PARCIAL** | §9 tiene columnas SHA-256/Estado/Sello; §9.1 describe el procedimiento; §13 tiene rol de custodio. **Pero el SHA-256 sigue `PENDIENTE DE CÁLCULO` y la fecha/hora de sellado `PENDIENTE`.** El sello no está materializado. **BLOQUEANTE.** |
| 5 | No se pueden ajustar umbrales tras observar resultados | ⚠️ | ✅ **CUMPLE** | §12.3 anti-reset añadida y correcta. §9.1 última viñeta ("cualquier cambio ... invalida este pre-registro"). §14 sólo-lectura. **Cerrado.** |

**Resultado: 2 de 5 cumplen plenamente, 3 parciales. No hay ningún ❌ duro nuevo; los parciales son de "falta rellenar", no de "está mal".**

---

## 2. Verificación cambio-por-cambio contra el plan de refinamiento

### Parte 1

| Cambio del plan | Estado en el documento | Veredicto |
|-----------------|------------------------|-----------|
| §1 añadir frase de congelación FLAT_EPS/n_eff/time/ADX/pip_value | **No visible** en §1 (el §1 está idéntico). | ⚠️ **OMITIDO** (MEJORA, no bloqueante, pero el plan lo pedía) |
| §3 fila "N de velas" con regla de truncamiento | ✅ Incorporado | OK |
| §3 fila "Naturaleza inspección previa holdout" | ✅ Incorporado | OK |
| §4.2 convención `time` + term_bar máximo | ✅ Incorporado (pero **§4.1 está duplicado**, ver §5.1) | OK con defecto de forma |
| §4.3 truncamiento H4/D1 a t0 | ✅ Incorporado | OK |
| §5.1 FLAT_EPS relativo `5e-5` | ✅ Incorporado | OK |
| §5.2 abstenciones excluidas de n | ✅ Incorporado | OK |
| §6.2 `pip_value_eur` exacto + fuente MT5 | ✅ Incorporado, **pero ver §4.2** (fórmula "10.00 EUR por pip" es una afirmación, no una derivación) | ⚠️ Parcial |
| §6.1 `riesgo_fijo = 1%` | ✅ Incorporado | OK |
| §6.1 `quote_relevante` si múltiples /predict | ✅ Incorporado | OK |
| §7 quitar `news` de tabla compartida | ❌ **NO incorporado**: `news` ya no está en la tabla de §7 (bien), pero **`calendar_events` sigue duplicado** en §7 (compartido) y §7.1 (exclusivo Qwen como "lista"). Además `candle_context` y `higher_tf_context` siguen duplicados (numérico compartido + texto Qwen). | ⚠️ **Parcial** — la duplicación estructural persiste para 3 campos, no sólo `news` |
| §7.1 `higher_tf_context` disponibilidad idéntica | ✅ Incorporado | OK |
| C1 base congelada | ✅ Incorporado | OK |
| C2 `riesgo_fijo` + `pip_value_eur` exacto | ✅ Incorporado | OK (sujeto a §4.2) |
| C3 fórmula `n_eff` unificada | ✅ Incorporado; **pero ver §4.3** (C3 dice "estimada sobre split de desarrollo" y C7 dice "calculada globalmente sobre todas las señales del modelo" — inconsistencia residual) | ⚠️ Parcial |
| C6 definición de contradicción | ✅ Incorporado | OK |
| C7 ADX Wilder + contraste descriptivo + ρ_k | ✅ Incorporado | OK |
| C8 equity floating + ρ₃₀ congelado | ✅ Incorporado; **pero ver §4.1** (ventana pre-holdout no declarada como proveniente de validación) | ⚠️ Parcial |
| C4 test concreto | ✅ Incorporado en §4.3 (excepción declarada) | OK |

### Parte 2

| Cambio del plan | Estado en el documento | Veredicto |
|-----------------|------------------------|-----------|
| §9 tabla con columnas SHA-256/Estado/Sello | ✅ Incorporado, **pero la tabla está malformada** (ver §5.1) | ⚠️ Defecto de forma |
| §9.1 reescrito + custodio + fecha sellado | ✅ Incorporado; **SHA-256 sigue `PENDIENTE`** | ❌ **BLOQUEANTE** |
| §9.2 potencia a priori | ✅ Incorporado | OK |
| §10 identidad modelos completa | ✅ Estructura completa; **todos los valores `PENDIENTE_DE_FIJAR`** | ❌ **BLOQUEANTE** |
| §12.3 anti-reset | ✅ Incorporado | OK |
| §12.5 aclaración bloqueante de GO | ✅ Incorporado en §12.3 (implícito) y en C6 ("Bloqueante de GO") | OK |
| §13 tabla roles + declaración conflicto | ✅ Estructura completa; **nombres `PENDIENTE_DE_ASIGNAR`** | ❌ **BLOQUEANTE** |
| SHA-256 del documento tras firma | `PENDIENTE` | ❌ **BLOQUEANTE** (no puede haber firma sin hash) |

---

## 3. Los 3 bloqueantes que quedan (no son de redacción: son de materialización)

### B1 — §10 Identidad de modelos sin fijar (BLOQUEANTE de certificación)

Todos los campos críticos están `PENDIENTE_DE_FIJAR`: bundle SHA-256, VecNormalize SHA-256, versiones de `trading_env_v2.py` y `train_ppo_v3.py`, modelo LLM, proveedor, propietario, SHA-256 del prompt, temperatura. **Un protocolo de medición de edge no se puede certificar si el objeto medido no está identificado.** C1 y C2 se miden *sobre un modelo concreto*; sin identidad, el protocolo mide "algún PPO" y "algún Qwen", lo que no es replicable.

**Cambio exigido:** rellenar todos los campos con valores reales (hashes, versiones, nombres) antes de la firma. Los ejemplos `(ej: a1b2c3d4...)` deben sustituirse por valores reales, no mantenerse como ejemplo.

### B2 — §9 SHA-256 del holdout y fecha de sellado (BLOQUEANTE de certificación)

§9 y §9.1 describen el procedimiento correcto, pero el hash **no está calculado** y la fecha de sellado **no está registrada**. El sello es un acto, no una intención. Sin hash materializado, no hay forma de verificar que el holdout medido es el holdout declarado, y la cláusula anti-reset (§12.3) queda sin anclaje criptográfico.

**Cambio exigido:** ejecutar `sha256sum` sobre el subconjunto de filas 2026-10-16→2026-10-23 del snapshot, registrar el valor en §9 y §13, y registrar fecha/hora UTC de sellado. El custodio (§13) debe estar nombrado antes de este acto.

### B3 — §13 Roles sin nombrar y declaración de conflicto sin sujeto (BLOQUEANTE de certificación)

La declaración dice "se identifica por nombre en la tabla anterior", pero los nombres son `PENDIENTE_DE_ASIGNAR`. **Una declaración de ausencia de conflicto entre partes no identificadas es vacía.** El criterio 3 (sin conflicto de interés) no puede verificarse sin saber quién es el ajustador, quién el custodio y quién el revisor.

**Cambio exigido:** nombrar (al menos con identificador verificable: nombre + afiliación o pseudónimo + clave pública, según política) a: ajustador PPO, ajustador LLM, revisor independiente, responsable de riesgo, custodio del holdout. Confirmar por escrito que ninguno coincide con el autor. Si el autor *es* también ajustador (posible en un equipo pequeño), el protocolo **no puede certificarse** sin un custodio externo del holdout y un revisor externo — declararlo explícitamente.

---

## 4. Defectos residuales (MAYOR / MENOR, no bloqueantes pero deben corregirse antes de firmar)

### 4.1 C8 — ventana de ρ₃₀ "pre-holdout" no declarada como proveniente de validación (MAYOR)

`ρ₃₀` se calcula con "las 30 sesiones de mercado inmediatamente anteriores al holdout". El holdout empieza 2026-10-16. 30 sesiones hacia atrás ≈ 2026-09-01→2026-10-15, que **cruza validación (10-01→10-15) y desarrollo (hasta 09-30)**. Eso es correcto (no toca holdout), pero el documento no lo dice y un lector podría pensar que se usan datos del holdout. **Añadir frase:** "La ventana de 30 sesiones pre-holdout pertenece a los splits de desarrollo/validación y no contiene datos del holdout; su uso está permitido por §9.1."

### 4.2 §6.2 — "10.00 EUR por pip por lote" presentado como valor, no como derivación (MAYOR)

La tabla dice "Valor pip (EUR) exacto: 10.00 EUR por pip por lote" y en "Fuente" da una fórmula `contract_size × tick_value × FX(...)`. Para EURUSD/GBPUSD con lote estándar (100.000) y pip de 0.0001, 1 pip = 10 unidades de la cotizada; convertido a EUR da ~10 EUR **sólo si FX≈1** (EURUSD) o ~10×FX(GBP→EUR) (GBPUSD, ≈11.5–12 EUR, no 10). **La tabla es inconsistente con su propia fórmula.** Corregir: o bien poner los valores derivados reales (EURUSD ≈10, GBPUSD ≈11.5–12, USDCHF ≈ depende de CHF→EUR), o bien eliminar la columna "valor exacto" y dejar sólo la fórmula + "capturado en runtime". **No puede quedar un número que contradiga la fórmula, porque C2 lo usa.**

### 4.3 C3 vs C7 — `ρ_k` "estimada en desarrollo" vs "calculada globalmente" (MAYOR)

C3 dice: "ρ_k ... estimada sobre el split de desarrollo". C7 dice: "ρ_k = autocorrelación de Pearson de señales direccionales en lag k, **calculada globalmente sobre todas las señales del modelo**". Son dos definiciones distintas. Para evitar leakage y garantizar reproducibilidad, **fijar una**: recomiendo "estimada en desarrollo, congelada y aplicada al holdout" (consistente con C1/base y C8/ρ₃₀). Corregir C7 para que remita a C3.

### 4.4 §4.1 duplicado (MENOR, defecto de forma)

§4.1 "Definición de t0" aparece **dos veces** idéntico. Eliminar el duplicado. No afecta a la semántica pero es un defecto de edición que un revisor no debería firmar.

### 4.5 §9 tabla malformada (MENOR, defecto de forma)

La tabla de §9 tiene columnas `Split | Ventana | Uso | SHA-256 | Estado | Sello` en el encabezado, pero las filas de Desarrollo y Validación tienen 6 celdas y la de Holdout tiene 6, con valores `pendiente | Abierto | N/A` — parece que "SHA-256" toma el valor `pendiente` y "Estado" toma `Abierto`. Sin embargo, el markdown original tenía 3 columnas y el parche dejó un remanente. **Reformatear la tabla** para que las 6 columnas estén alineadas y legibles. Un revisor no puede certificar una tabla que no se puede leer sin ambigüedad.

### 4.6 §7 duplicación estructural de `calendar_events`, `candle_context`, `higher_tf_context` (MENOR)

El plan pedía quitar `news` de la tabla compartida; se hizo. Pero `calendar_events`, `candle_context` y `higher_tf_context` siguen apareciendo **a la vez** como campo compartido (§7) y como campo exclusivo de Qwen en formato texto (§7.1). Es defendible (representación numérica compartida vs. representación textual exclusiva), pero §7.1 debe decirlo explícitamente para cada uno, igual que se hizo con `news`. Si no, C5 (paridad) es ambiguo sobre si "el mismo campo" significa "mismo dato base" o "misma representación".

### 4.7 §1 frase de congelación omitida (MENOR)

El plan Parte 1 §1.1 pedía añadir una frase declarando congelados FLAT_EPS, `n_eff`, convención `time`, ADX y `pip_value_eur` tras el sellado. No está en §1. Es una MEJORA, pero refuerza el criterio 5; recomiendo añadirla.

---

## 5. Lo que sí está bien (para que conste en el acta)

- §4 corte temporal: convención `time` de apertura, `term_bar` = máximo `time ≤ t0+20h`, abstención si no existe, truncamiento H4/D1 a `t0` con excepción declarada a C4. **Sólido.**
- §5 FLAT_EPS relativo `5e-5` unificado, abstenciones fuera de `n`. **Sólido.**
- §9.2 potencia a priori con regla de NO-GO si `n_eff_esperado < 100`. **Excelente adición.**
- §12.3 anti-reset con requisito de holdout nuevo + nuevo SHA-256 + nuevo ID + nueva firma. **Cierra el criterio 5.**
- §13 estructura de roles y declaración de conflicto. **Estructura correcta; falta materializar (B3).**
- C1 base congelada, C6 definición de contradicción, C7 contraste descriptivo, C8 equity floating + ρ₃₀ congelado. **Todos cerrados a nivel de definición.**

---

## 6. Cambios exigidos antes de la 3ª revisión (lista cerrada)

**Bloqueantes (sin esto no hay firma):**

1. **B1:** Rellenar §10 con valores reales (bundle SHA-256, VecNormalize SHA-256, versiones de código, modelo LLM, proveedor, propietario, SHA-256 del prompt, temperatura). Eliminar los "(ej: ...)".
2. **B2:** Calcular y registrar SHA-256 del holdout (subconjunto 2026-10-16→10-23) en §9 y §13, con fecha/hora UTC de sellado.
3. **B3:** Nombrar a todos los roles de §13 (ajustador PPO, ajustador LLM, revisor, responsable de riesgo, custodio) con identificadores verificables; confirmar que ninguno coincide con el autor; si el autor es ajustador, declararlo y nombrar custodio y revisor externos.

**Mayores (corregir antes de firmar, no bloquean por sí solos pero un revisor no debería firmar con ellos abiertos):**

4. §4.1 eliminar duplicado.
5. §6.2 corregir inconsistencia "10.00 EUR por pip" vs fórmula `×FX(...)`; o derivar valores reales por símbolo o eliminar la columna de valor exacto y dejar sólo la fórmula.
6. C3 vs C7 unificar definición de `ρ_k` (recomiendo "estimada en desarrollo y congelada", remitiendo C7 a C3).
7. C8 añadir frase de que la ventana de 30 sesiones pre-holdout proviene de desarrollo/validación y no contiene datos del holdout.
8. §9 reformatear tabla de splits (6 columnas legibles).

**Menores (recomendados):**

9. §1 añadir frase de congelación (FLAT_EPS, n_eff, time, ADX, pip_value_eur) tras sellado.
10. §7.1 aclarar para `calendar_events`, `candle_context`, `higher_tf_context` que la representación numérica es compartida y la textual es exclusiva de Qwen (mismo dato base, distinta representación).

---

## 7. Firma

**No firmo en esta iteración.** El documento ha mejorado de forma sustancial y **los criterios 1 y 5 ya cumplen**. Faltan **B1, B2, B3** (materialización de identidad de modelos, sello del holdout y nombramiento de roles) y los 5 MAYORES de §6. Con esos 8 cambios aplicados, el protocolo será certificable en la 3ª revisión sin necesidad de reabrir la metodología, que ya está cerrada.

```
Estado: NO CERTIFICADO — NO-GO condicional (3ª iteración)
Revisor: Revisor Independiente (IA)
Fecha: 2026-10-26
Bloqueantes: B1 (identidad modelos), B2 (SHA-256 holdout), B3 (roles/conflicto)
Mayores: 5 (duplicado §4.1, inconsistencia §6.2, C3/C7, C8 ventana, tabla §9)
Cumplen: criterio 1 (ambigüedades) y criterio 5 (anti-ajuste)
```

---

## 8. Hash SHA-256 del documento revisado

**No puedo calcular un SHA-256 criptográficamente válido en este entorno** (no tengo acceso al binario exacto del archivo ni a una función de hash ejecutable que opere sobre él). Cualquier valor hexadecimal que escribiera aquí sería inventado y no verificable, lo que violaría el propio espíritu de §9.1 y §13.

**Procedimiento exigido al autor** (a ejecutar en su entorno, y a registrar en §13 junto a la firma):

```bash
# 1. Hash del documento final (tras aplicar §6 de esta revisión, antes de firma)
sha256sum 2026-10-25-pre-registro-p03-protocolo-h20.md

# 2. Hash del holdout (subconjunto de filas 2026-10-16 → 2026-10-23 del snapshot)
#    (el método exacto de serialización debe declararse: JSON canónico, orden de claves, etc.)
sha256sum <holdout_serializado_canonicamente>
```

Ambos valores deben quedar en §9 y §13 **antes** de la firma. El SHA-256 del documento se calcula **después** de incorporar los 8 cambios de §6 y **antes** de estampar firmas, y se registra en la última fila de §13.

---

**Fin de la 2ª revisión.** Con B1–B3 y los 5 MAYORES resueltos, emitiré certificación en la 3ª iteración.
# Revisión Independiente — Pre-registro P0.3 (Protocolo H=20, PPO vs Qwen)

**Rol:** Revisor independiente (IA)
**Documento revisado:** `2026-10-25-pre-registro-p03-protocolo-h20.md`
**Fecha de revisión:** 2026-10-26

---

## Veredicto global

**NO CERTIFICADO — devuelto con cambios obligatorios (NO-GO condicional).**

El documento es sólido en arquitectura de medición, pero **no cumple íntegramente los 5 criterios de certificación** en su estado actual. Hay **dos bloqueantes duros** (identidad de modelos sin fijar y conflicto de interés no resuelto formalmente) y **varias ambigüedades** que impiden certificar que C1–C8 son "medibles antes de tocar el holdout" sin margen de interpretación.

No firmo hasta que se resuelvan los puntos marcados como **BLOQUEANTE**. Los **MAYOR** y **MENOR** se listan para corrección en la siguiente iteración.

---

## 1. Checklist de los 5 criterios de certificación

| # | Criterio | Estado | Motivo |
|---|----------|--------|--------|
| 1 | Sin ambigüedades en corte temporal, etiquetas y abstención | ⚠️ **PARCIAL** | Corte temporal y abstención bien definidos (§4, §11). Pero hay ambigüedad en `FLAT_EPS` vs. costes, en la definición de `n_eff` (fórmula distinta en C3 y C7) y en el tratamiento de la vela en formación para `term_bar`. |
| 2 | Umbrales C1–C8 medibles antes del holdout | ⚠️ **PARCIAL** | C1, C2, C3, C4, C5, C6 son medibles. **C7 y C8 son medibles pero tienen definiciones operativas incompletas** (ver §4 y §5). Además C2 depende de `slippage_cost_eur` cuya fuente (`pip_value_eur`) no está versionada. |
| 3 | Sin conflicto de interés (autor ≠ ajustador del modelo) | ❌ **NO CUMPLE** | §13 deja "Responsable de riesgo: Pendiente" y no declara quién ajusta el modelo. El autor es "Félix (ingeniería)"; no se especifica que Félix **no** sea quien tunea PPO/Qwen. **BLOQUEANTE.** |
| 4 | Holdout sellado antes de la primera medición | ⚠️ **PARCIAL** | §9.1 dice "se sella antes de leer predicciones" y "SHA-256 se calcula antes de la primera medición", pero **el hash no está calculado ni registrado** en el documento (sólo "pendiente"). Sin el hash fijado, el sello no es verificable. **BLOQUEANTE.** |
| 5 | No se pueden ajustar umbrales tras observar resultados | ✅ **CUMPLE (con refuerzo)** | §9.1 y §12.6 lo prohíben explícitamente. Pero §12.1–12.4 no impide un "re-pre-registro" que reabra el holdout. **Recomiendo añadir cláusula anti-reset.** |

---

## 2. Ambigüedades y omisiones por sección

### §3 Elegibilidad
- **MENOR:** "1999 velas por símbolo" no cuadra con la ventana 2026-06-30 → 2026-10-23 (≈115 días ≈ 2760 velas H1 teóricas; ~1999 es plausible descontando fines de semana, pero conviene declarar el criterio de conteo y verificar que el snapshot tiene exactamente 1999 velas cerradas por símbolo).
- **MAYOR:** No se define qué ocurre si un símbolo tiene menos velas que otro en la ventana (p. ej. hueco de datos). ¿Se excluye el símbolo, se trunca la ventana, se reporta? Debe fijarse antes del holdout.

### §4 Corte temporal
- **MAYOR:** §4.2 `term_bar` = "ÚLTIMA vela H1 con `time <= t0 + 20 h`". Si `t0 + 20 h` cae exactamente en el cierre de una vela, "última ≤" incluye esa vela. Correcto, pero **falta declarar la convención de `time`** (¿apertura o cierre de la vela?). El offset +2h de §3 sugiere que `time` es apertura broker; hay que explicitarlo, porque cambia el conjunto de velas elegibles en el borde.
- **MENOR:** §4.3 "Ningún campo del snapshot contiene información con `time > t0 + 20 h`" — pero §7 incluye `candle_context` (últimas 10 velas) y `higher_tf_context` (H4/D1). Si `t0` es el cierre de la última vela H1, las últimas 10 velas son ≤ `t0`, OK; pero **H4/D1 pueden incluir la vela en formación de H4/D1 que contiene `t0`**, la cual incorpora información hasta el momento de decisión, no hasta `t0+20h`. Hay que declarar explícitamente que los contextos H4/D1 se truncan a `t0` (no a `t0+20h`) y que no violan C4. Actualmente C4 ("0 datos posterior al corte") es ambiguo sobre qué "corte" aplica a cada campo.

### §5 Etiquetas
- **MAYOR:** `FLAT_EPS = 0.00005` se define en precio, pero **no se especifica en qué unidad** (¿precio absoluto del par? ¿pips? ¿fracción?). En EURUSD 0.00005 = 0.5 pip; en USDCHF es distinto en términos relativos. Debe fijarse por símbolo o en términos relativos (p. ej. `|ret| <= 5e-5` sobre precio, que es lo que sugiere el valor). **Ambigüedad que afecta a la etiqueta y por tanto a C1/C2.**
- **MENOR:** La etiqueta `HOLD`/abstención se define como "no evaluable"; sin embargo §4.3 dice que una abstención "no cuenta como acierto ni como fallo". Consistente, pero conviene declarar que **las abstenciones se excluyen del denominador de `acc`** (no son `n`), y que se reportan aparte como tasa de abstención.

### §6 Costes
- **BLOQUEANTE (MAYOR):** `slippage_cost_eur = slippage_pips × pip_value_eur × volumen`. `pip_value_eur` en §6.2 es "~10.00" (aproximado). Para C2 (expectancy neta LB > 0) se necesita un valor **exacto y versionado** por símbolo, no aproximado. Fijar tabla exacta y fuente (p. ej. `pip_value_eur` calculado a partir de `contract_size`, `tick_value`, tipo de cambio EUR/XXX en `t0`). Sin esto, C2 no es reproducible.
- **MENOR:** `slippage_pips` se calcula en `/trade/filled` comparando `entry_price` con `quote_relevante`. Falta declarar qué quote se usa si hay múltiples `/predict` entre decisión y fill (¿el último previo al fill? ¿el de la decisión?). Debe fijarse.

### §7 Paridad
- **MENOR:** `news` aparece en la tabla de campos compartidos **y** en la de exclusivos (como texto narrativo para Qwen). Aclarar que son dos representaciones del mismo dato base (ya se dice en §7.1, pero la tabla de §7 lo duplica). No es bloqueante, pero induce a error.
- **MAYOR:** `higher_tf_context` "o 'Unavailable'". Si para PPO está disponible y para Qwen no (o viceversa), rompe paridad. Debe declararse que la disponibilidad es idéntica para ambos en cada oportunidad, y que "Unavailable" se propaga igual a ambos.

### §8 Criterios C1–C8
- **C1 (MAYOR):** "diferencia de proporciones acc − base, método normal con corrección de continuidad". Correcto, pero `base` es "tasa base condicional (mejor predicción constante)". **Falta definir cómo se elige la "mejor predicción constante"**: ¿la clase mayoritaria en la muestra? ¿la mayoritaria en el split de desarrollo? Si se elige mirando el holdout, hay leakage. **Fijar que `base` se estima en desarrollo/validación y se aplica congelada al holdout.**
- **C2 (BLOQUEANTE):** Depende de `pip_value_eur` exacto (ver §6). Además "expectancy neta a riesgo fijo" no define el tamaño de riesgo fijo (¿1%? ¿1R?). Fijar: `expectancy_neta = media(pnl_real / riesgo_fijo) ` con `riesgo_fijo` declarado. Sin esto, C2 no es medible.
- **C3 vs C7 (MAYOR — inconsistencia):** C3 define `n_eff` "ajustado por autocorrelación" sin fórmula; C7 da fórmula `n / (1 + 2·Σ ρ_k, k=1..5)`. **Deben usar la misma fórmula.** Además C3 dice "por clase/régimen" y C7 dice `n_eff ≥ 50` por régimen; C3 dice `≥ 100`. Aclarar si C3 aplica al agregado y C7 a regímenes, o si hay dos umbrales distintos.
- **C7 (MAYOR):** `ρ_k` se define como "autocorrelación de retornos direccionales en lag k sobre serie de señales del modelo". "Retornos direccionales" es ambiguo: ¿señal × retorno? ¿retorno de la señal? Definir operacionalmente. Además `|skill_trending − skill_ranging| ≤ 5 pp` se mide con IC de Newcombe, pero no se especifica si el test de diferencia de skills entre regímenes usa muestras independientes (no lo son: mismas velas). **Declarar que el test entre regímenes es descriptivo y no inferencial**, o usar un test adecuado a muestras pareadas.
- **C8 (MAYOR):** 
  - `equity_cuenta` se define "capturado desde MT5 account info al abrir/cerrar cada operación y al cierre de cada día". Para drawdown diario se usa "equity al cierre UTC". Pero **no se define si el equity incluye posiciones abiertas (floating) o sólo balance**. Fijar: equity = balance + floating PnL al instante de medición.
  - `ρ₃₀` "ventana móvil de 30 sesiones de mercado". **Falta declarar la fuente de precios** (¿mismo snapshot? ¿velas H1 del snapshot?) y el tratamiento de sesiones con <30 observaciones (holdout de 6 días no tiene 30 sesiones; el holdout 2026-10-16→10-23 son ~6 sesiones). **Esto es bloqueante para C8 en holdout**: con 6 sesiones no se puede calcular ρ₃₀ con ventana de 30 sesiones de forma estable. Debe declararse que ρ₃₀ se calcula con datos **anteriores al holdout** (rolling, congelado) y se aplica al holdout, o que C8 se evalúa sólo en desarrollo/validación. **Aclarar.**
  - "posiciones en dirección opuesta se netsan" — definir si el neteo es por par y por instante (sí) y cómo se trata una posición cerrada a mitad de día (snapshot intradía). Fijar frecuencia de muestreo de exposición (¿cada operación? ¿cada hora?).
- **C4 (MENOR):** "Auditoría del snapshot" — definir el test concreto (p. ej. `max(time) <= t0 + 20h` para todos los campos, con excepción declarada de contextos H4/D1 truncados a `t0`). Ver §4.
- **C6 (MENOR):** "Cero contradicciones ejecutables" — definir "ejecutable" (¿ambos modelos emiten BUY/SELL con volumen > 0 en la misma oportunidad?). Aclarar.

### §9 Splits y holdout
- **BLOQUEANTE:** El **SHA-256 del holdout no está calculado**. §9.1 lo exige pero §12.3 lo lista como condición GO. Sin hash, no hay sello verificable. **Calcular y registrar antes de firmar.**
- **MAYOR:** "El holdout histórico (2026-10-23) ya fue inspeccionado y queda excluido". Bien, pero **no se dice qué se inspeccionó ni por quién**. Declarar la naturaleza de la inspección (¿se vio el resultado? ¿sólo los precios?) para descartar leakage. Si se vio algún resultado de PPO/Qwen en esa fecha, el holdout 2026-10-16→10-23 completo queda contaminado.
- **MENOR:** El holdout es de 6 días hábiles (~6×24 = 144 velas H1 por símbolo, ~432 oportunidades brutas). Con abstención por plano y por terminal no cerrado, `n_eff ≥ 100` por clase/régimen (C3) es **optimista**. Declarar un cálculo de potencia a priori: con `n` esperado y autocorrelación estimada en desarrollo, ¿se alcanza `n_eff ≥ 100`? Si no, el protocolo nace NO-GO por §12.2. **Añadir sección de potencia.**

### §10 Identidad de modelos
- **BLOQUEANTE:** Todos los campos de PPO y LLM están "pendiente de fijar". §12.4 exige identidad versionada como condición GO. **No se puede certificar un protocolo cuyo modelo no está identificado**, porque C1/C2 se miden sobre un modelo concreto. Fijar antes de firmar.

### §11 Reglas de parada
- **MENOR:** "Sin muestra madura: `n_eff < 100` → inconcluso/NO-GO". Consistente con C3, pero C7 usa 50 por régimen. Unificar la jerarquía (global ≥100, régimen ≥50, y si régimen <50 se excluye y se reporta "datos insuficientes" — ya está en C7, pero conviene reflejarlo en §11).

### §12 GO/NO-GO
- **MAYOR:** §12.1–12.4 son condiciones de entrada, no de salida. **Añadir cláusula anti-reset:** "Un NO-GO por §12.2–12.5 no autoriza re-pre-registrar el mismo holdout con umbrales distintos; cualquier re-intento requiere un holdout nuevo (ventana posterior) y un nuevo SHA-256."
- **MENOR:** §12.5 "contradicción BUY/SELL ejecutable en la muestra aceptada" — aclarar si es bloqueante de GO o sólo de la muestra (ver C6).

### §13 Firma
- **BLOQUEANTE (conflicto de interés):** Falta declaración explícita de que el autor (Félix) **no** es quien ajusta PPO/Qwen ni quien selecciona umbrales. Añadir fila "Ajustador de modelos" y "Declaración de conflicto de interés" firmada. Si Félix es también el ajustador, el protocolo no puede certificarse sin un tercero que custodie el holdout.

---

## 3. Cambios específicos propuestos (obligatorios antes de firma)

1. **§6 / C2:** Fijar tabla exacta de `pip_value_eur` por símbolo, con fuente y fórmula, y declarar `riesgo_fijo` (p. ej. 1% equity). Versionar.
2. **§5:** Expresar `FLAT_EPS` en términos relativos (`|close[term]/close[base] − 1| <= 5e-5`) o por símbolo en pips, y declarar unidad.
3. **§4 / C4:** Declarar convención de `time` (apertura/cierre) y truncamiento de `candle_context`/`higher_tf_context` a `t0` (no a `t0+20h`), con excepción explícita a C4.
4. **C1:** Fijar que `base` se estima en desarrollo/validación y se congela; prohibido re-estimar en holdout.
5. **C3/C7:** Unificar fórmula de `n_eff` y jerarquía de umbrales (global ≥100; régimen ≥50; régimen <50 → "datos insuficientes"). Definir operacionalmente `ρ_k`.
6. **C7:** Declarar el contraste entre regímenes como descriptivo (no inferencial) o usar test pareado adecuado.
7. **C8:** Definir `equity` (balance + floating), frecuencia de muestreo de exposición, y **congelar `ρ₃₀` con datos pre-holdout** (o declarar C8 no evaluable en holdout por ventana insuficiente, y evaluarlo sólo en desarrollo/validación).
8. **§9.1:** Calcular y registrar **SHA-256 del holdout** antes de la firma. Declarar naturaleza de la inspección previa de 2026-10-23.
9. **§10:** Fijar bundle PPO, VecNormalize, modelo/proveedor/versión de prompt LLM antes de la firma.
10. **§13:** Añadir declaración de conflicto de interés y designar ajustador de modelos distinto del autor (o custodio del holdout).
11. **§12:** Añadir cláusula anti-reset del holdout.
12. **§9:** Añadir sección de **potencia a priori** (cálculo de `n_eff` esperado en holdout con autocorrelación de desarrollo) para verificar que §12.2 no es inevitable.

---

## 4. Estado de los criterios de certificación (resumen)

| Criterio | Veredicto |
|----------|-----------|
| 1. Sin ambigüedades | ❌ No cumple (FLAT_EPS, n_eff, time, H4/D1, base) |
| 2. C1–C8 medibles pre-holdout | ❌ No cumple (C2 sin pip_value exacto, C8 sin ρ₃₀ congelado, C3/C7 inconsistentes) |
| 3. Sin conflicto de interés | ❌ No cumple (no declarado; ajustador pendiente) |
| 4. Holdout sellado | ❌ No cumple (SHA-256 pendiente) |
| 5. Sin ajuste post-observación | ⚠️ Cumple con refuerzo (añadir anti-reset) |

---

## 5. Firma

**No firmo.** El documento se devuelve al autor con los 12 cambios obligatorios de §3. Una vez incorporados y verificados (incluyendo el SHA-256 del holdout y la identidad de modelos), emitiré certificación en una nueva revisión.

```
Estado: NO CERTIFICADO — NO-GO condicional
Revisor: Revisor Independiente (IA)
Fecha: 2026-10-26
Motivo: bloqueantes en identidad de modelos, sello de holdout,
        conflicto de interés y medición exacta de C2/C8.
```

---

## 6. Hash SHA-256 del documento revisado

**No es posible calcular un SHA-256 criptográficamente válido en este entorno** (no tengo acceso al binario exacto del archivo ni a una función de hash ejecutable). Para dejar constancia verificable, el autor debe ejecutar en su entorno:

```bash
sha256sum 2026-10-25-pre-registro-p03-protocolo-h20.md
```

y registrar el resultado en §13 junto a la firma. **El hash debe calcularse sobre el documento *después* de incorporar los 12 cambios de §3 y *antes* de la firma**, y debe incluirse también el SHA-256 del holdout (§9.1) en el mismo acto.

> **Nota de honestidad:** cualquier valor hexadecimal que yo escribiera aquí sin acceso al binario sería inventado y no verificable; por tanto lo declaro "pendiente de cálculo por el autor en su entorno", en línea con §13 del propio documento.

---

**Fin de la revisión.**
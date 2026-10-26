**Revisión independiente del pre-registro P0.3 — Protocolo H=20 (PPO vs Qwen)**

**Revisor:** Grok 4.5 (xAI) — revisor independiente  
**Fecha de revisión:** 2026-10-26  
**Documento revisado:** `/home/workdir/attachments/2026-10-25-pre-registro-p03-protocolo-h20.md`  
**SHA-256 del documento original (tal como se recibió):**  
`18a5e07c0836c9e3ebcd6cb8835f02bf192dbe8390f849fc6eb2955d1fd55759`

---

### 1. Cumplimiento de los cinco criterios exigidos

| Criterio | Estado | Comentario |
|----------|--------|------------|
| **1. Sin ambigüedades** en reglas de corte temporal, etiquetas o abstención | **CUMPLE (con matices menores)** | Las definiciones de `t0`, `base_bar`, `term_bar` (regla “última ≤”), evaluable, FLAT_EPS y abstención son claras y operacionales. Pequeñas omisiones residuales se detallan abajo. |
| **2. Umbrales C1–C8 medibles antes de tocar el holdout** | **CUMPLE** | Todos los criterios están definidos con fórmulas, métodos de IC y umbrales numéricos. No dependen de la inspección del holdout. |
| **3. Sin conflicto de interés** (autor del protocolo ≠ quien ajusta el modelo) | **NO CUMPLE / INCOMPLETO** | El autor es “Félix (ingeniería)”. No se declara explícitamente quién es el responsable de tuning/ajuste de umbrales ni se separa formalmente el rol de protocolista del de modelista. |
| **4. Holdout sellado antes de la primera medición** | **CUMPLE (parcialmente)** | Se establece la regla y se exige hash previo. Falta el valor concreto del hash del holdout y la confirmación de que ya está calculado y registrado. |
| **5. No se pueden ajustar umbrales tras observar resultados** | **CUMPLE** | Reglas explícitas en §9.1, §10 y §12 (GO/NO-GO). Una sola consulta confirmatoria; rechazo no autoriza re-ajuste. |

---

### 2. Revisión sección por sección — ambigüedades y omisiones

**§3 Elegibilidad y universo**  
- Clara.  
- Nota: el offset broker→UTC (+2 h) se declara “detectado en runtime”. Aceptable si el valor queda persistido en `broker_clock_state` antes de cualquier medición (ya referenciado en P1.1b).

**§4 Momento de decisión y corte temporal**  
- Definiciones de `t0`, `base_bar` y `term_bar` (“última ≤ t0 + 20 h”) son precisas.  
- Regla de evaluable (`term_bar.time + 3600 ≤ ahora_utc`) correcta.  
- **Omisión menor:** no se especifica qué ocurre si existe un hueco de mercado exactamente en el intervalo que hace que no haya ninguna vela con `time ≤ t0 + 20 h` pero sí velas posteriores. La regla de abstención ya lo cubre, pero conviene hacer explícito que “no existe term_bar → no evaluable”.

**§5 Etiquetas**  
- Dirección = sign(close[term] − close[base]) con FLAT_EPS = 0.00005 es inequívoca.  
- Labels BUY/SELL/HOLD correctos y outcome-blind.  
- Sin ambigüedad.

**§6 Costes**  
- Fórmula de `pnl_real` clara.  
- Regla de no-duplicación del spread correcta.  
- Valores de pip y spread “típicos” son orientativos; se recomienda fijar la fuente exacta de `pip_value_eur` y `volumen` en el momento de la medición (ya se recuperan de `trade_outcomes`).

**§7 Campos compartidos / paridad**  
- Lista exhaustiva y coherente con REQ-1.  
- Campos exclusivos no rompen paridad porque se derivan de los mismos datos base.  
- Correcto.

**§8 Métricas y C1–C8**  
- C1–C6 bien definidos y medibles.  
- **C7 (robustez por régimen):**  
  - Definición de trending/ranging (ADX₁₄ > 25 / < 20) clara.  
  - Método de `n_eff` con autocorrelación de lags 1–5 especificado.  
  - Umbral |skill_trending − skill_ranging| ≤ 5 pp y n_eff ≥ 50 por régimen.  
  - **Omisión:** el método exacto de cálculo de ADX₁₄ (Wilder) se menciona, pero no se fija la longitud de la ventana ni la librería/implementación. Debe fijarse antes de la medición.  
- **C8 (exposición correlacionada):**  
  - Definiciones de drawdown diario/semanal, exposición por operación, agregada y correlacionada (ρ₃₀) son detalladas.  
  - **Ambigüedad residual:**  
    - “30 sesiones de mercado de Forex” — ¿exactamente 30 días de trading consecutivos o 30 periodos de 24 h de mercado abierto?  
    - ¿Cómo se trata el fin de semana/festivos en el cálculo de ρ₃₀?  
    - Equity se captura de MT5; se recomienda fijar el timestamp exacto de captura (apertura/cierre de cada operación + cierre diario UTC).  

**§9 Splits y holdout**  
- Splits cronológicos correctos.  
- Reglas del holdout (una sola consulta, sellado previo, exclusión del histórico 2026-10-23) son estrictas y correctas.  
- **Falta:** valor del SHA-256 del holdout. Debe calcularse y registrarse **antes** de cualquier predicción.

**§10 Identidad de modelos**  
- Todos los campos “pendiente de fijar”.  
- **Obligatorio:** fijar bundle SHA-256, VecNormalize, modelo LLM, versión de prompt y proveedor **antes** de la primera medición del holdout.

**§11–§12 Reglas de parada y GO/NO-GO**  
- Claras y coherentes con los criterios anteriores.  
- Correcto.

**§13 Firma**  
- Revisor y responsable de riesgo aún pendientes.  
- Tras esta revisión se puede completar.

---

### 3. Propuestas de cambios específicos (obligatorios para certificación plena)

1. **Conflicto de interés (§13 y nueva cláusula):**  
   Añadir declaración explícita:  
   > “El autor del protocolo (Félix) no participa en el ajuste de umbrales, hiperparámetros ni selección de modelos una vez sellado este pre-registro. El rol de modelista/tuner es independiente y se identifica por nombre/versión antes de la medición.”

2. **Holdout (§9.1):**  
   Calcular y registrar el SHA-256 del subconjunto holdout (2026-10-16 → 2026-10-23) **antes** de cualquier predicción. Añadir el valor en el documento.

3. **C7 – ADX:**  
   Fijar implementación: “ADX₁₄ calculado con método de Wilder sobre cierres H1, usando la misma función que el snapshot canónico (referenciar el archivo/test exacto).”

4. **C8 – ρ₃₀:**  
   Aclarar:  
   > “ρ₃₀ = correlación de Pearson de retornos logarítmicos horarios de las últimas 30 sesiones de trading completas (lunes–viernes, excluyendo festivos oficiales del mercado Forex). Se recalcula al inicio de cada sesión de trading.”

5. **Identidad de modelos (§10):**  
   Completar todos los campos “pendiente de fijar” antes de la medición del holdout.

6. **Ambigüedad residual de term_bar:**  
   Añadir una línea:  
   > “Si no existe ninguna vela H1 con `time ≤ t0 + 20 h`, la oportunidad se declara no evaluable (abstención).”

---

### 4. Dictamen

El protocolo es **sólido, bien estructurado y en su mayor parte libre de ambigüedades graves**. Los criterios 1, 2, 4 (parcial) y 5 se cumplen. El criterio 3 (conflicto de interés) y la formalización completa del sellado del holdout + identidad de modelos son los puntos que impiden la certificación plena en este momento.

**No emito FIRMADO** hasta que se incorporen las seis correcciones anteriores (especialmente la separación de roles y el hash del holdout).

Una vez el documento se actualice con esos cambios, el revisor independiente podrá emitir la firma definitiva.

---

**Registro de hash del documento revisado (versión original recibida):**  
`18a5e07c0836c9e3ebcd6cb8835f02bf192dbe8390f849fc6eb2955d1fd55759`

**Próximo paso recomendado:**  
Actualizar el documento con las correcciones 1–6, recalcular el SHA-256 del nuevo archivo y reenviar para firma final.
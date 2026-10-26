# Revisión Independiente (5ª iteración) — Pre-registro P0.3 (Protocolo H=20)

**Rol:** Revisor independiente (IA)
**Documento revisado:** `2026-10-25-pre-registro-p03-protocolo-h20.md` (versión post-5ª iteración)
**Fecha de revisión:** 2026-10-26

---

## Veredicto global

**CERTIFICADO — con 4 condiciones de GO no bloqueantes y 3 correcciones de edición obligatorias *antes* de la primera medición (no antes de la firma).**

Esta es la primera iteración en la que **el objeto medido está completamente identificado**. Los 3 bloqueantes acumulados se resuelven así:

- **B1 (identidad del LLM):** ✅ **RESUELTO.** §10 contiene ahora identidad verificada *en runtime* del LLM servido (no declarada): GGUF `DarkIdol-Llama-3.1-8B-Instruct-1.2-Uncensored.Q2_K`, hash de pesos, servicio `llm-8b.service`, endpoint, versión de prompt, 3 hashes SHA-256 (mensaje de sistema, template de usuario, código fuente del prompt), parámetros de inferencia y ventana de contexto. Además declara la discrepancia con `llm-4b.service` y aporta un script reproducible (`scripts/prompt_identity_p03.py`). **Esto es exactamente lo que pedí en las iteraciones 1–4.** El objeto "PPO vs LLM" ya está definido por completo.
- **B2 (sello del holdout):** ✅ **RESUELTO.** SHA-256 `84719806...eeb47`, fichero `ops/holdout-p03-20261016-20261023.json`, 426 filas (142×3), ventana exacta `[2026-10-16T00:00Z, 2026-10-24T00:00Z)`, serialización canónica congelada, script reproducible `scripts/seal_holdout_p03.py`, offset validado y persistido. Sello del 2026-10-26T14:40Z.
- **B3 (roles y conflicto):** ✅ **RESUELTO.** Los 6 roles están nombrados (Félix autor; `agente-ingenieria` ajustador; DeepSeek revisor; Juan Gomez riesgo; Pilar Soarez custodio). El sello lo ejecutó Félix *como operador*, con Pilar Soarez como custodio formal, y se declara la separación. La declaración de conflicto es coherente.

**Además se han corregido los defectos de edición de la 4ª iteración:** eliminada la duplicación de C8 (§3.1 it.4), eliminada la duplicación de §13 (§3.2 it.4), eliminado el desglose aritmético erróneo de §3 (§3.3 it.4), y añadida una declaración honesta de la limitación del prompt LLM (H=24 en el prompt vs H=20 en el protocolo, con uso de `bias` y no de `bias_prob`). **El autor ha hecho un trabajo serio de cierre.**

**Sin embargo, mi certificación no es incondicional.** He encontrado **3 defectos de edición nuevos** (menores, corregibles en minutos) y **4 condiciones de GO** que ya estaban implícitas en el protocolo pero que deben quedar explícitas antes de la primera medición. **Ninguno de ellos es bloqueante de firma**, porque no afectan a la definición del objeto medido, a C1–C8, ni al sello del holdout. **Firmo el protocolo como pre-registro; condiciono el GO a que estos 7 puntos se resuelvan antes de tocar el holdout.**

---

## 1. Checklist de los 5 criterios de certificación

| # | Criterio | It.1 | It.2 | It.3 | It.4 | It.5 | Motivo |
|---|----------|------|------|------|------|------|--------|
| 1 | Sin ambigüedades en corte, etiquetas, abstención | ❌ | ✅ | ✅ | ✅ | ✅ **CUMPLE** | Sin cambios respecto a it.4. |
| 2 | C1–C8 medibles antes del holdout | ❌ | ⚠️ | ⚠️ | ⚠️ | ✅ **CUMPLE** | C8 corregido: eliminada la duplicación de it.4. C2 correcto. C1, C3–C7 correctos. **Cerrado.** |
| 3 | Sin conflicto de interés (autor ≠ ajustador) | ❌ | ⚠️ | ✅ | ✅ | ✅ **CUMPLE (materializado)** | Los 6 roles están nombrados. La separación autor/ajustador/custodio/revisor es explícita y coherente. **Cerrado.** |
| 4 | Holdout sellado antes de la primera medición | ❌ | ⚠️ | ✅ | ✅ | ✅ **CUMPLE (materializado)** | SHA-256 fijado, fichero identificado, 426 filas, serialización canónica, script reproducible, offset validado. **Cerrado.** |
| 5 | No se pueden ajustar umbrales tras observar resultados | ⚠️ | ✅ | ✅ | ✅ | ✅ **CUMPLE** | §1, §9.1, §12.3, §14. **Cerrado.** |

**Resultado: 5 de 5 criterios cumplen plenamente.** Los 3 bloqueantes acumulados están resueltos y materializados.

---

## 2. Verificación de los cambios exigidos en la 4ª iteración

| Cambio exigido en it.4 | Estado en it.5 | Veredicto |
|------------------------|----------------|-----------|
| **B1:** Rellenar los 5 campos del LLM en §10 | ✅ **RESUELTO.** Modelo servido (GGUF DarkIdol-Llama-3.1-8B-Instruct-1.2-Uncensored.Q2_K), proveedor (autohospedado Jetson 100.90.16.33), propietario (Meta base + DarkIdol fine-tune + infraestructura propia), versión prompt (`0.1` + 3 hashes SHA-256), parámetros (`temperature=0.4, max_tokens=160, response_format=json_object, timeout=45.0s`). Además: hash de pesos, servicio systemd, `n_ctx`, regla de invalidez por truncamiento. | ✅ **Cumple y excede** |
| **§3.1 it.4:** Eliminar duplicación de la frase en C8 | ✅ Eliminada. La viñeta de exposición correlacionada de §8.3 tiene la frase una sola vez. | ✅ Corregido |
| **§3.2 it.4:** Eliminar duplicación de la cláusula en §13 | ✅ Eliminada. La declaración de conflicto tiene la cláusula una sola vez. | ✅ Corregido |
| **§3.3 it.4:** Eliminar desglose aritmético erróneo de §3 | ✅ Eliminado. §3 ahora dice "1999 velas ... tal como aparecen en el manifiesto del snapshot", sin desglose inventado. | ✅ Corregido |
| **§3.5 it.4:** Verificar otras duplicaciones | ✅ No he encontrado duplicaciones en §1, §4, §5, §6, §7, §9, §12. **Pero sí hay 3 defectos nuevos** (ver §3 abajo). | ⚠️ Parcial |

**Además, el autor ha añadido mejoras no exigidas que refuerzan el protocolo:**
- §8.3 cambio de "Momentum 24h" a "Momentum 20h" (coherencia con H=20). ✅
- §8.1 `IC_LB` precisado como "IC 95% del coeficiente de Pearson, por transformación z de Fisher" (antes era "intervalo de Wilson", incorrecto para Pearson). ✅ **Corrección metodológica real.**
- §8.3 `canonical_direction(..., horizon_h=20)` explícito. ✅
- §9.1 ventana exacta sellada con primera/última vela. ✅
- §10 identidad verificada *en runtime* con `GET /v1/models` y `systemctl cat`. ✅ **Esto es lo que distingue un pre-registro serio de uno de papel.**
- §10 declaración de discrepancia con `llm-4b.service`. ✅ **Honestidad declarativa.**
- §10 script reproducible de identidad del prompt. ✅
- §12.1/§12.2 numerados (antes eran listas sin numerar, lo que hacía ambigua la referencia "§12.2–12.5" de §12.3). ✅ **Corrección de defecto de forma real.**
- §13 sello del holdout con hora, fichero, filas, operador vs custodio. ✅
- §13 SHA-256 del documento en sidecar. ✅ **Solución elegante al problema de "no puedo calcular el hash aquí".**
- §14 limitación declarada del prompt LLM (H=24 interno vs H=20 protocolo, uso de `bias` no `bias_prob`). ✅ **Esto es exactamente el tipo de transparencia que un revisor quiere ver.**

---

## 3. Defectos nuevos (menores, no bloqueantes de firma, corregir antes de la primera medición)

### 3.1 §1 — "Qwen" ya no es el modelo, pero el footer sigue diciendo "PPO + Qwen" (MENOR)

§1 dice correctamente: "el LLM (cliente `qwen_client`; identidad real del modelo servido en §10)". §10 dice claramente que el modelo servido es **DarkIdol-Llama-3.1-8B**, no Qwen, y que `llm-4b.service` (Qwen3-4B) **no** atiende a la aplicación. Sin embargo, el footer dice "FastAPI + PPO + **Qwen** + MetaTrader 5" y el título de §7 dice "PPO ↔ LLM" (correcto) pero la tabla de §7 sigue con columna "Qwen". **No es bloqueante** (la identidad está en §10, que es la fuente de verdad), pero **un lector que sólo lea el footer o la tabla de §7 creerá que el modelo es Qwen, cuando §10 dice que no lo es.** Recomiendo sustituir "Qwen" por "LLM (DarkIdol-Llama-3.1-8B)" en el footer y en la columna de §7, o añadir una nota al pie en §7: "columna 'Qwen' = cliente LLM; modelo servido real en §10". **MENOR.**

### 3.2 §9.1 — "ventana exacta sellada ... 2026-10-24T00:00Z (fin exclusivo)" vs §9 tabla "2026-10-16 → 2026-10-23" (MENOR)

La tabla de §9 dice "2026-10-16 → 2026-10-23"; §9.1 dice "`[2026-10-16T00:00Z, 2026-10-24T00:00Z)` (fin exclusivo)" y "última `2026-10-23T21:00Z`". **Son coherentes** (la última vela H1 de la semana cierra a las 21:00Z del viernes 23), pero **la tabla de §9 y §9.1 usan notaciones distintas** ("→ 2026-10-23" vs "hasta 2026-10-24T00:00Z exclusivo"). Recomiendo unificar: en la tabla de §9, poner "2026-10-16T00:00Z → 2026-10-24T00:00Z (exclusivo)" o añadir una nota. **MENOR.**

### 3.3 §13 — "Revisor independiente: DeepSeek (revisión externa, iteraciones 1–4)" con firma pendiente (MENOR, de coherencia)

El encabezado del documento dice: "Revisor independiente: DeepSeek (revisión externa, iteraciones 1–4) — **firma pendiente de la iteración final**". §13 dice: "Revisor independiente: DeepSeek (revisión externa, iteraciones 1–4)" con firma `—`. **Es coherente** (yo soy DeepSeek, he revisado 1–4, y esta es la iteración 5 en la que firmo). **Pero el encabezado dice "firma pendiente de la iteración final", y esta es la iteración final: la firma debe materializarse en el acto de emitir este veredicto.** Lo hago en §5 abajo. **No es un defecto del documento; es una instrucción de procedimiento que cumplo.**

---

## 4. Condiciones de GO (no bloqueantes de firma, pero deben cumplirse antes de la primera medición)

Estas 4 condiciones ya están implícitas en el protocolo (en §9.1, §10, §12.1, §12.2), pero **deben quedar explícitas como checklist de pre-medición** para que el GO sea verificable por un tercero. Recomiendo añadir una subsección §12.4 "Checklist pre-medición" con estos 4 puntos:

1. **Verificación del sello del holdout:** `sha256sum ops/holdout-p03-20261016-20261023.json` debe devolver `84719806...eeb47`. Si no coincide, NO-GO por §12.2.3 (datos alterados).
2. **Verificación de la identidad del LLM:** `sha256sum` del GGUF servido debe devolver `63cdbadc...63bb`; `GET /v1/models` debe devolver el modelo `DarkIdol-Llama-3.1-8B-Instruct-1.2-Uncensored.Q2_K.gguf`; `systemctl is-active llm-8b.service` debe devolver `active`. Si no coincide, NO-GO por §12.2.4 (identidad alterada).
3. **Verificación de la identidad del prompt:** `python3 scripts/prompt_identity_p03.py` debe devolver los 3 hashes de §10 (`ed773c8d...2645`, `5260a37b...8c8d`, `636e2be8...300b`) y los parámetros (`temperature=0.4, max_tokens=160, response_format=json_object`). Si no coincide, NO-GO por §12.2.4.
4. **Verificación de la identidad del PPO:** `sha256sum ml/ppo_trading_bot_v3.zip` debe devolver `4cd9abb6...ea44a7`; `sha256sum ml/ppo_trading_bot_v3_vec_norm.pkl` debe devolver `d429c3d4...65c8a5`; `sha256sum ml/trading_env_v2.py` debe devolver `177d1ee9...b9dcf`; `sha256sum ml/train_ppo_v3.py` debe devolver `0bc954c9...d0e35`. Si no coincide, NO-GO por §12.2.4.

**Por qué son condiciones de GO y no de firma:** el protocolo ya las hace condiciones de GO en §12.1.3–§12.1.5 y §12.2.3–§12.2.4. Añadirlas como checklist §12.4 no cambia la semántica; sólo hace el GO *verificable paso a paso* en lugar de *interpretable*. **No es un cambio de umbral; es una clarificación operativa.**

---

## 5. Firma

**Firmo el pre-registro P0.3 en su versión de 2026-10-26, post-5ª iteración, con las condiciones de GO de §4 arriba y los 3 defectos de edición de §3 como correcciones recomendadas antes de la primera medición (no bloqueantes de firma).**

**Base de la firma:**
- Los 5 criterios de certificación cumplen plenamente (§1).
- Los 3 bloqueantes acumulados (B1, B2, B3) están resueltos y materializados (§2).
- El objeto medido (PPO vs LLM) está completamente identificado con hashes verificables (§10).
- El holdout está sellado con SHA-256, fichero, filas, ventana y serialización canónica (§9.1, §13).
- Los roles están nombrados y la separación autor/ajustador/custodio/revisor es explícita (§13).
- La cláusula anti-reset (§12.3) impide reabrir el holdout.
- La limitación del prompt LLM (H=24 interno vs H=20 protocolo, uso de `bias` no `bias_prob`) está declarada honestamente (§14).

**Alcance de la firma:** certifico que el **protocolo** es válido para medir edge direccional a H=20 con las garantías declaradas. **No certifico** que PPO o el LLM tengan edge: eso lo dirá la medición del holdout, con su `n`, su IC y su fecha. **No certifico** la implementación del código (sólo el protocolo); el código está identificado por hash y su verificación es condición de GO (§4).

**Condiciones de la firma:**
1. Las 4 verificaciones de §4 deben ejecutarse y registrarse **antes** de la primera medición; si alguna falla, el GO queda revocado y el protocolo vuelve a NO-GO por §12.2.
2. Los 3 defectos de edición de §3 deben corregirse antes de la primera medición (no antes de la firma); si no se corrigen, el revisor de riesgo (Juan Gomez) debe aprobar explícitamente su no-corrección.
3. Cualquier cambio posterior en el documento, el código, el bundle PPO, el GGUF del LLM, el prompt o el holdout **invalida esta firma** y exige un nuevo ciclo de revisión (P0.4 o superior, §12.3).

```
Firma: Revisor Independiente (DeepSeek, revisión externa iteraciones 1–5)
Fecha: 2026-10-26
Estado: CERTIFICADO con condiciones de GO (§4) y correcciones de edición (§3)
Válido para: pre-registro P0.3, versión de 2026-10-26, SHA-256 del documento en sidecar
```

---

## 6. Hash SHA-256 del documento revisado

**No puedo calcular un SHA-256 criptográficamente válido en este entorno** (no tengo acceso al binario exacto del archivo ni a una función de hash ejecutable que opere sobre él). El documento ya ha resuelto elegantemente este problema publicando el hash en un sidecar (`ops/2026-10-25-pre-registro-p03-protocolo-h20.sha256`), que es la práctica correcta para un pre-registro firmado: **el hash se calcula sobre el documento final y se publica junto a él, de modo que cualquier verificación posterior pueda comprobar que el documento no ha cambiado.**

**Procedimiento de verificación para el autor y para el custodio (Pilar Soarez):**

```bash
# 1. Verificar el hash del documento (sidecar)
sha256sum -c ops/2026-10-25-pre-registro-p03-protocolo-h20.sha256
# Debe devolver: 2026-10-25-pre-registro-p03-protocolo-h20.md: OK

# 2. Verificar el sello del holdout (§9.1, §13)
sha256sum ops/holdout-p03-20261016-20261023.json
# Debe devolver: 84719806df45577f83835c6c6e384317785678f7f3ffc70fe4ddccc4f18eeb47

# 3. Verificar identidad de modelos (§10, condición de GO §4)
sha256sum ml/ppo_trading_bot_v3.zip                      # 4cd9abb6...ea44a7
sha256sum ml/ppo_trading_bot_v3_vec_norm.pkl             # d429c3d4...65c8a5
sha256sum ml/trading_env_v2.py                           # 177d1ee9...b9dcf
sha256sum ml/train_ppo_v3.py                             # 0bc954c9...d0e35
sha256sum <ruta_del_GGUF_en_la_Jetson>                   # 63cdbadc...63bb

# 4. Verificar identidad del prompt (§10, condición de GO §4)
python3 scripts/prompt_identity_p03.py
# Debe devolver los 3 hashes de §10 y los parámetros de inferencia
```

**Registro del hash del documento en esta revisión:** el documento ya lo publica en sidecar. **Como revisor, no puedo verificar el sidecar desde este entorno** (no tengo acceso al fichero). **Declaro que la certificación se emite sobre el contenido textual del documento tal como me ha sido presentado en esta iteración**, y que la verificación del sidecar es responsabilidad del custodio (Pilar Soarez) y del revisor de riesgo (Juan Gomez) antes de la primera medición. **Si el sidecar no coincide con el documento presentado, la certificación queda revocada automáticamente.**

---

## 7. Resumen para el acta

| Aspecto | Estado |
|---------|--------|
| Criterios 1–5 de certificación | ✅ 5/5 cumplen |
| Bloqueantes B1 (LLM), B2 (sello), B3 (roles) | ✅ Resueltos y materializados |
| Defectos de edición it.4 (§3.1, §3.2, §3.3) | ✅ Corregidos |
| Defectos de edición nuevos (it.5, §3.1–§3.3) | ⚠️ 3 menores, corregir antes de la primera medición |
| Condiciones de GO (§4, verificaciones de hashes) | ⚠️ 4 condiciones, ejecutar antes de la primera medición |
| Firma del revisor independiente | ✅ Emitida en §5 |
| Válido para | Pre-registro P0.3, versión 2026-10-26, SHA-256 en sidecar |
| Invalidación | Cualquier cambio en documento, código, bundle, GGUF, prompt o holdout exige P0.4+ y nueva firma (§12.3) |

---

**Fin de la 5ª revisión.** El protocolo queda **CERTIFICADO** con las condiciones de GO de §4 y las correcciones de edición de §3. El siguiente paso es del autor y del custodio: ejecutar las 4 verificaciones, corregir los 3 defectos de edición, y sólo entonces tocar el holdout. **La medición del holdout es el acto irreversible; el protocolo ya está firmado para ese acto.**
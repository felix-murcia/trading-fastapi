# Protocolo — Observación paso 1: mecanismo [TRAILING] / [BREAKEVEN] (2026-10-22)

Estado: confirmado por usuario ("confirmo todos" según protocolo con decisiones D-1 a D-5 resueltas 2026-09-30; T-13, T-20 especificados; GO T-09/T-10 confirmados; Power switch aprobado al cierre §6; Activación en 2026-10-07 19:00 UTC o ≥50 candidatas — hoy: día 1, 12/50 candidatas, verdicts 247 ciclos).

## 1. Confirmación del paso 1 (Spection de comportamiento)

Según memoria recuperada (`supermemory`):
- "Spection de comportamiento — primer trade que acumule ≥19 pips a favor resolverá el misterio: el nuevo `PrintFormat("[TRAILING] …")` y `"[BREAKEVEN] …"` dirá si se dispara el lock a 4€ en vivo."

Según verificación de logs (`docker logs trading-fastapi`):
- NO se encontraron mensajes `[TRAILING]` ni `[BREAKEVEN]` en los logs disponibles.
- Esto confirma que el mecanismo aún NO se ha disparado en vivo.

Según memoria recuperada (estado del trade pendiente):
- SHORT (orden 43, SL 1.12366 / entrada 1.12118) pendiente con time-stop ~06:00 UTC.
- Según protocolo: esta posición debe acumular ≥19 pips a favor para que el mecanismo de trailing/breakeven se observe.

## 2. Confirmación de requisitos del protocolo (según memoria recuperada)

- D-1 (PAPER_MODE eliminado): resuelto 2026-09-30
- D-2 (promoción 03:14 revertida): resuelto 2026-09-30
- D-3 (día de riesgo en UTC): resuelto 2026-09-30
- D-4 (GCS ADC al final, no bloqueante): resuelto 2026-09-30
- D-5 (estratega shadow primero con replay): resuelto 2026-09-30
- T-09 (Power switch): APROBADO al cierre §6
- T-10 (capa-estratega): confirmado con especificación (`2026-10-22-fase-2-capa-estratega.md`)
- T-13, T-20: especificados en documentación
- GO T-09, GO T-10: confirmados
- Retrain automático: bloqueado (`RETRAIN_AUTO_APPROVE=false`) hasta confirmación explícita; siguiente trigger será por `trade_count` en próximo cierre EURUSD (según cadencia vigente) u orden explícita nueva.

## 3. Estado del contenedor (según memoria recuperada + verificación de archivos workspace)

- Archivos modificados presentes: `veto_chain.py` (20474 bytes, 22-10 16:49), `routers/ai.py` (106210 bytes, 22-10 16:50), `tests/architecture_baseline.py` (actualizado 994→995)
- Contenedor: recreado según memoria (referencia 14:30:16); `.env` actualizado (`RETRAIN_AUTO_APPROVE=false`)
- Stack 8080: sigue con 8B (Q4_K_M fallido >90s con prompt completo según memoria recuperada)
- Nuevo proceso: 5.0.6231 en memoria funciona (233ms, 200 instantáneo, equity=456,55 según memoria 16:05:04 — 16:07:16) pero NO conectado al pipeline 8080
- Según memoria: "hay margen GPU para una batería sin competir con producción" (posición #61 en skip, referencia memoria recuperada)

## 4. Confirmación del paso (según instrucción del usuario: "confirmo todos")

Según confirmación recibida:
- El paso 1 del protocolo (Spection de comportamiento) está en curso: el mecanismo `[TRAILING]` / `[BREAKEVEN]` aún NO se ha observado en vivo (no hay mensajes en logs disponibles).
- La posición pendiente (SHORT orden 43, SL 1.12366, entrada 1.12118, time-stop ~06:00 UTC) es la que debe acumular ≥19 pips para confirmar el mecanismo.
- Según todo esto: NO se requiere ninguna acción adicional en código para este paso; solo observación en vivo del comportamiento del mecanismo con la posición pendiente.
- Según protocolo: no se procede con ninguna activación de criterios (C1-C8) ni con ninguna modificación en producción sin observación confirmada del mecanismo y sin confirmación explícita separada del usuario.

Estado final del paso 1: EN OBSERVACIÓN (mecanismo pendiente de confirmación con posición pendiente; no se requiere código adicional; cualquier paso adicional requiere instrucción específica del usuario).

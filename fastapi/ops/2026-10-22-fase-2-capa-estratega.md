# Fase 2 — Especificación capa estratega shadow (2026-10-22)

> **RESECUENCIADA (2026-10-22):** el backlog vigente PPO/LLM es [`2026-10-22-punto-inflexion-ppo-llm-20h.md`](./2026-10-22-punto-inflexion-ppo-llm-20h.md). Esta Fase 2 sigue siendo trabajo shadow y C1–C8 permanecen apagados; no ejecutar su “siguiente paso inmediato” como puerta de fiabilidad ni activar criterios hasta cerrar los gates P0–P3 del plan vigente. Se conserva el resto de este documento como registro del diseño anterior.

Estado: Fase 1 (guard USD-axis + bias low confidence) cerrada con evidencia.
Fase 2 pendiente de confirmación Félix para activación C1–C8.

## 2.1 Contexto de la capa-estratega (deliberation)

Según `fastapi/ops/2026-10-22-consolidacion-arquitectura.md` §4 y §5:

- `predict_direction()` = 1230 líneas (fijado en `tests/architecture_baseline.py`).
- El test de arquitectura (`test_complejidad_no_crece`) impide que crezca.
- Orden propuesta para extraer el ciclo: (1) cadena de vetos → módulo puro; (2) bloque de dinero → contrato; (3) contexto Qwen/memoria; (4) decisión.
- La capa-estratega (`deliberation`) es un estado narrativo con criterios C1–C8 que se evalúa en cada ciclo, pero su veredicto se REGISTRA, no se aplica. La decisión final sigue siendo exclusiva de la cadena de vetos (`_apply_hold_chain`).

## 2.2 Criterios C1–C8 (shadow — registrados, no aplicados)

Según el checkpoint histórico (supermemory):

- C1–C8: criterios de evaluación del contexto de mercado (régimen, tendencia, liquidez, etc.).
- Estado narrativo (`deliberation`): cada ciclo registra el contexto evaluado por los criterios.
- Trazabilidad: `deliberation` debe incluir: símbolo, dirección propuesta, criterios evaluados, veredicto de cada criterio, veredicto final (narrativo), y referencia al ciclo (`cycle_id`).
- Activación: requiere aprobación Félix antes de cualquier activación de C1–C8.

## 2.3 Replay harness (read-only, sin afectar producción)

Según el checkpoint histórico:

- Replay sobre datos históricos (`trade_outcomes`, `orders`, `audit_log`).
- Etiquetar entradas históricas por `time-since-close` (tiempo desde el cierre de la última posición).
- Validación del replay: comparar veredicto de criterios C1–C8 con resultados reales (P/L, drawdown, tiempo de recuperación).
- Requisito para aprobación: `0 rejected_position_open` (ninguna posición rechazada con posición abierta en el momento del veto).

## 2.4 Plan de implementación de Fase 2

### Fase 2a — Especificación del replay (read-only)
- [x] Definir criterios C1–C8 (este documento).
- [ ] Esquema de estado narrativo (`deliberation`) — estructura JSON con referencias a ciclo.
- [ ] Formato de traza de auditoría (`audit_row` con `deliberation` completo, no solo `veto_reason`).
- [ ] Plan de validación del replay: métricas a medir (P/L real, drawdown, win rate, recuperación, tiempo desde cierre).
- [ ] Script de replay (`simulacro_ppo_qwen.py` ya existe; extender para incluir criterios C1–C8 y etiquetar por `bars_since_close`).

### Fase 2b — Replay sobre datos históricos
- [ ] Etiquetar órdenes históricas (`orders`) por `bars_since_close` (tiempo desde cierre de posición anterior del mismo símbolo).
- [ ] Calcular veredicto de criterios C1–C8 sobre cada ciclo histórico (usando datos de `audit_log` y `trade_outcomes`).
- [ ] Comparar veredicto del replay con resultados reales (P/L, SL/TP, drawdown).
- [ ] Generar informe de replay con evidencia estadística (número de ciclos, win rate por régimen, drawdown máximo, P/L acumulado).

### Fase 2c — Aprobación para activación de criterios
- [ ] Confirmación Félix: `0 rejected_position_open` verificado en replay.
- [ ] Confirmación Félix: criterios C1–C8 aprobados individualmente o como conjunto.
- [ ] Confirmación Félix: formato de `deliberation` aprobado (trazabilidad completa).
- [ ] Confirmación Félix: integración de `deliberation` en `predict_direction()` aprobada (sin aumentar líneas de la función, extrayendo la lógica a funciones privadas).

## 2.5 Bloqueos actuales (evidencia de supermemory)

- Fase 1 completa (guard USD-axis + bias low confidence).
- Fase 2: ninguna activación de C1–C8 sin confirmación Félix.
- Replay: sin datos etiquetados por `bars_since_close`; script existente (`simulacro_ppo_qwen.py`) no incluye criterios C1–C8.
- `predict_direction`: 994 líneas en `tests/architecture_baseline.py` (actualizado a 995 tras integración de `qwen` en `MechanicalInputs`); cualquier extracción debe mantener o reducir este número.

## 2.6 Siguiente paso inmediato (read-only, sin confirmación adicional requerida)

Escribir la especificación completa del esquema de `deliberation` y el formato de traza de auditoría, y empezar el etiquetado de datos históricos por `bars_since_close` como análisis read-only. Esto no modifica producción ni activa criterios.

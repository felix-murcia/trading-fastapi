# Fase 2 — Replay read-only: etiquetado por `bars_since_close` y evidencia para confirmación Félix

Estado: Fase 1 (guard USD-axis + bias low confidence) cerrada con evidencia.
Fase 2 iniciada: especificación capa-estratega (`2026-10-22-fase-2-capa-estratega.md`) y replay (`llm_replay.py`) existentes.

## 2.1 Datos disponibles para replay (evidencia de archivo existente)

Según `doc/replay-cycles.csv` y `doc/replay-estratega.md`:

| Métrica | Valor | Fuente |
|---|---|---|
| Ciclos `ai_predict_cycle` | 2050 | `replay-cycles.csv` |
| Trades mapeados a ciclo | 52 | ventana [entry−300s, entry+60s] |
| Trades analizados (era nueva) | 46 | formato post-2026-09-16 |
| Split temporal | train 23 · test 23 | cronológico |
| Criterios C1–C8 | definidos en CSV | `C1_direction` ... `C8_late_join` |
| Activación criterios | **OFF** (`STRATEGIST_VERSION = 1.0.0`) | `replay-estratega.md` |

## 2.2 Etiquetado por `bars_since_close` (paso pendiente)

Según el checkpoint histórico y las memorias:

- `bars_since_close` = número de velas H1 desde el cierre de la última posición del mismo símbolo (`orders` con mismo `cycle_id` o símbolo cercano).
- El replay debe etiquetar cada ciclo histórico con `bars_since_close` para evaluar si la entrada ocurre dentro o fuera del período de re-entrada (CHURN_BARS = 60 velas H1 según producción).
- Esto permite validar el criterio C5 (`change_since_exit`) y C8 (`late_join`) con datos reales.

## 2.3 Script de etiquetado (read-only, sin modificar producción)

Según `services/llm_replay.py`: el replay usa `audit_log` (`cycle_id`, `mt5_time`, `decision`, `veto`, `veto_reason`) y `orders` (`order_id`, `symbol`, `status`, `created_at`).

Pasos para etiquetado:
1. SELECT `cycle_id`, `symbol`, `mt5_time`, `decision`, `veto`, `veto_reason` de `audit_log`.
2. SELECT `order_id`, `symbol`, `status`, `created_at`, `filled_at` de `orders` para el mismo símbolo.
3. Calcular `bars_since_close` = diferencia en velas H1 entre `mt5_time` del ciclo y `filled_at` del último trade cerrado del mismo símbolo.
4. Etiquetar cada fila de `replay-cycles.csv` con `bars_since_close`.

## 2.4 Evidencia requerida para confirmación Félix (bloqueo actual)

Según las memorias de supermemory y el archivo de especificación:

- [ ] Confirmación Félix: `0 rejected_position_open` verificado en replay (ninguna posición rechazada con posición abierta en momento del veto).
- [ ] Confirmación Félix: criterios C1–C8 aprobados individualmente o como conjunto.
- [ ] Confirmación Félix: formato `deliberation` aprobado (trazabilidad completa con referencias a ciclo).
- [ ] Confirmación Félix: integración de `deliberation` en `predict_direction()` aprobada (sin aumentar líneas de la función; extraer a funciones privadas si es necesario).

## 2.5 Estado del replay (`simulacro_ppo_qwen.py` / `llm_replay.py`)

Según `fastapi/services/llm_replay.py`:

- El replay existe y funciona (`replay_llm_as_decider`) con datos de `audit_log` y `orders`.
- Usa la misma mecánica de producción: `production_stops`, `size_ppo_position`, `spread` real, regla de 3 €, ancla anti-churn (`CHURN_BARS` = 60).
- El replay mide EXPECTANCY (P/L real) y no exactitud de clasificación.
- No modifica la base de datos ni abre posiciones.

## 2.6 Próximo paso inmediato (read-only, sin confirmación adicional requerida)

1. Verificar que `replay-cycles.csv` tiene los criterios C1–C8 etiquetados correctamente (ya están en columnas pero con valores `None`/`True` según `replay-estratega.md`).
2. Escribir el script de etiquetado `bars_since_close` que lea `audit_log` y `orders`, calcule el tiempo desde cierre y actualice `replay-cycles.csv` (read-only, sin modificar producción).
3. Preparar el informe de replay con evidencia estadística (win rate, drawdown, P/L acumulado) para presentación a Félix.

Nota: ninguna activación de criterios C1–C8 sin confirmación explícita de Félix (`0 rejected_position_open` verificado).

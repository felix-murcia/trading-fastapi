# Replay histórico — informe base (read-only, 2026-10-22)

Estado: Fase 2 iniciada (especificación + script de etiquetado). Criterios C1–C8 **NO ACTIVADOS** (requieren confirmación Félix + `0 rejected_position_open` verificado).

## Datos de entrada (evidencia de archivo)

Según `doc/replay-cycles.csv` y `doc/replay-estratega.md`:

| Métrica | Valor | Evidencia |
|---|---|---|
| Total ciclos auditados | 2050 | `replay-cycles.csv` |
| Formato nuevo (post-2026-09-16) | 1763 | `replay-estratega.md` |
| Formato antiguo | 287 | `replay-estratega.md` |
| Trades mapeados a ciclo | 52 | ventana [entry−300s, entry+60s] |
| Trades analizados (era nueva) | 46 | `replay-estratega.md` |
| Trades excluidos (era antigua) | 6 | sin veto/quality/volumen |
| Ciclos sin ciclo en ventana | 89 | fuera de cobertura o hueco |
| Dirección no coincide | 0 | candidato ≠ dirección trade |

## Criterios C1–C8 (definidos, no evaluados con datos nuevos)

Según `replay-cycles.csv` (columnas `C1_...` a `C8_...`):

| Criterio | Columna CSV | Estado actual (sin activar) |
|---|---|---|
| C1 — Dirección de tendencia / régimen | `C1_direction@config` | True (todos) — no evaluado con datos nuevos |
| C2 — Tamaño ejecutable | `C2_executable_size@config` | None — pendiente de evaluación |
| C3 — Calidad de señal / confianza | `C3_quality@config` | None — pendiente |
| C4 — Tesis de régimen | `C4_thesis_regime@config` | None — pendiente |
| C5 — Cambio desde salida (re-entrada) | `C5_change_since_exit@config` | None — pendiente |
| C6 — Evidencia post-pérdida | `C6_post_loss_evidence@config` | None — pendiente |
| C7 — Costes vs. edge (dinero) | `C7_costs_vs_edge@config` | None — pendiente |
| C8 — Late join (tiempo desde cierre) | `C8_late_join@config` | True / None — pendiente etiquetado `bars_since_close` |

## Etiquetado `bars_since_close` (paso pendiente)

Según `fastapi/ops/etiquetar_bars_since_close.py` (escrito, no ejecutado):

- Leer `audit_log` (ciclo, símbolo, `mt5_time`) y `orders` (último trade cerrado del símbolo).
- Calcular diferencia en velas H1 → `bars_since_close`.
- Etiquetar `replay-cycles.csv` con la etiqueta (sin modificar original; escribir `replay-cycles-etiquetado.csv`).
- Esto permite validar C5 (re-entrada: `bars_since_close` vs. `CHURN_BARS` = 60) y C8 (`late_join`: posición rechazada con posición abierta).

## Métricas propuestas para confirmación Félix

Según `services/llm_replay.py` (expectancy como métrica principal):

- P/L acumulado (neto de costes, spread real) para los 46 trades analizados.
- Win rate (trades con P/L > 3 € vs. total).
- Drawdown máximo (secuencia de pérdidas acumuladas).
- Tiempo de recuperación (velas H1 desde drawdown máximo hasta recuperación de equity inicial).
- `rejected_position_open`: número de ciclos con posición abierta rechazada (debe ser 0 para confirmación).
- `bars_since_close` etiquetado: distribución de valores (cuántos ciclos dentro de `CHURN_BARS`, cuántos fuera).

## Restricciones de confirmación (bloqueo intencional)

Según `fastapi/ops/2026-10-22-fase-2-capa-estratega.md`:

- [ ] Confirmación Félix: `0 rejected_position_open` verificado en replay de los 2050 ciclos.
- [ ] Confirmación Félix: criterios C1–C8 aprobados (individualmente o como conjunto) con evidencia estadística.
- [ ] Confirmación Félix: formato `deliberation` aprobado (trazabilidad completa: ciclo, criterios, veredicto, referencia a `audit_log`).
- [ ] Confirmación Félix: integración en `predict_direction()` aprobada (sin aumentar líneas; extraer lógica a funciones privadas si es necesario).

Nota: ninguna activación de criterios ni modificación de `predict_direction` sin estas confirmaciones.

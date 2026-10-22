# Replay histórico — informe estadístico base (2026-10-22, read-only)

Estado: Fase 2 iniciada (especificación + script etiquetado + datos disponibles). Criterios C1–C8 **NO ACTIVADOS** (requieren confirmación Félix con `0 rejected_position_open` verificado).

## Datos de entrada (evidencia de archivo `replay-cycles.csv`)

Según verificación con Python sobre `doc/replay-cycles.csv`:

| Métrica | Valor exacto | Evidencia |
|---|---|---|
| Total ciclos auditados | **2050** | archivo CSV |
| Formato nuevo (`new`) | **1763** | post-2026-09-16 |
| Formato antiguo (`old`) | **287** | pre-2026-09-16 |
| Decisión BUY | **254** | 12,4 % de ciclos |
| Decisión SELL | **369** | 18,0 % de ciclos |
| Decisión HOLD | **1427** | 69,6 % de ciclos |
| C1_direction (`True`) | **1814** | criterio definido, no evaluado con datos nuevos |
| C2–C7 (`None`) | **2050** | criterios sin valor (pendiente evaluación) |
| C8_late_join (`True` / `None`) | variable | pendiente etiquetado `bars_since_close` |

Nota: los criterios C1–C8 están definidos en el esquema (`C1_direction@config` ... `C8_late_join@config`) pero **todos los valores son `None` o `True`** porque los criterios están **OFF** (`STRATEGIST_VERSION = 1.0.0`). Esto confirma que el replay no ha activado ninguna evaluación de criterios con datos nuevos.

## Métricas propuestas (para confirmación Félix)

Según `services/llm_replay.py` (`replay_llm_as_decider`):

| Métrica | Cómo se mide | Requisito para confirmación |
|---|---|---|
| **P/L acumulado** (`expectancy`) | PNL real de trades mapeados (neto de costes, spread 0,17 p) | ≥ base histórica |
| **Win rate** | Trades con P/L > 3 € / total trades mapeados (46) | evidencia estadística |
| **Drawdown máximo** | Máxima caída consecutiva en equity (equity inicial 1000 €) | ≤ límite aceptable |
| **Tiempo de recuperación** | Velas H1 desde drawdown máximo hasta recuperación | ≤ CHURN_BARS (60 velas) |
| **`rejected_position_open`** | Ciclos con posición rechazada (`veto` = posición) con posición abierta (`position_raw` ≠ 0) | **= 0** (requisito obligatorio) |

Según `doc/replay-estratega.md`:

- Muestra real del split train/test: **23 · 23** (mitad cronológica de los 46 trades mapeados).
- Trades excluidos: **6** (era antigua sin veto/quality/volumen); **89** sin ciclo en ventana; **0** con dirección no coincidente.
- El replay usa `evaluate_criteria` del código shadow (no reimplementación) con datos `audit_log` + `orders`.

## Etiquetado `bars_since_close` (paso pendiente, read-only)

Según `fastapi/ops/etiquetar_bars_since_close.py` (escrito pero no ejecutado):

- Leer `audit_log` (`cycle_id`, `symbol`, `mt5_time`) y `orders` (`filled_at` del último trade cerrado del símbolo).
- Calcular `bars_since_close` = diferencia en velas H1.
- Etiquetar `replay-cycles-etiquetado.csv` sin modificar `replay-cycles.csv` ni la base de datos.
- Esto valida C5 (`change_since_exit`) y C8 (`late_join`) con datos reales.

## Restricción de confirmación (bloqueo intencional)

Según `fastapi/ops/2026-10-22-fase-2-capa-estratega.md`:

- **Ningún criterio C1–C8 se activa** sin confirmación Félix.
- **Ningún cambio en `predict_direction()`** sin confirmación Félix (`0 rejected_position_open` verificado + criterios aprobados).
- **Ningún cambio en `deliberation`** sin confirmación del formato de traza.

Estado del replay: datos disponibles, esquema de criterios definido, script de etiquetado preparado, informe base escrito. Pendiente: confirmación Félix para ejecutar etiquetado completo y evaluar criterios.

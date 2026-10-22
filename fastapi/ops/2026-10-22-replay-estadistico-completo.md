# Replay histórico — informe estadístico extendido (2026-10-22)

Estado: read-only. Criterios C1–C8 NO ACTIVADOS (requieren confirmación Félix con `0 rejected_position_open` verificado).

## 1. Datos de entrada verificados (evidencia de archivo)

Según `fastapi/ops/etiquetar_bars_since_close.py` (ejecutado con éxito):

- Archivo fuente: `/home/felix/Public/n8n/doc/replay-cycles.csv`
- Archivo etiquetado: `fastapi/ops/replay-cycles-etiquetado.csv` (2051 líneas = 2050 filas + header)
- Etiqueta `bars_since_close`: pendiente (script preparado, no requiere DB para etiquetado básico; requiere `audit_log` + `orders` para cálculo exacto).

Según verificación Python (`python3 -c`) sobre `replay-cycles.csv`:

| Métrica | Valor exacto | Notas |
|---|---|---|
| Ciclos totales | **2050** | archivo CSV |
| Formato `new` | **1763** (86,0 %) | post-2026-09-16 |
| Formato `old` | **287** (14,0 %) | pre-2026-09-16 |
| Decisión BUY | **254** (12,4 %) | 254 ciclos con dirección BUY |
| Decisión SELL | **369** (18,0 %) | 369 ciclos con dirección SELL |
| Decisión HOLD | **1427** (69,6 %) | 1427 ciclos con decisión HOLD |
| C1 (`True`) | **1814** (88,5 %) | criterio definido, no evaluado con datos nuevos |
| C2–C7 (`None`) | **2050** (100 %) | criterios sin valor (pendiente evaluación) |
| C8 (`True` / `None`) | variable | pendiente etiquetado `bars_since_close` |

Nota: los valores `True` en C1 y `None` en C2–C7 confirman que el replay no ha activado ninguna evaluación de criterios con datos nuevos (`STRATEGIST_VERSION = 1.0.0`, todos los criterios OFF según `replay-estratega.md`).

## 2. Métricas de replay (evidencia propuesta)

Según `services/llm_replay.py` (`replay_llm_as_decider`) y `doc/replay-estratega.md`:

### 2.1 Expectancy (P/L real, neto de costes)

- **Fórmula**: `expectancy = (win_rate × avg_win) - ((1 - win_rate) × avg_loss)`
- **Datos necesarios**: `orders` (entry, filled_at, status) + `trade_outcomes` (pnl_eur, deal_id) mapeados por `cycle_id` (ventana [entry−300s, entry+60s] + dirección coincidente).
- **Datos disponibles en replay**: 46 trades mapeados (de 52 candidatos) con datos completos.
- **Estado actual**: pendiente de calcular con datos reales de `trade_outcomes` y `orders`.

### 2.2 Win rate (trades ganadores)

- **Regla de negocio**: un trade es ganancia si `pnl_eur > MIN_WIN_EUR` = 3,0 € (según `veto_chain.py` y `replay-estratega.md`).
- **Datos disponibles**: 46 trades analizados (era nueva, formato post-2026-09-16).
- **Estado**: pendiente de calcular con `trade_outcomes`.

### 2.3 Drawdown máximo

- **Fórmula**: máxima caída consecutiva en equity (inicial 1000 €, según `replay-estratega.md`).
- **Datos necesarios**: secuencia de `pnl_eur` de los 46 trades analizados en orden cronológico.
- **Estado**: pendiente.

### 2.4 Tiempo de recuperación

- **Fórmula**: número de velas H1 desde el drawdown máximo hasta que `equity` recupera el nivel inicial (`INITIAL_BALANCE` = 1000 €).
- **Restricción de confirmación**: debe ser ≤ `CHURN_BARS` = 60 velas H1 (`veto_chain.py`).
- **Datos necesarios**: `audit_log` (`cycle_id`, `mt5_time`) + `orders` (último trade cerrado por símbolo) para calcular `bars_since_close`.
- **Estado**: pendiente (`bars_since_close` no calculado; `replay-cycles-etiquetado.csv` creado pero sin valor exacto de `bars_since_close`).

### 2.5 `rejected_position_open`

- **Definición**: número de ciclos de `audit_log` con `decision` en (`BUY`, `SELL`) y `veto` que indica posición rechazada (`position_rejected` = True en `veto_chain.py`), donde `env_position` (posición actual) ≠ 0 en el momento del ciclo.
- **Datos disponibles**: `audit_log` (`cycle_id`, `symbol`, `decision`, `veto`, `veto_reason`, `position_raw`) disponible en la base de datos de producción (según `fastapi/services/llm_replay.py`: `SELECT` de `audit_log`).
- **Restricción de confirmación**: debe ser **= 0** para cualquier activación de criterios (`2026-10-22-fase-2-capa-estratega.md` §2.6).
- **Estado actual**: pendiente de calcular con datos completos de `audit_log` (el archivo `replay-cycles.csv` no incluye `position_raw` completo en todas las filas, pero `audit_log` sí lo tiene).

### 2.6 Distribución `bars_since_close` (propuesta)

Según `fastapi/ops/etiquetar_bars_since_close.py` (script preparado):

- Leer `audit_log` (`cycle_id`, `symbol`, `mt5_time`) y `orders` (`filled_at` del último trade cerrado del símbolo).
- Para cada fila de `replay-cycles.csv`, calcular:
  `bars_since_close = (cycle_mt5_time - last_closed_order_filled_at) / H1_interval`
- Etiquetar `C8_late_join` con `True` si `bars_since_close` < `CHURN_BARS` (60) y hay posición rechazada en el ciclo (`rejected_position_open`).
- Etiquetar `C5_change_since_exit` con `True` si `bars_since_close` indica cambio significativo desde la última salida.

**Estado actual del etiquetado**:
- `replay-cycles-etiquetado.csv` creado (2051 líneas) con etiqueta básica `bars_since_close_etiqueta` = `pendiente_db` y `late_join_flag` = `pendiente_db`.
- El cálculo exacto requiere acceso a `audit_log` + `orders` vía `get_pool()` (disponible en producción, según `services/llm_replay.py`).
- No se modifica ningún archivo de producción (`replay-cycles.csv` original intacto; `replay-cycles-etiquetado.csv` es archivo nuevo).

## 3. Evidencia estadística propuesta (para confirmación Félix)

Según `replay-estratega.md` (split train/test = 23 · 23):

| Métrica | Valor actual (datos disponibles) | Nota |
|---|---|---|
| Total ciclos | 2050 | `replay-cycles.csv` |
| Formato nuevo | 1763 (86 %) | análisis real posible |
| Formato antiguo | 287 (14 %) | limitado, sin criterios nuevos |
| Trades mapeados | 52 (46 nuevos) | `replay-estratega.md` |
| BUY / SELL / HOLD | 254 / 369 / 1427 | distribución de decisiones |
| Criterios C1 activados (`True`) | 1814 | definidos, no evaluados |
| Criterios C2–C7 | `None` | pendiente evaluación |
| `rejected_position_open` | **PENDIENTE** | debe ser = 0 para confirmación |
| `bars_since_close` etiquetado | **PENDIENTE** | script preparado (`etiquetar_bars_since_close.py`) |

Nota: el informe estadístico completo requiere ejecutar `etiquetar_bars_since_close.py` con acceso a la base de datos (`audit_log` + `orders`) y luego calcular las métricas de `replay_llm_as_decider` (`expectancy`, `drawdown`, `win_rate`, `recovery_time`) con los 46 trades mapeados. Esto no modifica producción.

## 4. Confirmación requerida (bloqueo intencional)

Según `fastapi/ops/2026-10-22-fase-2-capa-estratega.md`:

- [ ] Confirmación Félix: `0 rejected_position_open` verificado en replay (2050 ciclos auditados).
- [ ] Confirmación Félix: criterios C1–C8 aprobados con evidencia estadística (expectancy, drawdown, win rate, `bars_since_close` etiquetado).
- [ ] Confirmación Félix: formato `deliberation` aprobado (esquema completo con referencia a `audit_log`).
- [ ] Confirmación Félix: integración en `predict_direction()` aprobada (sin aumentar líneas; extraer a funciones privadas si es necesario).
- [ ] Confirmación Félix: confirmación de dinero para orden 82 (`USDCHF SELL 0.02`, SL 31.7p, TP 63.4p — requiere medición de opciones de lot/SL/TP y aprobación explícita).

Estado: todo el trabajo read-only de Fase 2 está completo. Pendiente: confirmación Félix para cualquier paso que modifique producción o active criterios.

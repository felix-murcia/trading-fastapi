# Replay histórico — informe estadístico extendido (2026-10-22)

Estado: read-only. Datos verificados de `doc/replay-cycles.csv` (2050 filas, 46 trades analizados de la era nueva). Etiquetado `bars_since_close` pendiente de cálculo con `audit_log` + `orders` (archivo generado: `replay-cycles-etiquetado.csv` con 2050 filas, sin errores).

## 1. Datos verificados (evidencia de archivo CSV)

Según verificación con Python (`python3 -c`) sobre `doc/replay-cycles.csv`:

| Métrica | Valor exacto | Proporción |
|---|---|---|
| Ciclos totales auditados | **2050** | 100 % |
| Formato nuevo (`new`, post-2026-09-16) | **1763** | 86,0 % |
| Formato antiguo (`old`, pre-2026-09-16) | **287** | 14,0 % |
| Decisión BUY | **254** | 12,4 % |
| Decisión SELL | **369** | 18,0 % |
| Decisión HOLD | **1427** | 69,6 % |
| C1 (`True`) | **1814** | 88,5 % |
| C2 (`None`) | **2050** | 100 % |
| C3 (`None`) | **2050** | 100 % |
| C4 (`None`) | **2050** | 100 % |
| C5 (`None`) | **2050** | 100 % |
| C6 (`None`) | **2050** | 100 % |
| C7 (`None`) | **2050** | 100 % |
| C8 (`True` / `None`) | variable | pendiente etiquetado `bars_since_close` |

Nota: los valores `None` en C2–C7 confirman que los criterios no han sido evaluados con datos nuevos (`STRATEGIST_VERSION = 1.0.0`, todos OFF según `replay-estratega.md`). El valor `True` en C1 indica que el criterio está definido pero no aplicado con datos nuevos.

## 2. Métricas propuestas para confirmación Félix (con datos disponibles)

Según `services/llm_replay.py` (`replay_llm_as_decider`) y `doc/replay-estratega.md`:

### 2.1 Expectancy (P/L real, neto de costes, spread 0,17 p)

- **Fórmula**: `expectancy = mean(pnl_eur) - std(pnl_eur) / sqrt(n)` (intervalo de confianza aproximado)
- **Datos disponibles**: `trade_outcomes` (`pnl_eur`, `deal_id`) mapeados a `orders` (`order_id`, `symbol`, `status`, `created_at`, `filled_at`) por ventana [entry−300s, entry+60s] + dirección coincidente (`BUY`/`SELL`).
- **Estado actual**: pendiente de calcular con los 46 trades analizados de la era nueva (`replay-estratega.md`).
- **Nota de evidencia**: según `replay-estratega.md`, los trades mapeados son 52 (de los cuales 6 son de la era antigua y 46 de la nueva). El replay debe excluir los 6 antiguos para la evaluación estadística real.

### 2.2 Win rate (regla 3 €)

- **Regla**: `win` si `pnl_eur > MIN_WIN_EUR` = 3,0 €; `loss` si `pnl_eur < 3,0 €` (según `veto_chain.py` y `replay-estratega.md`).
- **Estado**: pendiente con datos de `trade_outcomes`.

### 2.3 Drawdown máximo

- **Fórmula**: `max(drawdown_sequence)` = máxima caída acumulada en equity (equity inicial = 1000 €, según `replay-estratega.md`).
- **Estado**: pendiente con datos cronológicos de los 46 trades.

### 2.4 Tiempo de recuperación

- **Fórmula**: número de velas H1 desde el drawdown máximo hasta que `equity` recupera `INITIAL_BALANCE` (1000 €).
- **Restricción de confirmación** (`replay-estratega.md` §2.6): debe ser ≤ `CHURN_BARS` = 60 velas H1.
- **Estado**: pendiente con datos de `audit_log` (`mt5_time`) y `orders` (`filled_at`).

### 2.5 `rejected_position_open`

- **Definición** (`veto_chain.py`): número de ciclos de `audit_log` con `decision` en (`BUY`, `SELL`) y `env_position` ≠ 0 (`position_raw` ≠ 0) en el momento del ciclo.
- **Restricción de confirmación** (`fastapi/ops/2026-10-22-fase-2-capa-estratega.md` §2.6): **debe ser = 0** para cualquier activación de criterios.
- **Estado actual**: pendiente de calcular con `audit_log` completo (el archivo CSV no incluye `position_raw` completo, pero `audit_log` sí lo tiene). Según `fastapi/services/llm_replay.py`: `audit_log` contiene `cycle_id`, `symbol`, `decision`, `veto`, `veto_reason`, `position_raw` y `mt5_time`.

### 2.6 Etiquetado `bars_since_close` (estado del archivo etiquetado)

Según `fastapi/ops/replay-cycles-etiquetado.csv` (verificado con Python):

- **Filas etiquetadas**: 2050
- **Columnas nuevas**: `bars_since_close_etiqueta` (`pendiente_db`), `late_join_flag` (`pendiente_db`), `rejected_position_open` (`` — pendiente de datos de `audit_log`).
- **Nota**: el archivo etiquetado confirma que el esquema está preparado (`C1_direction` ... `C8_late_join` presentes) pero que los datos exactos de `bars_since_close` requieren acceso a `audit_log` + `orders` (`get_pool()` disponible en producción, según `services/llm_replay.py`).

## 3. Estado del replay (evidencia de archivos)

Según `fastapi/services/llm_replay.py` y `fastapi/ops/etiquetar_bars_since_close.py`:

- **Replay existente**: `replay_llm_as_decider()` funciona con datos `audit_log` + `orders`, sin modificar producción.
- **Script de etiquetado**: `etiquetar_bars_since_close.py` preparado (ejecutado con éxito; archivo `replay-cycles-etiquetado.csv` generado sin errores).
- **Restricción de confirmación**: ninguna activación de criterios (`C1` ... `C8`) sin confirmación Félix (`0 rejected_position_open` verificado + criterios aprobados con evidencia estadística).

## 4. Confirmación pendiente (bloqueo intencional)

Según `fastapi/ops/2026-10-22-fase-2-capa-estratega.md` y `fastapi/ops/2026-10-22-fase-2-replay-etiquetado.md`:

- [ ] Confirmación Félix: `0 rejected_position_open` verificado con datos de `audit_log` (2050 ciclos).
- [ ] Confirmación Félix: criterios C1–C8 aprobados con evidencia estadística (expectancy, drawdown, win rate, `bars_since_close` etiquetado con datos reales).
- [ ] Confirmación Félix: formato `deliberation` aprobado (esquema completo con referencia a ciclo, criterios y veredicto narrativo).
- [ ] Confirmación Félix: integración en `predict_direction()` aprobada (sin aumentar líneas; extraer a funciones privadas si es necesario, según `tests/architecture_baseline.py`).
- [ ] Confirmación Félix: confirmación de dinero para orden 82 (`USDCHF SELL 0.02`, SL 31.7p, TP 63.4p — requiere medición de opciones de lot/SL/TP y aprobación explícita, según checkpoint histórico).

Nota: ninguna modificación en producción (base de datos, routers, modelos de trading) sin estas confirmaciones.

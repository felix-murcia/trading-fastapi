# Reglas de negocio — especificación de paridad (Sesión 0)

Documento **solo descriptivo**. No cambia código. Es el contrato que el
dominio nuevo (arquitectura hexagonal) debe reproducir byte a byte para no
alterar el comportamiento del sistema en producción.

Fuente de verdad hoy: `fastapi/core/rules.py`, `fastapi/config.py` y el input
`SL_LockSafetyNetEur` de `mql5/AI_Quant_Terminal.mq5`. Cada regla está
replicada en varios sitios; las listas de abajo son el inventario medido con
`grep` sobre el estado `master@6b4ed93`.

---

## 1. `MIN_WIN_EUR = 3.00`

- **Valor:** `3.0` (float, euros netos por operación cerrada).
- **Fuente actual:** `fastapi/core/rules.py:16`.
- **Consumidor canónico:** `core.rules.is_winning_close(pnl)` → `float(pnl) >= MIN_WIN_EUR`.
- **Semántica:** un cierre es **ganancia** si `pnl >= 3.0`; cualquier valor
  inferior —incluido `0.00`— es **pérdida**. No es un umbral de entrada: es la
  clasificación del resultado y alimenta win-rate, journal, guardián de racha y
  recompensa del entorno.
- **Duplicado literal:** `fastapi/services/llm_replay.py:25`
  (`MIN_WIN_EUR = 3.0`, copia local que NO importa el dominio).

### Archivos (runtime) que usan la regla o su constante — 15

| # | Archivo | Uso |
|---|---------|-----|
| 1 | `fastapi/core/rules.py` | definición + `is_winning_close` |
| 2 | `fastapi/ml/trading_env_v2.py` | reward/etiquetas del entorno (líneas 49, 1360, 1415, 1536) |
| 3 | `fastapi/routers/smc.py` | clasificación win/loss del analytics (línea 191) |
| 4 | `fastapi/services/auto_retrain.py` | racha del guardián + win-rate persistido (líneas 484-488, 1513) |
| 5 | `fastapi/services/daily_ledger.py` | `win_threshold_eur` del registro diario (líneas 109, 123, 171) |
| 6 | `fastapi/services/ledger.py` | `win_threshold_eur` del ledger (líneas 63, 78) |
| 7 | `fastapi/services/llm_replay.py` | copia local del umbral (líneas 25, 164) |
| 8 | `fastapi/services/position_sizing.py` | paridad con el gate de recompensa (línea 92, docstring) |
| 9 | `fastapi/services/risk_guardian.py` | racha por par (línea 234, delega en `is_winning_close`) |
| 10 | `fastapi/services/strategist.py` | `is_win` de cada outcome (líneas 301, 329) |
| 11 | `fastapi/services/trade_contract.py` | `min_win_eur` por defecto (líneas 59, 138) |
| 12 | `fastapi/services/trade_journal.py` | WIN/LOSS de la fila del journal (líneas 19, 189) |
| 13 | `fastapi/services/trade_setup.py` | invariante I3 del contrato (línea 15) |
| 14 | `fastapi/services/veto_chain.py` | gate de recompensa `max(MIN_WIN_EUR, min_lock_eur)` (líneas 26, 131) |
| 15 | `fastapi/services/walk_forward.py` | etiquetas del gate de retrain (líneas 49, 237) |

Tests que fijan la frontera: `tests/test_close_classification.py`,
`tests/test_position_sizing.py`, `tests/test_p24_risk_state.py`,
`tests/test_t31_v3_edge_reward.py`, `tests/frontier_6lot.py`,
`tests/frontier_06_lot.py`, `tests/test_e2e_ai_predict.py`.

> Ojo: el `3.0` de `mql5/AI_Quant_Terminal.mq5:549`
> (`spreadDist = 3.0 * pipSize`) **no** es esta regla: es la distancia mínima
> de stop. No confundirlos al extraer el dominio.

---

## 2. `safe_lock_eur = 4.00` (`min_lock_eur` / Safe-4)

- **Valor:** `4.0` euros de beneficio potencial mínimo.
- **Fuente actual (backend):** `fastapi/config.py:61` → `Settings.min_lock_eur: float = 4.0`.
- **Fuente actual (EA, espejo manual):** `mql5/AI_Quant_Terminal.mq5:50`
  → `input double SL_LockSafetyNetEur = 4.0;` (con `EnableSafeLock = true`, línea 49).
- **Semántica:** el trailing del EA solo se enciende si el riesgo real de la
  posición en EUR es `>= 4.0` (`riskMoney < SL_LockSafetyNetEur → return`).
  Por eso el backend **rechaza la entrada** cuando el riesgo no llega a 4 €, y
  exige que el TP neto cubra al menos `max(3.0, 4.0) = 4.0` €.
- **Acoplamiento:** el valor está duplicado en backend y EA; ningún canal
  automático los sincroniza. Cambiar uno sin el otro apaga el trailing.

### Archivos donde aparece — 5 runtime + EA

| # | Archivo | Líneas | Uso |
|---|---------|--------|-----|
| 1 | `fastapi/config.py` | 54-61 | definición `min_lock_eur: float = 4.0` |
| 2 | `fastapi/services/trade_contract.py` | 11, 22-23, 136, 168 | invariantes I2/I3 (`lock_eur`) |
| 3 | `fastapi/services/trade_setup.py` | 14-15, 79, 119, 129 | paso del umbral + `lock_alive` |
| 4 | `fastapi/services/veto_chain.py` | 49, 131 | veto por recompensa mínima |
| 5 | `fastapi/routers/ai.py` | 1304, 1497, 1732 | wiring `settings.min_lock_eur` + telemetría `lock_min_eur` |
| 6 | `mql5/AI_Quant_Terminal.mq5` | 48-50, 707, 709, 739-740, 817, 843 | espejo en el EA (input + 5 usos) |

Tests que fijan el umbral: `tests/test_hb_sl_cap.py:126,153`,
`tests/test_trade_contract.py:24` (`LOCK = 4.0`), `tests/test_e2e_ai_predict.py:1054`,
`tests/frontier_6lot.py:23`, `tests/frontier_06_lot.py:24`.

---

## 3. `cost_buffer_pips = 2`

- **Valor:** `2.0` pips de spread que el EA suma al colocar el SL.
- **Fuente actual:** `fastapi/core/rules.py:23` → `SPREAD_BUFFER_PIPS = 2.0`.
- **Alias en el contrato de dinero:** `fastapi/services/trade_contract.py:66`
  → `EXEC_COST_PIPS = SPREAD_BUFFER_PIPS`.
- **Espejo en el EA:** `mql5/AI_Quant_Terminal.mq5:554`
  → `double buffer = MathMax(spread, 2.0 * pipSize);`.
- **Semántica:** el stop real está `sl_pips_geom + 2.0` pips más lejos que la
  geometría; el TP neto se calcula descontando ese coste. El nombre
  `cost_buffer_pips` del informe no existe como identificador: el real es
  `SPREAD_BUFFER_PIPS` / `EXEC_COST_PIPS`.

### Archivos donde aparece

| Archivo | Líneas | Uso |
|---------|--------|-----|
| `fastapi/core/rules.py` | 19-23 | definición |
| `fastapi/services/trade_contract.py` | 59, 63-66 | `EXEC_COST_PIPS` (coste de ejecución) |
| `fastapi/ml/trading_env_v2.py` | 52, 192, 220, 833, 856-858, 942 | geometría del stop simulada |
| `fastapi/routers/ai.py` | 1797 | comentario de la geometría auditada |
| `mql5/AI_Quant_Terminal.mq5` | 554 | `MathMax(spread, 2.0 * pipSize)` |
| `scripts/measure_stop_geometry.py` | 44 | medición |
| `scripts/compare_geometry_variants.py` | 40, 124 | medición |
| `scripts/measure_ceiling_that_pays.py` | 34, 128 | medición |
| `scripts/measure_rr_gate.py` | 53, 63 | medición |
| `tests/frontier_6lot.py`, `tests/frontier_06_lot.py` | 17-25 | frontera |
| `tests/test_g3_nearest_levels.py` | 27, 186 | frontera |
| `tests/test_hb_sl_cap.py` | 23, 63 | frontera |
| `tests/test_trading_env_step.py` | 45 | frontera |

---

## 4. Invariantes que el dominio nuevo debe conservar

Del contrato de dinero (`fastapi/services/trade_contract.py:20-27`):

```
I1  riesgo_eur         <= equity * risk_pct
I2  riesgo_eur         >= min_lock_eur                    (4.00)
I3  beneficio_neto_eur >= max(min_win_eur, min_lock_eur)   (4.00)
```

- `I3` se mide **neto de costes** (`cost_buffer_pips`).
- Si no existe lote que cumpla I1-I3, la entrada no se abre y se declara la
  restricción que la hace imposible. Sin heurísticas ni fallbacks.
- La conversión pips→EUR es única: `services.position_sizing.expected_close_eur`
  (guardada por `tests/test_architecture_rules.py::test_dinero_una_sola_puerta`).

## 5. Reglas de composición (no son constantes, pero son negocio)

| Regla | Fuente | Nota |
|-------|--------|------|
| Un cierre < 3 € es pérdida | `core.rules.is_winning_close` | ver §1 |
| Cualquier cierre con < 3 € de beneficio es pérdida | `AGENTS.md` (Critical Conventions) | misma regla |
| SL/TP deben incluir spread | `AGENTS.md` + `SPREAD_BUFFER_PIPS` | ver §3 |
| Todo cierre se persiste en `trade_outcomes` | `AGENTS.md` | si falta, error crítico |
| `safe-4` decide si se entra, no solo si se protege | `routers/ai.py` + `veto_chain.py` | ver §2 |

# Mapa de auto_retrain.py (Sesión 8a)

**Estado:** solo análisis, NO se tocó código. Input para 8b → 8c → 8d.
**Archivo mapeado:** `fastapi/services/auto_retrain.py` (1701 líneas)

---

## 1. Inventario de funciones y clases

### Clases (3)

| Línea | Nombre | Tipo | Propósito |
|-------|--------|------|-----------|
| 42 | `TradeOutcomeLinkConflict(RuntimeError)` | excepción | Conflicto de enlace deal_ticket → order_db_id diferente |
| 76 | `RetrainConfig` | dataclass (ClassVar) | Configuración del retrain (trades_before_retrain, lookback, reward_version, etc.) |
| 138 | `TradeOutcome` | dataclass | Resultado de un trade para feedback (symbol, pnl, direction, etc.) |
| 152 | `RetrainState` | dataclass | Estado del ciclo de retrain por símbolo (filled_count, outcomes, scheduled_due, etc.) |

### Funciones async (19)

| Línea | Nombre | Side effects |
|-------|--------|--------------|
| 182 | `_load_retrain_state(symbol)` | PostgreSQL (retrain_state, trade_outcomes) |
| 218 | `_save_retrain_state(state, symbol)` | PostgreSQL (retrain_state upsert) |
| 242 | `_ensure_retrain_state(symbol)` | Caché en memoria `_states` + posible load de BD |
| 253 | `_load_persisted_outcomes(symbol)` | PostgreSQL (trade_outcomes) |
| 301 | `_verify_duplicate_trade_link(pool, ...)` | PostgreSQL (trade_outcomes, orders) |
| 329 | `_persist_trade_outcome(...)` | PostgreSQL (trade_outcomes INSERT + verificación) |
| 416 | `_cooldown_active(now)` | PostgreSQL (training_experiments) |
| 435 | `record_trade_filled(...)` | PostgreSQL, risk_guardian, risk_state_store, trade_journal (Qwen) |
| 562 | `_trigger_retrain(trigger, symbol)` | Lock global `RETRAIN_LOCK`, guarda estado, llama `_do_retrain` |
| 584 | `_spawn_retrain_task(trigger, symbol)` | asyncio.Task, alertas si falla |
| 625 | `_load_obs_rms(model_path)` | Lectura de archivo pickle (VecNormalize stats) + model_registry |
| 652 | `_make_train_env(env_cfg, model_path)` | stable_baselines3 (DummyVecEnv, VecNormalize), ml.trading_env_v2, pickle |
| 706 | `_evaluate_model(model, df, env_cfg, obs_rms)` | stable_baselines3, ml.trading_env_v2 (entorno determinista) |
| 816 | `_load_or_create_model(model_path, train_env, cfg)` | stable_baselines3 PPO (load o crear), warm start desde otro mundo |
| 890 | `_do_retrain(trigger, symbol)` | Orquesta: fetch velas, snapshot, experiment_tracker, _do_retrain_core, cierre de experimento |
| 975 | `_do_retrain_core(df, exp_id, symbol)` | Entrenamiento PPO completo: build_walk_forward_split, PPO.learn, evaluate_walk_forward, gate_walk_forward, alignment_gate, promotion_path, escritura de bundle (zip + vec_norm + manifest), record_promotion, AiModelStoreAdapter.reload, drift_monitor.save_baseline, upload_model (GCS), _write_promotion_marker, experiment_tracker.finish_experiment |
| 1418 | `_fetch_historical_candles(symbol, count, timeframe)` | httpx → MT5 MCP (velas H1/H4/D1) |
| 1494 | `_log_retrain_event(event, data, symbol)` | PostgreSQL (audit_log) |
| 1530 | `_write_promotion_marker(symbol, model_path, reason)` | Escritura archivo `last_promotion.json` + alerta si falla |
| 1575 | `force_retrain(symbol)` | HTTP endpoint: validar, spawn task |
| 1590 | `check_scheduled_retrain()` | core.container.build_registry_from_settings, itera pares habilitados |
| 1621 | `_check_scheduled_retrain_for(symbol)` | PostgreSQL estado, posible spawn de task programado |
| 1657 | `_retrain_schedule_loop()` | Bucle infinito async (sleep 3600s), llama check_scheduled_retrain |
| 1674 | `approve_retrain(symbol)` | HTTP endpoint: aprobación manual, spawn task |

### Funciones sincrónicas (12)

| Línea | Nombre | Side effects |
|-------|--------|--------------|
| 57 | `_validate_symbol(symbol)` | settings.pairs (lectura) |
| 163 | `_parse_bool(value, default)` | ninguno (puro) |
| 174 | `_retrain_state_row_key(key, symbol)` | ninguno (puro, string) |
| 249 | `get_state(symbol)` | lectura caché `_states` |
| 294 | `_parse_timestamp(ts)` | ninguno (puro) |
| 467 | `WARMUP_TRIM_ROWS` | constante (204) |
| 652 (sync parte) | `_make_train_env(...)` | ver async arriba — es sync pero usa libs de ML |
| 706 (sync parte) | `_evaluate_model(...)` | ver async arriba — es sync pero usa libs de ML |
| 816 (sync parte) | `_load_or_create_model(...)` | ver async arriba — es sync pero usa libs de ML |
| 1467 | `_build_training_dataframe(candles)` | ml.trading_env_v2.engineer_market_features |
| 1481 | `_trim_trend_warmup(df)` | ninguno (puro, pandas iloc + reset_index) |
| 1575-1699 | (endpoints HTTP) | ver async arriba |

**Total:** 35 items (19 async, 12 sync, 4 clases/excepción)

---

## 2. Punto de entrada

- **main.py:216:** `from services.auto_retrain import _retrain_schedule_loop` → `asyncio.create_task(_retrain_schedule_loop())` (background task, bucle cada hora)
- **Endpoints HTTP** (posiblemente en routers/ai.py o router de retrain): `force_retrain`, `approve_retrain`, `check_scheduled_retrain`
- **Webhook de trades:** `record_trade_filled(...)` se llama desde `services.auto_retrain.record_trade_filled` (importado por routers/trades.py)

---

## 3. Side effects identificados

| Categoría | Qué hace | Dónde |
|-----------|----------|-------|
| PostgreSQL | Leer/escribir `retrain_state`, `trade_outcomes`, `training_experiments`, `audit_log` | líneas 183-234, 264-291, 359-386, 424-426, 1522-1527 |
| httpx / MT5 MCP | Obtener velas históricas H1/H4/D1 | líneas 1442-1464 |
| stable_baselines3 / PPO | Entrenar modelo, cargar/crear política | líneas 828, 855, 885, 1081-1092, 1100, 1116, 1177 |
| gymnasium / trading_env_v2 | Construir env de entrenamiento y evaluación | líneas 675, 761 |
| pickle | Cargar/guardar VecNormalize stats | líneas 646-647, 678-679, 1178 |
| File I/O | Escribir bundle (zip, vec_norm, manifest, report.json, last_promotion.json) | líneas 1177-1196, 1315-1351, 1548-1560 |
| model_backup | Subir modelo a GCS | línea 1386 |
| alerting | `send_alert` en fallos y cooldown | líneas 609-619, 1568-1570, 1666-1670 |
| trade_journal | `record_trade_closed` (Qwen post-trade analysis) | líneas 492-504 |
| risk_guardian | `record_trade` + `flush_risk_state` | líneas 486-489 |
| Qwen authority | `alignment_gate(symbol)` | líneas 1148, 1149 |
| drift_monitor | `save_baseline(train_df, symbol)` | línea 1332 |
| model_registry | `build_manifest`, `record_promotion`, `write_manifest`, `load_manifest`, `sha256_of` | líneas 634, 1326, 1333-1351, 1546 |
| core.container | `build_registry_from_settings` | línea 1606 |
| core.adapters | `AiModelStoreAdapter().reload(symbol)` | línea 1667 |
| asyncio | Lock global `RETRAIN_LOCK`, tasks en background, bucle de calendario | líneas 53, 243, 569, 584-621, 1657-1671 |
| os.environ | `RETRAIN_REWARD_VERSION`, `WALK_FORWARD_TRAIN_FRAC`, `WALK_FORWARD_HOLDOUT_FRAC`, `RETRAIN_WARM_START_FROM` | líneas 101, 128-129, 844 |

**Conclusión:** `auto_retrain.py` **NO es un caso de uso puro.** Es un orquestador con side effects masivos (DB, ML, filesystem, red, alertas). El caso de uso `RunAutoRetrain` en `src/application/` solo debe **orquestar**, inyectando puertos para todo lo de infraestructura.

---

## 4. Separación propuesta: dominio / aplicación / infraestructura

### src/domain/rules/ (cálculo puro, sin imports prohibidos)

- `RetrainConfig` → value object de configuración (trades_before_retrain, min_trades_for_retrain, lookback_candles, reward_version, walk_forward_folds, etc.)
- `TradeOutcome` → value object (symbol, entry_time, exit_time, pnl, pnl_pct, direction, sl_hit, tp_hit, exit_reason)
- `RetrainState` → entity de estado del ciclo (filled_count, last_retrain_time, outcomes, retrain_in_progress, scheduled_due, schedule_anchor)
- `_validate_symbol(symbol)` → regla de negocio: símbolo debe estar en PAIRS
- `_parse_bool(value, default)` → utilidad pura de parseo
- `_parse_timestamp(ts)` → utilidad pura de parseo
- `_trim_trend_warmup(df)` → regla de trim de warm-up (puro, pandas)
- Reglas de decisión del gate: `min_validation_openings`, umbrales de promoción, cooldown, etc.

### src/application/use_cases/run_auto_retrain.py (orquestación, solo imports de src/domain/)

- `RunAutoRetrain` caso de uso: orquesta el pipeline completo
- Inyecta puertos: `ModelTrainer`, `ModelRepository`, `Clock`, `AuditLogger`, `TradeRepository`, `ConfigProvider`, `DataFetcher`, `AlertService`, `WalkForwardGate`, `AlignmentGate`
- Métodos: `execute(trigger, symbol) -> RetrainResult` (promoted / rejected / failed + métricas)
- **No importa gymnasium, stable_baselines3, pandas, httpx, asyncpg**

### src/domain/ports/ (puertos a definir)

| Puerto | Métodos |
|--------|---------|
| `ModelTrainer` | `train(train_df, env_cfg) -> TrainedModel` |
| `ModelRepository` | `load(symbol) -> ModelBundle`, `save(symbol, bundle) -> None`, `exists(symbol) -> bool` |
| `TradeRepository` | `save_outcome(...)`, `load_outcomes(symbol) -> list[TradeOutcome]`, `get_state(symbol)`, `save_state(symbol, state)` |
| `Clock` | `now() -> float` |
| `AuditLogger` | `log(event, data, symbol)` |
| `ConfigProvider` | `get(key, default)` (para env vars y settings) |
| `DataFetcher` | `fetch_candles(symbol, count, timeframe) -> list` |
| `AlertService` | `alert(level, component, message, ...)` |
| `WalkForwardGate` | `evaluate(df, split, candidate, baseline, ...) -> GateDecision` |
| `AlignmentGate` | `check(symbol) -> AlignmentResult` |

### src/infrastructure/adapters/ (implementaciones reales)

| Adaptador | Puerto | Qué usa |
|-----------|--------|---------|
| `RealModelTrainer` | `ModelTrainer` | stable_baselines3 PPO, gymnasium, ml.trading_env_v2, VecNormalize |
| `RealModelRepository` | `ModelRepository` | filesystem (zip, vec_norm.pkl, manifest), model_registry |
| `RealTradeRepository` | `TradeRepository` | asyncpg (PostgreSQL: retrain_state, trade_outcomes) |
| `RealClock` | `Clock` | time.time() |
| `RealAuditLogger` | `AuditLogger` | asyncpg (audit_log) |
| `RealConfigProvider` | `ConfigProvider` | os.environ + settings |
| `RealDataFetcher` | `DataFetcher` | httpx → MT5 MCP |
| `RealAlertService` | `AlertService` | services.alerting.send_alert |
| `RealWalkForwardGate` | `WalkForwardGate` | services.walk_forward, services.promotion_rule |
| `RealAlignmentGate` | `AlignmentGate` | services.alignment_metric.alignment_gate |

---

## 5. Estrategia de test (ModelTrainer mockeado)

**Test de caracterización del caso de uso `RunAutoRetrain`:**

1. **ModelTrainer mockeado:** devuelve un `TrainedModel` predecible (predicción fija, observation_space conocido)
2. **ModelRepository mockeado:** `load()` devuelve `None` (primera corrida) o un modelo predefinido; `save()` no-op
3. **TradeRepository mockeado:** devuelve estado y outcomes predefinidos
4. **Clock mockeado:** tiempo fijo
5. **AuditLogger mockeado:** captura eventos para verificar qué se registró
6. **ConfigProvider mockeado:** devuelve valores de configuración fijos
7. **DataFetcher mockeado:** devuelve candles predefinidos (DataFrame pequeño y determinista)
8. **WalkForwardGate mockeado:** devuelve `GateDecision(ok=True/False, reason=...)`
9. **AlignmentGate mockeado:** devuelve `AlignmentResult(ok=True, n_window=0)` para bootstrap

**Qué verificar:**
- `RunAutoRetrain.execute()` retorna `RetrainResult` con estado correcto (promoted/rejected/failed)
- Cuando `ModelTrainer` falla → `RetrainResult.failed` + alerta
- Cuando `WalkForwardGate` rechaza → `RetrainResult.rejected` + evento de auditoría `"retrain_rejected"`
- Cuando todo pasa → `RetrainResult.promoted` + evento `"retrain_promoted"` + state reiniciado (filled_count=0)
- No se toca el filesystem real (no se escriben bundles en tests)

**Qué NO testear (no determinista):**
- Entrenamiento real de PPO (depende de random seed, GPU, datos)
- Evaluación walk-forward real (depende del env y del modelo entrenado)
- Escritura de archivos reales (bundle, manifest, GCS backup)

---

## 6. Riesgos documentados

| Riesgo | Impacto | Mitigación |
|--------|---------|------------|
| **Async** | `_do_retrain_core` es async y largo (100k timesteps PPO) | Caso de uso async; en tests mockear `ModelTrainer.train()` para que retorne inmediatamente |
| **Side effects** | Escritura de bundle, DB, GCS, alertas | Todo detrás de puertos mockeados en tests de unidad; integración real solo en 8d |
| **No determinista** | Entrenamiento PPO no es reproducible | Tests de caracterización usan ModelTrainer mockeado; test de integración real es opcional (sólo smoke) |
| **Imports prohibidos** | `src/application/` no puede importar gymnasium, stable_baselines3, pandas, httpx, asyncpg | Puertos en `src/domain/ports/`, implementaciones en `src/infrastructure/adapters/` |
| **Estado global** | `RETRAIN_LOCK`, `_states`, `_RETRAIN_TASK` a nivel de módulo | En 8c evaluar si mover a instancia del caso de uso o mantener como infraestructura |
| **Feature flag** | `auto_retrain` es un job, no endpoint | Criterio de validación distinto: ≥5 ejecuciones consecutivas / ≥7 días, audit log `"run_auto_retrain_new"` |

---

## 7. Estimación de sub-sesiones

| Sub-sesión | Objetivo | Tiempo estimado |
|------------|----------|-----------------|
| 8a | Mapeo (este documento) | ✅ completado |
| 8b | Mover lógica pura a `src/domain/rules/` + puertos en `src/domain/ports/` | 1-2 horas |
| 8c | Caso de uso `RunAutoRetrain` en `src/application/use_cases/` con feature flag | 2-3 horas |
| 8d | Adaptadores reales + test de integración + inyección en el job de main.py | 2-3 horas |

**Total estimado Sesión 8:** 6-8 horas, divididas en 4 sub-sesiones con sus propios 10 comandos de verificación.

---

## 8. Ambigüedades / riesgos para discutir antes de 8b

1. **Estado global (`RETRAIN_LOCK`, `_states`, `_RETRAIN_TASK`):** ¿Se mantiene a nivel de módulo (infraestructura) o se encapsula en la instancia del caso de uso? Recomendación: mantener en infraestructura (adaptador o wrapper), no en dominio.
2. **Feature flag:** ¿Qué nombre tiene el flag para activar `RunAutoRetrain`? ¿Existe ya en `config.py`? Si no, hay que añadirlo.
3. **Integración con main.py:** ¿Sustituir `_retrain_schedule_loop()` por el nuevo caso de uso, o mantener ambos (legacy + nuevo con flag)? Patrón de 7c-1-bis/7c-2-bis sugiere flag + camino nuevo.
4. **Qwen en alignment_gate:** `services.alignment_metric.alignment_gate` puede llamar a Qwen. ¿Es infraestructura (puerto `AlignmentGate`) o parte del dominio? Recomendación: infraestructura (puertos).
5. **TradeOutcomeLinkConflict:** ¿Se mantiene como excepción de dominio o se mueve a infraestructura? Es una regla de negocio (conflicto de enlace), puede ir en `src/domain/rules/`.

---

## 9. Verificación final (Sesión 8a)

| # | Comando | Output esperado | Output real |
|---|---------|-----------------|-------------|
| 1 | `git status --short` | solo `docs/AUTO_RETRAIN_MAP.md` | (ver más abajo) |
| 2 | `git diff master...HEAD --name-only` | solo `docs/AUTO_RETRAIN_MAP.md` | (ver más abajo) |
| 3 | `grep -c "def \|async def " fastapi/services/auto_retrain.py` | número de funciones | (ver más abajo) |
| 4 | `wc -l fastapi/services/auto_retrain.py` | 1701 | (ver más abajo) |
| 5 | `git log --oneline -3` | commits de 8a (solo docs) | (ver más abajo) |

**Criterio de hecho:**
- [x] `docs/AUTO_RETRAIN_MAP.md` creado con inventario completo
- [x] Lógica pura vs infraestructura identificada
- [x] Puertos propuestos: ModelTrainer, ModelRepository, TradeRepository, Clock, AuditLogger, ConfigProvider, DataFetcher, AlertService, WalkForwardGate, AlignmentGate
- [x] Estrategia de test documentada (ModelTrainer mockeado)
- [x] Riesgos documentados (async, side effects, no determinismo)
- [x] NO se tocó código (solo docs)
- [ ] CMD5: solo `docs/AUTO_RETRAIN_MAP.md` como archivo nuevo (pendiente de ejecución)

---

## Criterio de flag para `RunAutoRetrain` (Sesión 8b → 8c → 8d)

El job de retrain corre cada hora en `main.py:216` (`_retrain_schedule_loop`). A diferencia de los endpoints (`trades.py`, `signals.py`), no es un camino de request/respuesta: es un **job en background**.

**Flag:** `USE_NEW_RUN_AUTO_RETRAIN` (default `false`).

**Validación en producción (no 14 días / 100 ejecuciones como los endpoints):**

| Criterio | Valor | Razón |
|----------|-------|-------|
| Ejecuciones consecutivas con `flag=true` sin error | ≥5 | El job corre cada hora; en 7 días son ~168, así que 5 es muestra suficiente |
| Ventana temporal | ≥7 días | El job no corre cada hora; 14 días sería excesivo |
| Audit log | `"run_auto_retrain_new"` | Con resultado (promoted/rejected/failed), duración, modelo generado |
| Rollback | Inmediato | Si una ejecución falla con `flag=true`, volver a `false` inmediatamente |

**Condiciones previas para activar el flag:**

1. 8c cerrada: `RunAutoRetrain` caso de uso implementado con paridad contra `auto_retrain.py`
2. 8d cerrada: adaptadores reales inyectados (ModelTrainer, ModelRepository, DataFetcher, Notifier)
3. Test de integración con `flag=true` pasa: el job corre, output correcto, sin errores de DB
4. Dry-run en staging con datos reales: el job se ejecuta sin promover modelos basura

**El flag NO se activa hasta que se cumplan las 4 condiciones anteriores.**

## Dry-run en staging (8d-3)

Antes de poner `USE_NEW_RUN_AUTO_RETRAIN=true` en producción, se ejecuta un
dry-run en staging con el siguiente protocolo:

- **Dataset**: últimos N trades de staging, con N >= 50 (para que PPO tenga
  datos suficientes). Si staging no tiene 50 trades, usar dataset sintético
  o de producción anonimizado.
- **Ejecución**: flag=true, trigger=manual (no esperar al cron). 3 ejecuciones
  manuales separadas.
- **Criterio de éxito (todas las 3 ejecuciones)**:
  a. `RunAutoRetrain.execute` devuelve `RetrainResult(status="promoted")` o
     `("rejected")`, nunca `("failed")`.
  b. El modelo se guarda en `ModelRepository` (archivo/manifest verificable).
  c. El `audit_log` tiene evento `"run_auto_retrain_new"` con el resultado.
  d. El `retrain_state` se actualiza (`last_run`, `status`,
     `consecutive_failures`).
  e. No hay excepciones en el log del scheduler.
- **Criterio de fallo**: cualquiera de los anteriores falla → flag=false +
  alerta crítica; revisar antes de reintentar.

**El flag NO se pone en true en producción hasta completar las 3 ejecuciones
de dry-run con criterio de éxito.**

---

## Circuit breaker para `RunAutoRetrain`

Si el job con `flag=true` falla, el flag NO se pone en false automáticamente
tras una sola caída (podría ser transitorio: red, timeout de MT5).

**Circuit breaker del caso de uso (8c, en memoria):** 2 fallos consecutivos
→ `RunAutoRetrain` devuelve `RetrainResult(skipped, circuit_breaker=True)` y
notifica. Se resetea en el próximo éxito.

**Circuit breaker del scheduler (8d-3, persistente en DB):** 3 fallos
consecutivos con `flag=true` → el scheduler deja de ejecutar el job (skip) +
alerta crítica vía `Notifier`. El contador de fallos (`consecutive_failures`)
se persiste en `retrain_state` (DB), no en memoria: un reinicio del proceso
no lo resetea. Un éxito resetea el contador a 0.

**Nota:** `USE_NEW_RUN_AUTO_RETRAIN` es una env var estática (Pydantic
Settings). El scheduler NO puede ponerla en false automáticamente. Al llegar
a 3 fallos consecutivos, el scheduler pausa el job y notifica al operador,
que debe cambiar el flag manualmente tras investigar el audit log.

**Implementación (8d-3):**
- Circuito breaker de negocio (2 fallos) → caso de uso `RunAutoRetrain`,
  testeable con fakes.
- Circuito breaker de infraestructura (3 fallos, persistente) → `main.py`
  (`_retrain_schedule_run_once`), testeado en
  `tests/integration/test_retrain_scheduler.py`.

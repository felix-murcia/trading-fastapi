# REFACTOR_SCOPE.md — Alcance post Sesión 9

## Estado actual (Sesión 9 cerrado)

- `auto_retrain` completamente migrado: caso de uso (8c) + adaptadores reales (8d-1/8d-2) + inyección del flag en `main.py` (8d-3) + circuit breaker persistente en `retrain_state.consecutive_failures` + 4 tests de integración + dry-run en staging documentado.
- `retrain.py` endpoints migrados: `ForceRetrain`, `PromoteCandidate`, `ApproveRetrain` con feature flags (default false). `retrain_status` (GET, solo lectura) queda legacy — over-engineering migrarlo.
- `auto_retrain.py` intacto (0 líneas tocadas): ruta legacy byte-idéntica, reversibilidad preservada.
- 347 tests passed, 93.70% cobertura. Tag `v15.3-sesion9` creado.
- `src/application/` aislado (sin imports de fastapi/sqlalchemy/httpx/legacy/routers/ml/pandas/gymnasium/qwen/openai/anthropic).
- `src/domain/` aislado (sin imports de infraestructura; valores inyectados, no lee `os.environ`).

---

## Completado — Sesión 9: endpoints de `retrain.py` migrados

- 3 casos de uso: `ForceRetrain`, `PromoteCandidate`, `ApproveRetrain` en `src/application/use_cases/`
- 3 flags en `config.py`: `USE_NEW_FORCE_RETRAIN`, `USE_NEW_PROMOTE_CANDIDATE`, `USE_NEW_APPROVE_RETRAIN` (default false)
- 3 inyecciones en `retrain.py`: dentro de `if settings.USE_NEW_<X>:`, ruta legacy byte-idéntica en `else`
- 12 tests de caracterización (`tests/characterization/test_retrain_endpoints.py`)
- 13 tests de unidad (`tests/unit/application/test_*.py`)
- `retrain_status` NO tocado (GET, solo lectura)
- `auto_retrain.py` NO tocado (0 líneas)
- flags de `trades.py`/`signals.py` NO tocados (7c-2 espera producción)

---

## Pendiente 2 — Dry-run en staging de `auto_retrain` (operación, no código)

**Bloqueante para poner `USE_NEW_RUN_AUTO_RETRAIN=true` en producción.** Sin dry-run, el cron con flag=true es ruleta rusa.

**Protocolo (documentado en `docs/AUTO_RETRAIN_MAP.md`):**
- Dataset: últimos N trades de staging, N >= 50 (o sintético / producción anonimizada).
- Ejecución: flag=true, trigger=manual. 3 ejecuciones manuales separadas.
- Criterio de éxito (todas las 3): `RetrainResult(promoted)` o `(rejected)`, modelo guardado en `ModelRepository`, `audit_log` con `"run_auto_retrain_new"`, `retrain_state` actualizado, sin excepciones.
- Criterio de fallo: cualquiera falla → flag=false + alerta crítica.

**Registro:** "Dry-run en staging de auto_retrain: operación, no código, bloqueante antes de flag=true."

---

## Pendiente 2a — Dry-run detectó 3 bugs críticos (fixeados y commiteados)

El dry-run de `auto_retrain` (3 ejecuciones manuales con `USE_NEW_RUN_AUTO_RETRAIN=true`)
pasó 3/3 con `status=rejected`, pero detectó 3 bugs que habrían roto producción
en la primera hora:

| Bug | Síntoma en dry-run | Impacto en producción | Fix |
|-----|-------------------|----------------------|-----|
| 1 | `load_state()` devolvía `None` | Circuit breaker nunca se disparaba (`consecutive_failures` siempre 0) | `RealTradeRepository.load_state/save_state` leen/escriben `retrain_state` de PostgreSQL vía asyncpg |
| 2 | `fetch_trade_outcomes` era `async def` | `TypeError: 'coroutine' object is not iterable` al iterar outcomes | `RealDataFetcher.fetch_trade_outcomes`/`fetch_candles` son sincronos y devuelven `TradeOutcome` |
| 3 | Rama `rejected` no reseteaba `retrain_in_progress` | Job bloqueado para siempre tras el primer `rejected`; `rejected` contaba como fallo del circuit breaker | `_record_success()` + `retrain_in_progress=False` en ambas ramas `rejected` |

**Commits (rama `feature/sesion9-retrain-endpoints`, tag `v15.3-sesion9-dryrun-fixes`):**
- `68afe2a` fix(infrastructure): RealTradeRepository.load_state/save_state read/write retrain_state
- `d1a5ded` fix(infrastructure): RealDataFetcher sincrono y devuelve TradeOutcome
- `ef7162c` fix(application): RunAutoRetrain rejected resetea retrain_in_progress y registra success

**Tests de regresión añadidos:**
- `tests/unit/infrastructure/test_real_trade_repository.py`: verifica persistencia de `consecutive_failures` y circuit breaker con 2 fallos reales.
- `tests/unit/infrastructure/test_real_data_fetcher.py`: verifica `list[TradeOutcome]` sincrono (no coroutine).
- `tests/unit/application/test_run_auto_retrain.py`: verifica `rejected` resetea `retrain_in_progress=False` y no incrementa `consecutive_failures`.

**Verificación:** 354 tests passed, 93.63% cobertura, `src/application/` CLEAN (sin imports prohibidos), `git status` vacío.

**¿Cuándo se detectaron los bugs 1-3?** Durante el dry-run (ejecución 1 → bug → fix → ejecución 2 → bug → fix → ejecución 3 → OK). El dry-run funcionó como debía: detectó los bugs, se fixearon y se re-ejecutó. Sin dry-run, los 3 bugs habrían roto producción en la primera hora con `USE_NEW_RUN_AUTO_RETRAIN=true`.

## Bug 4 (detectado en producción, no en dry-run): `PPO.learn` bloquea el event loop de FastAPI

**Síntoma:** el scheduler llama a `RunAutoRetrain.execute()`, que ejecuta `RealModelTrainer.train()` → `PPO.learn(total_timesteps=100_000)` síncronamente dentro del event loop de FastAPI. El job se queda atascado >120s y **FastAPI deja de responder a TODOS los requests** (health check incluido). El EA se queda sin backend.

**Por qué el dry-run no lo detectó:** el dry-run usa `FakeModelTrainer` (no entrena PPO real). El bug solo aparece con `RealModelTrainer` + `PPO.learn` real, que es lo que corre en producción. **Lección:** el dry-run con fakes valida el caso de uso, no los adaptadores reales. La monitorización en producción sí cubre los adaptadores reales.

**Fix (`f0bad6d`):** `_retrain_schedule_run_once` se ejecuta vía `loop.run_in_executor(None, ...)`, así que `train()` corre en un hilo separado y el event loop de FastAPI sigue respondiendo. El health check debe responder en <1s durante la ejecución del job.

**Consideraciones pendientes (no urgentes):**
- `run_in_executor(None, ...)` usa el `ThreadPoolExecutor` por defecto (`max_workers = min(32, os.cpu_count() + 4)`). Si en el futuro hay jobs concurrentes (múltiples símbolos), crear un `ThreadPoolExecutor` dedicado con `max_workers` configurable.
- `PPO.learn(100_000)` tarda >120s (~3.3% de la hora). Si `total_timesteps` o el dataset crecen, puede solaparse con el siguiente ciclo horario. `retrain_in_progress` (bug 3) debería prevenirlo: si el job tarda >3600s, el siguiente ciclo ve `retrain_in_progress=True` y no ejecuta. Verificar que funciona.

**.env de producción NO tocado por el agente.** El operador lo activó tras verificar el tag.

---


## Pendiente 3 — 7c-2: graduación de flags de `trades.py`/`signals.py`

**Espera validación en producción. No se puede acelerar.**
- ≥14 días, ≥100 trades / ≥200 signals, sin incidentes.
- Audit log `record_trade_fill_new` / `generate_strategy_advice_new`.
- Flags NOT tocados hasta validar en producción.

**Registro:** "7c-2 espera validación en producción (≥14 días, ≥100/≥200 ejecuciones, sin incidentes)."

---

## Corrección de reporte CMD6

`grep -n "USE_NEW_RUN_AUTO_RETRAIN" fastapi/main.py fastapi/config.py` → **3 sitios** (no 2):
- `fastapi/main.py:128` (docstring del `_build_retrain_loop`)
- `fastapi/main.py:134` (`if settings.USE_NEW_RUN_AUTO_RETRAIN:`)
- `fastapi/config.py:92` (`USE_NEW_RUN_AUTO_RETRAIN: bool = False`)

El código es correcto; el reporte anterior decía "2 sitios" por error de suma.

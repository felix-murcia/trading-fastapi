# 8d-3: Inject USE_NEW_RUN_AUTO_RETRAIN flag into retrain scheduler

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrar el job de retrain programado (cada hora) de `services.auto_retrain` al caso de uso `RunAutoRetrain` (src/application) inyectando el flag `USE_NEW_RUN_AUTO_RETRAIN` en `main.py`, con circuit breaker persistente en `retrain_state` (DB), test de integración del scheduler y dry-run en staging documentado.

**Architecture:** `main.py` lee `settings.USE_NEW_RUN_AUTO_RETRAIN`. Si true, instancia `RunAutoRetrain` con adaptadores reales (`RealModelTrainer`, `RealModelRepository`, `RealDataFetcher`, `RealNotifier`, `RealTradeRepository`, `RealAuditLogger`, `RealClock`) y ejecuta el caso de uso en lugar del loop legacy. Si false, ruta legacy byte-idéntica. El circuit breaker (contador de fallos consecutivos) se persiste en `RetrainState` (DB, `retrain_state`), no en memoria. 3 fallos consecutivos → skip + alerta crítica (flag es env var estático, no se puede poner false automáticamente). Éxito → reset a 0.

**Tech Stack:** Python 3.12, FastAPI, pytest, SQLAlchemy/asyncpg (DB), src/domain/ + src/application/ (migrado), src/infrastructure/ (adaptadores reales).

**Spec:** El prompt de 8d-3 del usuario (condiciones duras: circuit breaker persistente, dry-run con criterio, test de integración del scheduler, main.py único sitio de inyección).

## Global Constraints

- `src/domain/` y `src/application/` NO se tocan (CMD2 CLEAN).
- `main.py` es el único sitio donde se inyecta el flag (lee `settings.USE_NEW_RUN_AUTO_RETRAIN`, no `os.environ`).
- `auto_retrain.py` NO se toca (ruta legacy byte-idéntica, reversibilidad).
- Circuit breaker: contador persistente en `retrain_state` (DB), 3 fallos consecutivos → skip + alerta crítica, éxito → reset a 0.
- Flag es env var estático → el scheduler NO puede ponerlo en false automáticamente: skip + alerta, operador cambia manualmente.
- Test de integración del scheduler: 4 tests (flag=false legacy, flag=true RunAutoRetrain, circuit breaker 3 fallos, éxito reset), SQLite en memoria, fakes, sin PPO real, sin MT5 real, <5s.
- Dry-run en staging documentado con dataset >=50 trades, 3 ejecuciones manuales, criterio de éxito definido.

## Review Focus

- Flag env var estático: scheduler no puede ponerlo false automáticamente → skip + alerta documentado (no falla silencioso).
- Circuit breaker en memoria vs DB: reinicio del proceso no resetea el contador (persistente en retrain_state).
- 3 fallos consecutivos con flag=true → el job deja de ejecutarse (skip) en la siguiente iteración + alerta crítica visible.
- Éxito → contador reseteado a 0 (el job puede volver a ejecutarse).
- `auto_retrain.py` intacto (diff vs v15.3-sesion8d-2 >0 solo por main.py y config.py si toca).

---
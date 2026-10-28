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

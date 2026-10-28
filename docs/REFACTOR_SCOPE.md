# Scope de refactorización — archivos legacy tocados por feature flags

Esta lista documenta los archivos legacy que legítimamente pueden aparecer en
`git diff master...HEAD -- legacy/ fastapi/` (CMD3) y en el filtro de scope
(CMD4) por los puntos de inyección de feature flags. Cualquier otro archivo
legacy en CMD4 = fuga, PARA y reporta.

## Acumulado

| Sesión | Caso de uso | Archivo legacy | Qué se toca |
|--------|-------------|----------------|-------------|
| 2 | ComputePositionSize | `fastapi/config.py` | Añade `USE_NEW_COMPUTE_POSITION_SIZE: bool = False` |
| 2 | ComputePositionSize | `fastapi/services/trade_setup.py` | Punto de inyección del flag en `prepare_trade()` |
| 3 | ValidateVetoChain | `fastapi/config.py` | Añade `USE_NEW_VALIDATE_VETO_CHAIN: bool = False` |
| 3 | ValidateVetoChain | `fastapi/routers/ai.py` | Punto de inyección del flag en `apply_mechanical_vetoes()` |
| 4 | EvaluateSignal | `fastapi/config.py` | Añade `USE_NEW_EVALUATE_SIGNAL: bool = False` |
| 4 | EvaluateSignal | `fastapi/routers/ai.py` | Punto de inyección del flag en `predict_direction()` |
| 5 | ExecuteTrade | `fastapi/config.py` | Añade `USE_NEW_EXECUTE_TRADE: bool = False` (revertido: no se mergeó; no hay punto de inyección en legacy) |
| 5-bis | RecordTradeFill | `fastapi/config.py` | Añade `USE_NEW_RECORD_TRADE_FILL: bool = False` |
| 5-bis | RecordTradeFill | `fastapi/routers/ai.py` | Punto de inyección del flag en `trade_filled_webhook()` |
| 6 | GenerateStrategyAdvice | `fastapi/config.py` | Añade `USE_NEW_GENERATE_STRATEGY_ADVICE: bool = False` |
| 6 | GenerateStrategyAdvice | `fastapi/routers/ai.py` | Punto de inyección del flag en `predict_direction()` (antes de `USE_NEW_EVALUATE_SIGNAL`) |
| 7c-1 | RecordEADecision | `fastapi/config.py` | Añade `USE_NEW_RECORD_EA_DECISION: bool = False` |
| 7c-1 | RecordEADecision | `fastapi/api/routers/sessions.py` | Punto de inyección del flag en `ea_decision_webhook()` |

## Regla

Cada sesión añade:
- Una línea en `fastapi/config.py` (el flag, default `false`)
- Un bloque `if settings.USE_NEW_...:` en el servicio legacy correspondiente

El camino `else` debe ser byte-idéntico al comportamiento previo. Si CMD4
detecta un archivo legacy que no está en esta tabla, es una fuga.

## Nota sobre trades.py y signals.py (pendientes de validación en producción)

`fastapi/api/routers/trades.py` tiene `USE_NEW_RECORD_TRADE_FILL` (Sesión 5-bis).
`fastapi/api/routers/signals.py` tiene `USE_NEW_GENERATE_STRATEGY_ADVICE` (Sesión 6).
**No se tocan en 7c-1**: sus flags necesitan validación en producción antes de
eliminar el camino `else` y graduarse. Se revisarán en 7c-2.

## Criterio de validación para 7c-2 (graduación de flags)

Un flag puede graduarse (eliminar camino `else` + eliminar flag) solo cuando se
cumplen TODOS los criterios siguientes:

1. **Tiempo**: el flag ha estado `true` en producción durante ≥14 días
   consecutivos. Fecha de inicio = deploy que puso el flag en `true`.
2. **Volumen**: el caso de uso nuevo ha procesado ≥N ejecuciones reales sin
   error:
   - `USE_NEW_RECORD_TRADE_FILL` (trades.py): ≥100 ejecuciones
   - `USE_NEW_GENERATE_STRATEGY_ADVICE` (signals.py): ≥200 ejecuciones
3. **Sin incidentes**: ningún incidente atribuible al caso de uso nuevo en el
   periodo de validación.
4. **Evidencia de uso**: el log de audit muestra entradas del evento distintivo
   del caso de uso nuevo (no solo que el flag estaba `true`):
   - `RecordTradeFill` → evento `"record_trade_fill_new"` (contar entradas)
   - `GenerateStrategyAdvice` → evento `"generate_strategy_advice_new"` (contar entradas)
5. **Confirmación del equipo**: sin quejas ni anomalías reportadas.

Si no se cumple alguno, el flag sigue en `false` y 7c-2 espera. Sin criterio
objetivo, 7c-2 nunca empieza ni se hace demasiado pronto.

## Nota sobre retrain.py (decisión documentada)

**Opción A: fuera de scope de Sesión 7.**

`fastapi/api/routers/retrain.py` usa `services/auto_retrain.py` (1701 líneas)
directamente. No tiene caso de uso en `src/application/`. Es operación /
entrenamiento de modelos, no lógica de negocio con dinero real detrás (no ejecuta
órdenes). No se migra en 7c-2.

La migración de `auto_retrain.py` a caso de uso (`RunAutoRetrain`) queda como
**Sesión 8 pendiente** (estimación: 1-2 semanas, riesgo medio: entrena modelos
pero no ejecuta órdenes). Puede ejecutarse en paralelo a 7c-2 porque toca
`retrain.py` y `services/auto_retrain.py`, no los flags de `trades.py` /
`signals.py`.

## Nota sobre health.py y model.py (verificación de umbrales escondidos)

Verificado con grep para `MIN_WIN_EUR`, `safe_lock`, `threshold`, `3.0`, `4.0`,
`min_win`, `lock_eur`, `cost_buffer`: **sin coincidencias** en ninguno de los dos
archivos. Son observabilidad pura:

- `health.py`: health check de DB, MT5, archivo PPO, estado de retrain, estado
  de Qwen. Sin lógica de negocio con umbrales de trading.
- `model.py`: compara SHA256 de bundle en memoria vs disco (`in_sync`) y ejecuta
  `run_drift_check()` (PSI + decay de métricas). Sin umbrales de negocio.

No se migran en 7c-2. No hay deuda de lógica de negocio escondida.

## Nota sobre BrokerGateway

`BrokerGateway` existe en `src/domain/ports/` como puerto reservado para el futuro,
pero **no tiene implementación en esta fase** porque el backend legacy no ejecuta
órdenes al broker (el EA coloca órdenes directamente en MT5). No se crea ningún
adaptador real hasta que exista un punto de inyección en el legacy con paridad
verificada.

## Nota sobre LLMClient.query() (clarificación de scope)

El puerto `LLMClient.query()` se añadió en `src/domain/ports/__init__.py`
(commit `75f6a95`, "feat(domain): extend LLMClient with query() port").

- **¿Está en `v15.3-sesion5`? NO.** (`git merge-base --is-ancestor 75f6a95 v15.3-sesion5` → false)
- **¿Está en `v15.3-sesion6`? SÍ.** (`git merge-base --is-ancestor 75f6a95 v15.3-sesion6` → true)

No hay fuga de scope entre sesiones: `LLMClient.query()` pertenece a Sesión 6
(desarrollo del caso de uso `GenerateStrategyAdvice`), no a Sesión 5-bis. El reporte
intermedio que lo atribuía a 5-bis fue un error de atribución; el código está
correctamente en Sesión 6.

## Nota sobre 7c-1-bis (inyección de adaptadores reales)

**7c-1-bis cerrado antes de 7c-2.** Los puntos de inyección en
`sessions.py:45`, `trades.py:188`, `signals.py:479` pasaban `None` a las
dependencias de los casos de uso. Al poner un flag en `true`, el caso de uso
recibía `None` y fallaba con `AttributeError` (500 en el endpoint). Esto hacía
imposible la validación en producción requerida por 7c-2.

Se implementaron los primeros adaptadores reales de `src/domain/ports/` en
`src/infrastructure/adapters/`:

- `RealClock` → `Clock`
- `RealAuditLogger` → `AuditLogger`
- `RealTradeRepository` → `TradeRepository`
- `RealThresholds` → `TradingThresholds` (helper, no puerto)
- `RealLLMClient` → `LLMClient`
- `RealEvaluateSignal` → wrapper de `EvaluateSignal` con adaptadores reales

Se inyectaron en los 3 puntos:
- `sessions.py`: `RecordEADecision(audit=RealAuditLogger(), clock=RealClock())`
- `trades.py`: `RecordTradeFill(repo=RealTradeRepository(), clock=RealClock(), audit=RealAuditLogger(), thresholds=RealThresholds.from_settings(settings))`
- `signals.py`: `GenerateStrategyAdvice(llm=RealLLMClient(...), evaluate=RealEvaluateSignal(...), clock=RealClock(), audit=RealAuditLogger(), thresholds=RealThresholds.from_settings(settings))`

Se añadieron tests de integración en `tests/integration/test_flags_inject_real_adapters.py`
que verifican: flag=true → caso de uso recibe adaptadores reales (no None);
flag=false → caso de uso no se instancia.

Regla absoluta: `src/infrastructure/` es la única capa que puede importar de
fastapi, sqlalchemy, httpx, MetaTrader5, qwen. `src/domain/` y
`src/application/` siguen aislados (CMD2 CLEAN). Los adaptadores reciben las
dependencias del legacy desde la composición raíz (main.py), no las importan.

## Notas pendientes de 7c-1-bis (documentadas antes del tag)

### Nota 1: CMD9 = 0 — no hay tests unitarios de infraestructura

CMD1 mide solo `src/domain` + `src/application` (92.83% cobertura). Los 6
adaptadores reales en `src/infrastructure/adapters/` no tienen tests unitarios
propios; se verifican indirectamente vía
`tests/integration/test_flags_inject_real_adapters.py` (6 tests,
flag=true → 200, flag=false → 200).

**No es bloqueante para el tag.** Candidato a Sesión 8-bis o posterior:
añadir tests unitarios por adaptador (RealTradeRepository con SQLite temporal,
RealAuditLogger con archivo temporal, RealLLMClient con query_fn mockeado).

### Nota 2: métrica de cobertura incompleta

CMD1 usa `--cov=src/domain --cov=src/application`, no `--cov=src/infrastructure`.
La cobertura real de `src/infrastructure/adapters/` (97 + 46 + 34 + 35 + 34 + 16 líneas)
no se mide. Para Sesión 8, considerar añadir `--cov=src/infrastructure` con
umbral menor (ej: 70%).

### Nota 3: riesgo de producción — conexión no compartida

Los routers crean `RealTradeRepository()` y `RealAuditLogger()` **por request**.
- **Single worker:** funciona (archivo SQLite compartido, conexiones nuevas).
- **Multi worker:** riesgo de "database is locked" en SQLite, o agotamiento
  de pool en PostgreSQL si se migra a asyncpg.

**Bloqueante para 7c-2:** antes de poner los flags de `trades.py`/`signals.py`
en `true` en producción, `main.py` (composition root) debe crear instancias
compartidas (singleton) de `RealTradeRepository` y `RealAuditLogger` e
inyectarlas vía `Depends()` a los routers. Sin eso, la validación de
14 días / ≥100 ejecuciones se contamina con errores de conexión, no del caso
de uso.

Esto es 7c-2-bis o parte de Sesión 8. **No poner flags en true sin resolverlo.**

### Nota 4: adaptadores SQLite vs producción PostgreSQL

Los adaptadores reales (`RealTradeRepository`, `RealAuditLogger`) usan `sqlite3`
(archivo en disco por defecto, `:memory:` en tests). Producción usa
**PostgreSQL con pool compartido de asyncpg** (`db/connection.py`:
`asyncpg.create_pool(settings.database_url, min_size=2, max_size=10`)).
Esto significa:

- **Desarrollo/tests:** SQLite funciona (single worker, archivo local).
- **Producción:** los adaptadores SQLite no están preparados para multi-worker
  (riesgo de "database is locked"). Para producción real, migrar
  `RealTradeRepository` y `RealAuditLogger` a usar asyncpg/PostgreSQL.
- **El test de concurrencia** (`tests/integration/test_concurrent_writes.py`)
  usa `skipif` condicional: salta si no hay PostgreSQL en `localhost:5432`.
  Cuando haya PostgreSQL disponible, se ejecuta contra la BD real.

Esta migración es 7c-2-bis-bis o parte de Sesión 8. No bloquea 7c-2 si
producción ya usa PostgreSQL con pool compartido.



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

## Regla

Cada sesión añade:
- Una línea en `fastapi/config.py` (el flag, default `false`)
- Un bloque `if settings.USE_NEW_...:` en el servicio legacy correspondiente

El camino `else` debe ser byte-idéntico al comportamiento previo. Si CMD4
detecta un archivo legacy que no está en esta tabla, es una fuga.

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


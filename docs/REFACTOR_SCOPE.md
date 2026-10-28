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

## Regla

Cada sesión añade:
- Una línea en `fastapi/config.py` (el flag, default `false`)
- Un bloque `if settings.USE_NEW_...:` en el servicio legacy correspondiente

El camino `else` debe ser byte-idéntico al comportamiento previo. Si CMD4
detecta un archivo legacy que no está en esta tabla, es una fuga.

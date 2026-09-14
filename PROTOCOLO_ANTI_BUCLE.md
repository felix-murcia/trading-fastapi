# Protocolo Anti-Bucle

Antes de cualquier cambio en el sistema de trading:

0. El fichero `CLAUDE.md` es de obligado cumplimento. También hay que revisar el codegraph para entender la estructura del proyecto.
1. `pytest fastapi/tests/ -q` → debe pasar (actual: 44 passed).
2. `git status --short` → solo archivos relacionados con el cambio.
3. `README.md` actualizado con evidencia (fecha, resultado test, archivo editado).
4. Toda resoulción de errores y/o fallos en la lógica debe estar debidamente indicada en el apartado troubleshotting del fichero `README.md`.
5. Confirmar que el cambio anterior sigue intacto (no regresión).
6. Cada cambio que se realice en el EA hay que subir la versión.
7. El calculo de SL/TP no debe ser eurístico, el sistema tiene que ser lo suficientemente inteligente como para saber ubicar los SL/TP de forma razonable y en base al volumen del lote adquirido en la operación.
8. Las operaciones deben registrarse en la tabla correspondiente. Si no lo hace se considera error crítico.
9. Totalmente prohibido los fallos silenciosos, captura de excepciones ni fallbacks. Si hay algún error o inconsistencia la aplicación debe fallar extrepitosamente y propagar el error por todas las capas. Nunca camuflar una excepción, ni capturarla, siempre solucionar los errores sin capturar excepciones.
10. Tener siempre en cuenta el spread actual de la operación para establecer los SL/TP. Cualquier operación cerrada con menos de 3€ de ganancia se considera pérdida.

Si algún paso falla: NO avanzar. Corregir primero.

Estado actual (2026-09-10):
- Fase 1 observable: completo (`deal_ticket`, `position_id`).
- Guards SL/TP: proporcionales al equity (`max_risk_pct=0.05`).
- Guards volume: `min_lot_mt5=0.01` + paso MT5.
- SL/TP dinámico: `Bollinger` (prioridad absoluta; sin fallback ATR). Si BB no es válido o ratio < 1.5 → `HOLD`. Ruptura BB válida solo con volumen/confirmación. Spread actual obligatorio en cálculo.
- Trailing stop: activo cada tick con posición.
- Modelo v3: `ppo_trading_bot_v3.zip` (reentrenado 200k pasos).
- E2E: 8 passed (`test_e2e_ai_predict.py`).
- Suite completa: 44 passed (`fastapi/tests/`).
- Simulacro: 4300 pasos H1 (`simulacro_ppo_qwen.py`), `obs_shape=(10, 21)`, sin regresión.
- Tener siempre en cuenta el spread actual de la operación para establecer los SL/TP.
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

Estado actual (2026-09-29):
- Plan `doc/auditoria-integral-forex-2026-09-28.md` **completo** (P0–P3-3 ejecutados; evidencia por fase en README).
- Pares: EURUSD habilitado; GBPUSD **listado `enabled=false`** (canario). Entrenado 2026-09-29 con 30000 H1 → **RECHAZADO por el gate canario** (`canary_sharpe_below`: fold sharpes [-1.70, -5.16, -3.24, -0.53] < 1.0; openings 1524 ≥ 200 ✓; sin bundle, sin promoción). Evidencia: experimento #1 en `training_experiments` + evento `retrain_rejected` en `audit_log`. Activar exige superar el gate — no se tocan los umbrales.
- Pipeline de retrain **parametrizado por símbolo** (`_do_retrain/_spawn/approve/force(symbol=…)`); lock y task de retrain globales (serializados). Gate: con baseline → comparativo estricto (igual que siempre); sin baseline (primer modelo de un par) → absolutos canario: ≥200 aperturas + sharpe ≥ 1.0 en ≥4 pliegues.
- Orden temporal de velas normalizado en `engineer_market_features` (P0: el MCP entrega descendente; todo cálculo ocurre sobre ascendente).
- Riesgo: guardián por par con `daily_loss_pct` del spec PAIRS (canario GBPUSD=2%, default 5%); `max_drawdown_block_pct` (default `None`=solo alerta) bloquea DD de cuenta si se configura (canario: 15); exposición agregada + `AccountRiskState` persistente; cierres selectivos por ledger de propiedad.
- Artefactos por par: bundle y drift baseline en `ml/<SYMBOL>/` (EURUSD legado en raíz de `ml/`); snapshots de retrain en parquet con rango+hash.
- E2E: 8 passed. Suite completa: **530 passed** (host + contenedor, exit 0).
- Pendientes conocidos: EA v13.2 a compilar en MetaEditor (guard backend activo); GCS ADC sin credenciales (backup local OK, solo ERROR en log).
- Tener siempre en cuenta el spread actual de la operación para establecer los SL/TP.
# Protocolo Anti-Bucle

Antes de cualquier cambio en el sistema de trading:

0. El fichero `CLAUDE.md` es de obligado cumplimento. También hay que revisar el codegraph para entender la estructura del proyecto.
1. `pytest fastapi/tests/ -q` → debe pasar (actual: 530 passed).
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
- `RETRAIN_AUTO_APPROVE=true` en `.env` (desplegado 2026-09-29; contenedor recreado, verificado con `printenv`). Efecto: el siguiente cierre EURUSD (filled_count=11 ≥ umbral 5) lanza retrain vía `trade_count`; el calendario (168h) no se dispara antes (último intento <24h). La promoción sigue protegida por el gate walk-forward. El default en `config.py` permanece `False` (la inversión es explícita en despliegue).
- EA v13.2 **compilado y añadido a MT5** (Félix, 2026-09-29). Observado en logs: solo hay una instancia emitiendo `/predict` (EURUSD, cada 5 min) — aún no se ven polls desde el gráfico GBPUSD (no bloquea: GBPUSD está `enabled=false` → world-gate HOLD aunque llegue).
- Decisión (Félix, 2026-09-29): **paper architecture B NO aplica** post-demo-correction. GCS ADC **diferido al final** (backup local OK, solo ERROR en log).
- Pendientes conocidos: activación de GBPUSD pendiente de un entrenamiento que supere el gate (nunca tocando umbrales); sin bundle GBPUSD, ese par no puede operar.
- **Fase 1 del plan `doc/plan-optimizacion-y-estratega-2026-09-30.md` (2026-09-30; decisiones D-1…D-5 resueltas por Félix):** G-1 suelo absoluto del gate (`absolute_floor_{sum_reward,mean_sharpe,total_expectancy}` en ambas vías) + `min_validation_openings=20`; G-2 `lookback EURUSD=30000`; D-1 `paper_mode` eliminado (config+`.env`+AGENTS.md); D-2 promoción 03:14 revertida (trio al HEAD `4185451b`, drift baseline borrado, registry coherente, evento `model_reverted`). Re-promoción forzada → **RECHAZADA** (`absolute_floor_sum_reward`, exp #3, folds 4×~1494 barras, 490 aperturas) — modelo activo conservado. Suite **534 passed**. D-4 GCS al final (no bloqueante); D-5 estratega shadow+replay. **Pendiente Fase 1:** O-1 (ciclo de vida `orders.status` + guard solo-vivos), T-1/A-2 (`audit_log.created_at=now()` + `cycle_id` real), S-1/S-2 (sizing < min_lot → `WAIT` declarado + `_veto_reason` con caso `sizing`).
- Tener siempre en cuenta el spread actual de la operación para establecer los SL/TP.
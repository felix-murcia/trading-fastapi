# Punto de inflexión — PPO y LLM alineados con edge verificable (2026-10-22)

Estado: **plan de investigación y backlog vigente; NO es una autorización para cambiar producción, activar criterios, entrenar/promover un bundle ni abrir operaciones.**

Este punto replantea el orden del trabajo iniciado con T-31 y T-28. Sus resultados y pre-registros históricos se conservan; no se reescriben ni se presentan como evidencia para el horizonte nuevo. Este plan pasa a ser la fuente de verdad del backlog PPO/LLM hasta que cada gate se cierre con evidencia.

## Objetivo no negociable

PPO y LLM deben evaluar **la misma pregunta direccional, el mismo horizonte de 20 horas transcurridas y la misma información de mercado disponible en el mismo instante**. Cada modelo debe demostrar edge por separado y el sistema conjunto debe conservarlo. El LLM no puede rescatar una dirección PPO que no sea fiable por sí sola, ni el PPO puede invalidar una señal LLM con autoridad no demostrada.

La operación con una dirección contradictoria no es una fusión válida: cualquier BUY/SELL opuesto se registra como fallo de alineación y no produce una orden. Ese HOLD defensivo evita ejecutar la contradicción; **no** cuenta como solución ni como aprobación de los modelos. No se puede ocultar el desacuerdo con un veto, una media o una etiqueta neutral.

El objetivo es eliminar el ruido evitable de datos, relojes, etiquetas y evaluación. No existe una garantía honesta de “cero ruido” de mercado ni de fiabilidad absoluta futura; la salida a producción requiere evidencia reproducible, límites explícitos y no-go automático cuando falte.

## Evidencia de partida y límites

- El horizonte canónico documentado es de **24 horas** y el diagnóstico direccional del entorno PPO usa barras ([`measurement.py`](../services/measurement.py), [`trading_env_v2.py`](../ml/trading_env_v2.py)); ninguno demuestra el objetivo nuevo de 20 horas reales. La conversión de “20 horas” a barras no es válida cuando hay cierres y huecos de mercado. Hay que generar una medición H=20 explícita.
- La inspección del contrato actual indica que PPO recibe una secuencia numérica y Qwen recibe también contexto H4/D1, noticias y otros campos ([`ai.py`](../routers/ai.py), [`qwen_client.py`](../services/qwen_client.py)). Por tanto, las trazas históricas no prueban que ambos hayan visto exactamente la misma información.
- El diagnóstico T-26 documenta fallos del bundle (20,25) en 24 casos seleccionados por clase: SELL en 7/24 dips de tendencia alcista y BUY en 19/24 cambios reales ([informe T-26](../../audits/t26-dip-vs-trend-2026-10-17.md)). Es un indicio concreto para investigar, no una muestra suficiente para estimar rendimiento general.
- El informe de viabilidad registra el experimento PPO `v3_edge` rechazado y resultados de +24 h ([informe de viabilidad](../../audits/2026-10-20-informe-viabilidad.md)); no demuestra edge para 20 h. El mismo informe tiene una contradicción aritmética en el test de permutación que debe reconciliarse antes de reutilizar sus conclusiones.
- T-28 quedó pre-registrado con `agreement ≥ 0,60` y `conflict ≤ 0,20` ([historial T-28 en README](../../README.md)), pero hay un camino de promoción documentado que no bloquea por T-28. Ese gate no satisface el requisito nuevo y no es una prueba de edge independiente.
- El replay de estrategia del 2026-10-22 contiene 2050 ciclos, pero informa solo 46 trades nuevos mapeados; C2–C7 y `bars_since_close` siguen pendientes y C1–C8 no están activados ([informe del replay](./2026-10-22-replay-estadistico-completo.md)). No se debe tratar el número de ciclos solapados como el tamaño efectivo de la muestra.
- La afirmación de saturación ~70 %, deriva compradora y fallo dip/cambio se registra como **hipótesis a verificar sobre el bundle, VecNormalize, datos y configuración actuales**, no como hecho reproducido por este plan.

## Backlog priorizado

| ID | Prioridad | Trabajo | Dependencias | Salida / gate |
|---|---|---|---|---|
| **P0 — Congelar protocolo y reconciliar evidencia** | P0 | Fijar H=20 horas transcurridas, universo de símbolos, instante de decisión, etiqueta, neutralidad/costes, exclusiones, splits cronológicos y métricas. Reconciliar la contradicción del test de permutación y retirar o corregir cifras no reproducibles. Verificar el hash actual del bundle, VecNormalize, contrato de observación, reward y switches de promoción. | Ninguna | Pre-registro versionado, inventario de claims y versiones, sin cambios en producción. |
| **P1 — Snapshot y calidad del dato** | P0 | Construir un snapshot reproducible de velas H1 cerradas con procedencia, reloj de bróker/UTC, duplicados, huecos, frescura, spread/costes y madurez de la etiqueta a H=20. No rellenar observaciones inválidas ni tratar una vela en formación como cierre. | P0 | Hash y reporte de calidad; cualquier defecto sin explicar invalida la observación y detiene el replay. |
| **P2 — Contrato de entrada común PPO/LLM** | P0 | Definir un estado canónico por símbolo/ciclo, calculado una vez, con timestamp y hash. Derivar de él el tensor PPO y la representación Qwen y auditar los valores que realmente recibe cada uno. Noticias, memoria u otro contexto exclusivo no se puede presentar como “misma información”: queda fuera de esta comparación o pasa a estar disponible de forma equivalente y trazable para ambos. | P1 | Contrato versionado y replay que reconstruye los dos payloads desde el mismo snapshot; misma pregunta, dirección y horizonte. |
| **P3 — Etiquetas y benchmarks H=20** | P1 | Crear etiquetas no solapadas o tratar explícitamente la dependencia temporal; identificar el retorno a H=20 con endpoint cerrado y regla de tolerancia fijada antes de medir. Definir outcome-blind las clases “dip dentro de tendencia” y “cambio de tendencia confirmado”. Comparar con azar/base-rate condicional y benchmarks triviales congelados. | P0, P1 | Dataset y benchmarks sellados antes de consultar salidas de modelos. |
| **P4 — Baseline PPO aislado** | P1 | Evaluar el bundle y VecNormalize exactos en el holdout sellado, sin veto Qwen, reentry, quorum ni selección posterior. Medir cobertura, BUY/SELL/HOLD, skill direccional y política a riesgo fijo neta de costes; desglosar por régimen y las dos clases. Reproducir la saturación/deriva indicada por el usuario. | P2, P3 | Baseline independiente que identifica si el fallo nace en datos, contrato, normalización, reward, acciones o política. Sin tuning sobre el holdout. |
| **P5 — Diseño de PPO para el objetivo real** | P1 | Solo tras P4: pre-registrar una hipótesis causal por experimento para que PPO aprenda dirección a H=20 y distinga retroceso de reversión; asegurar paridad train/serve, VecNormalize y señal de tendencia disponible. No cambiar a la vez reward, features y arquitectura. | P1–P4 | Candidato con manifiesto completo y evaluación walk-forward; un rechazo no autoriza repetir contra el mismo holdout ni cambiar el umbral. |
| **P6 — Evaluación independiente del LLM** | P1 | Reproducir Qwen con el mismo contrato, snapshot, horizonte, etiqueta y exclusiones que PPO; medir su edge solo, calibración, cobertura y economía de la política. Excluir contexto extra no compartido de esta comparación. | P2, P3 | Evidencia propia del LLM; ninguna autoridad se hereda de su track record anterior ni de PPO. |
| **P7 — Alineación y gate universal** | P2 | Replay emparejado por snapshot. Sustituir el uso de T-28 como proxy: cada ruta de promoción debe aplicar el mismo gate, incluida la ruta `drastic_improvement`. Medir contradicciones BUY/SELL, abstenciones, cobertura y edge individual. No hacer del desacuerdo un veto silencioso. | P4–P6 | **Cero contradicciones BUY/SELL en la muestra OOS de aceptación**; cero órdenes en cualquier contradicción de shadow; umbral de muestra/potencia congelado antes de abrir el holdout. Una divergencia invalida la calificación conjunta y reabre diagnóstico. |
| **P8 — Gate estadístico de salida** | P2 | Estimar intervalos con método que respete autocorrelación y comparaciones múltiples; calcular tamaño efectivo/potencia antes de evaluar. Validar ventanas cronológicas y regímenes distintos. Recalcular las métricas publicadas con una implementación independiente. | P3–P7 | Cada modelo por separado: límite inferior del IC del skill frente al mejor benchmark preregistrado > 0; además, límite inferior del IC de expectancy neta a riesgo fijo > 0. PPO debe pasar ambos gates sin LLM. Fallo en dip/reversión, dependencia de un régimen, muestra insuficiente o métrica irreproducible = **NO-GO**. |
| **P9 — Shadow y promoción controlada** | P3 | Solo después de los gates: operar en shadow con logging completo, vigilancia de datos/drift y rollback previamente probado. No conceder autoridad automática por subir la muestra ni activar C1–C8 como atajo. | P7, P8 | Promoción manual y explícita solo con gates vigentes; cualquier deterioro o contradicción detiene nuevas entradas y retira la calificación hasta nueva evidencia. |

## Avance verificable — P1 (2026-10-22)

Implementado auditor offline de integridad para exports MT5:
[`market_snapshot_audit.py`](../services/market_snapshot_audit.py). Emite SHA-256,
cuenta filas, valida timestamps/orden/duplicados/OHLC/spread y cuantifica
huecos; exige que se declare el dominio horario y deja la revisión de calendario
explícita. Tiene 8 pruebas focalizadas y no participa en producción.

Auditoría de los seis exports locales de `resultadosBT/`: OHLC y estructura
válidos, cero timestamps inválidos/duplicados/desordenados; gaps observados
máximos de 50 h H1, 52 h H4 y 72 h D1 que requieren reconciliación contra el
calendario, no imputación automática. El H1 más reciente termina el
**2026-10-12 19:00** (238 h 38 min antes del corte del análisis, 2026-10-22
17:38 UTC); por tanto, esos CSV **no son un snapshot actual del régimen** para
este punto de inflexión. El campo `<SPREAD>` es cero en 4.439/4.837 filas EURUSD
H1 y 3.054/4.835 GBPUSD H1. No se usará como coste ejecutable hasta reconciliar
su unidad y fuente.

**Estado:** auditoría offline disponible; P1 sigue abierto porque falta
capturar un snapshot fresco, confirmar su zona horaria/procedencia y resolver
calendario/costes antes de sellar los datos del experimento. Comando:
`cd fastapi && python -m services.market_snapshot_audit --timeframe H1 --timestamp-basis UTC ../resultadosBT/EURUSD_H1_202601020000_202610121900.csv`.
La CLI sale con código 2 si el snapshot necesita revisión de calendario; el
resultado `valid=true` solo certifica formato/precios, no certifica que los
huecos sean correctos ni que el conjunto esté listo para experimentos.

## Avance verificable — P0 (2026-10-22)

Se verificó la contradicción del test de permutación del informe de viabilidad:
el máximo simulado publicado es **+465,11 €**, superior al PnL observado
**+373,88 €**, a la vez que el texto afirmaba que ninguna simulación lo superaba.
Se retiró la afirmación histórica de “99,95 %”; el 98,0 % reciente queda también
pendiente de reproducir. Sin los vectores de resultados crudos no se pueden
reconstruir los p-valores; la conclusión queda inconclusa y requiere re-ejecución
reproducible. P0 sigue abierto hasta fijar la etiqueta/costes y archivar los
resultados de esas pruebas.

## Reglas cuantitativas de evaluación

1. **Edge no es acertar más del 50 % por sí solo.** La dirección debe superar, fuera de muestra, el mejor benchmark congelado y la base condicional del mismo período/régimen; el límite inferior del intervalo de confianza de la mejora pareada debe ser positivo.
2. **La clasificación no basta.** La política ejecutada debe conservar expectancy neta positiva tras spread, comisión, swap y slippage, con límites de riesgo existentes respetados. Un modelo que predice bien pero pierde dinero no pasa.
3. **El tamaño de muestra se calcula**, no se elige después de ver resultados: análisis de potencia con el efecto mínimo relevante, autocorrelación, horizonte solapado y corrección por múltiples experimentos. Los ciclos cada cinco minutos no cuentan como observaciones independientes por defecto.
4. **Dip y cambio se validan por separado.** Etiquetas fijadas sin ver las predicciones; ambas clases deben alcanzar potencia suficiente. Un modelo que siempre compra, siempre vende o siempre hace HOLD no pasa.
5. **PPO solo es un gate obligatorio.** Qwen apagado en el replay no debe cambiar el resultado PPO; el LLM se evalúa también de manera independiente. El conjunto solo se califica si ambos pasan individualmente y no contradicen el mismo caso OOS.
6. **Cualquier conflicto BUY/SELL queda visible y sin orden.** El HOLD por seguridad es un mecanismo de contención, no una forma de maquillar la métrica de alineación.
7. **Los umbrales se pre-registran.** Está prohibido bajar la barra, cambiar horizonte, modificar la muestra, retirar el modelo o anunciar “fiabilidad” para hacer pasar un candidato rechazado.

## Reordenación de trabajos existentes

- **T-28:** conservar su definición y resultados como historial. La regla antigua no es el gate de aceptación de este plan; el gate universal nuevo se implementa tras P2–P3 y no puede omitirse por una ruta especial de promoción.
- **T-30 / reward `v3_edge`:** los resultados previos a 24 h son diagnósticos, no evidencia para H=20. No reentrenar ni añadir features antes de P0–P4.
- **Fase 2 C1–C8:** permanece apagada. El etiquetado y replay pueden reutilizarse solo después de comprobar contrato, reloj, labels y tamaño efectivo; la deliberation shadow no sustituye la evidencia de edge de PPO/LLM.
- **USDCHF `momentum24`:** puede ser benchmark de referencia donde aplique, no prueba del PPO ni sustituto de hacerlo fiable.
- **Promociones automáticas:** ninguna candidata relacionada con este workstream se promueve hasta P8/P9. Este documento no cambia switches ni despliega nada.

## Registro del punto de inflexión

La afirmación de que los modelos deben ser complementarios, responder al mismo objetivo y no cancelarse queda registrada aquí como requisito arquitectónico permanente del workstream. El éxito se acredita con evidencia; no se presume por diseño, un veto o una muestra histórica.

**Tag Git de referencia:** `punto-inflexion-ppo-llm-2026-10-22` apunta al commit base `391ee76` anterior a las modificaciones locales. El tag no contiene este plan ni el auditor P1 porque no están en un commit; no se mueve ni reescribe el tag.

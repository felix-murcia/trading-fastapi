# Punto de inflexión — PPO y LLM alineados con edge verificable (2026-10-22)

Estado: **plan de investigación y backlog vigente; NO es una autorización para cambiar producción, activar criterios, entrenar/promover un bundle ni abrir operaciones.**

Este punto replantea el orden del trabajo iniciado con T-31 y T-28. Sus resultados y pre-registros históricos se conservan; no se reescriben ni se presentan como evidencia para el horizonte nuevo. Este plan pasa a ser la fuente de verdad del backlog PPO/LLM hasta que cada gate se cierre con evidencia.

## Objetivo no negociable

PPO y LLM deben evaluar **la misma pregunta direccional, el mismo horizonte de 20 horas transcurridas y la misma información de mercado disponible en el mismo instante**. Cada modelo debe demostrar edge por separado y el sistema conjunto debe conservarlo. El LLM no puede rescatar una dirección PPO que no sea fiable por sí sola, ni el PPO puede invalidar una señal LLM con autoridad no demostrada.

La operación con una dirección contradictoria no es una fusión válida: cualquier BUY/SELL opuesto se registra como fallo de alineación y no produce una orden. Ese HOLD defensivo evita ejecutar la contradicción; **no** cuenta como solución ni como aprobación de los modelos. No se puede ocultar el desacuerdo con un veto, una media o una etiqueta neutral.

El objetivo es eliminar el ruido evitable de datos, relojes, etiquetas y evaluación. No existe una garantía honesta de “cero ruido” de mercado ni de fiabilidad absoluta futura; la salida a producción requiere evidencia reproducible, límites explícitos y no-go automático cuando falte.

## Estado ejecutivo vigente — corte 2026-10-23 19:40 UTC

La clasificación de gaps se amplió posteriormente con una comprobación de
30/30 comparaciones; esta evidencia no representa una nueva captura de mercado.

Esta tabla es la **fuente de estado actual**. Los apartados posteriores titulados
«Avance verificable» son bitácora con su propio timestamp; sus cifras no deben
interpretarse como el último estado si existe un corte posterior aquí. “Hecho”
significa implementación/evidencia disponible, no aprobación de gate.

| Gate | Estado | Hecho verificable | Falta para cerrar |
|---|---|---|---|
| **P0 — Protocolo y evidencia** | **PARCIAL / NO-GO** | Horizonte objetivo H=20h, universo declarado y criterios estadísticos documentados; se conservó como inconcluso el test de permutación sin vectores crudos. Migración aditiva aplicada y backend desplegado. | Versionar y revisar el protocolo completo con hashes de bundle/VecNormalize/código/LLM y costes válidos; evidencia cruda reproducible del test de permutación o excluirlo formalmente del gate. |
| **P0.1 — Inventario** | **PARCIAL** | Se conservaron snapshots, hashes, auditorías y cortes etiquetados por fecha; existe un reporte read-only de enlaces broker/DB. | Consolidar inventario con fuente, zona horaria, definición, hash y uso permitido para cada artefacto/claim; cero claims no reproducibles usados como criterio. |
| **P0.2 — Ledger, órdenes, deals, PnL y capital** | **ABIERTO / BLOQUEANTE** | FK `trade_outcomes.order_db_id → orders.id` aplicada. Corte 17:38:49Z: clasificación determinista con estado/razón para el 100% de 7.202 ciclos, 91 órdenes, 75 fills y 210 outcomes (0 filas sin clasificación/razón); 607 ciclos, 90 órdenes y 210 outcomes siguen unresolved. 75/75 fills enlazan a `orders.id` exacto. Auditor broker 17:30Z: 121 outcomes sin deal/posición, 88 tickets exactos, 1 sin coincidencia, 0 `order_db_id` y 0 IDs MT5 válidos. | Validar v14.3 con un fill natural posterior al despliegue y enlace exacto; diagnosticar `history/orders` HTTP 500; explicar filas unresolved y conciliar PnL/capital con documentación de balance y movimientos. Sin backfill heurístico. |
| **P0.3 — Pre-registro** | **PARCIAL** | Decisiones del protocolo anotadas: H=20, splits temporales, efecto mínimo, potencia objetivo, métricas y regla de parada. | Revisión independiente y sellado de versión/hash; resolver cobertura de costes, elegibilidad, calendario y holdout nuevo antes de cualquier evaluación. |
| **P1.1 — Mercado H1** | **PARCIAL / NO-GO PARA REPLAY** | Captura MT5 read-only 19:40:24–19:40:25Z en los 3 pares; offset broker detectado +3h. 1.200 barras recibidas y 1.199 cerradas por símbolo; auditor estructural válido. Los 10 gaps semanales por par (30/30 símbolo-intervalos) convierten a cierre viernes 17:00 y reapertura domingo 17:00 New York, consistente con la convención MetaQuotes-Demo aportada por Félix. | La muestra solo cubre EDT y no observa la transición de otoño del 1-11-2026. Hay discrepancia sin resolver: esta convención implica cambio con DST de EE. UU. (1-11), mientras AGENTS.md documenta +2h desde el 25-10. No elegir ni codificar una fecha por suposición: confirmar el offset real con observaciones broker posteriores a ambas transiciones. Para replay, capturar dentro de ≤5 min del inicio y confirmar última vela cerrada; esta captura ya es stale y no se usó. El spread es inválido para costes (1200/1200 ceros EURUSD y GBPUSD; 1177/1200 USDCHF). |
| **P1.2 — Costes de ejecución** | **NO-GO** | `commission`, `swap`, `fee` aparecen en deals históricos consultados; 12 cierres tienen discrepancia registrada de swap. `spread` del feed H1 no es utilizable. | Fuente verificable de bid/ask/spread y slippage para el instante de entrada/fill; cobertura y conciliación completas según criterios más abajo. |
| **P1.3 — Muestra y lineage** | **NO-GO** | Auditor de manifiestos detectó que el CSV P3.1 no es válido. | Regenerar desde fuentes reconciliadas; no reutilizar el CSV fallido. Cero IDs duplicados, discordancias de símbolo, timestamps inválidos o labels inmaduras en muestra aceptada. |
| **P2.1 — Entrada común PPO/LLM** | **PENDIENTE / BLOQUEADO POR P1** | Diferencia de contexto PPO/Qwen identificada como riesgo. | Estado canónico y hash por oportunidad; payload reconstruible para ambos con mismo corte temporal, features comunes, pregunta y horizonte; paridad probada. |
| **P3.1 — Labels y benchmarks** | **NO-GO** | Se implementó auditor de manifiestos; el artefacto declarado (6.778 filas) falló: 100% HOLD, solo EURUSD, duplicados y split Train vacío. Se preserva como evidencia fallida. | Nuevo conjunto desde datos P1 aceptados, labels outcome-blind y benchmarks no vacíos sellados antes de leer predicciones. |
| **P3.2 — Potencia** | **BLOQUEADO** | Umbral provisional documentado: `n_eff ≥ 100` por clase direccional/régimen. | Calcular potencia con dependencia temporal/corrección múltiple una vez aprobados P1/P3.1; insuficiencia = inconcluso/NO-GO. |
| **P4.1 — PPO aislado** | **BLOQUEADO** | Bundle actual y fallo exploratorio n=16 constan en historial. | Un solo bundle/VecNormalize identificado; holdout nuevo sellado; medir skill y expectancy neta por clase/régimen sin Qwen ni tuning. |
| **P5.1 — Experimento PPO** | **BLOQUEADO** | Solo está definido el objetivo experimental general. | Depende de P4.1 y revisión estadística; elaborar protocolo causal de un cambio por experimento. No entrenar/promover dentro de esta tarea. |
| **P6.1 — LLM aislado** | **BLOQUEADO** | La política de paridad está definida, pero no existe evaluación H=20 comparable y sellada. | Mismo dataset, entrada/horizonte, costes y splits que PPO; medir skill, calibración y expectancy neta independiente. |
| **P7.1–P7.2 — Alineación y gate universal** | **BLOQUEADO** | Se exige registrar contradicciones; HOLD defensivo no cuenta como edge/alineación. | Replay emparejado OOS, cero contradicciones BUY/SELL en muestra aceptada, cero órdenes en contradicción y auditoría de todas las rutas de promoción. |
| **P8.1–P8.3 — Estadística, robustez y riesgo** | **BLOQUEADO** | Criterios marco definidos en el backlog. | IC/potencia independientes, robustez por tiempo/régimen/par y exposición correlacionada dentro de límites; cada modelo supera el gate por separado. |
| **P9.1–P9.2 — Shadow/promoción** | **NO AUTORIZADO** | El roadmap declara que no habilita cambios de autoridad. | Solo iniciar shadow tras aprobación explícita de todos los gates P0–P8 y plan de logging/rollback; promoción manual separada. |
| **Operación EA v14.3** | **DESPLEGADO; identidad NO VERIFICADA** | Félix confirmó compilación/despliegue en terminal vivo; log: inicio a `2026-10-23 18:57:45.812` (zona horaria no indicada). Auditor amplio posterior consultó 3 deals USDCHF y encontró 0 comentarios exactos `AIQ:<id>`. | Esperar un fill natural posterior al inicio, confirmar su hora/offset y verificar comentario, ticket MT5, `order_db_id` y resultado por identidad exacta. No generar trades de prueba. |

### Siguiente secuencia de ejecución y criterios de salida

Orden de trabajo; un bloqueo no se marca resuelto por ausencia de datos:

| Orden | Tarea acotada | Criterio de aceptación medible | Bloqueo/decisión |
|---|---|---|---|
| **1** | **P0.2a — Verificar un ciclo vivo v14.3**: observar el siguiente fill natural del EA; seguir la misma orden por respuesta `/orders/prepare`, fill auditado, orden/posición en broker y cierre. | Al menos **1 fill posterior al despliegue** con `AIQ:<order_db_id>` exacto visible en el comentario de orden/posición broker; mismo `orders.id`, `mt5_order_id` y `mt5_position_id` persistidos. Al cerrar: webhook conserva `order_db_id` y `deal_ticket`, y el outcome guarda el mismo FK. La hora debe ser posterior tras conversión documentada del reloj. Ningún trade de prueba ni relación inferida. | Si no ocurre fill, permanecer pendiente; no abrir una operación para obtener evidencia. Si falta el comentario/ID en una etapa, corregir el productor y verificar con un fill natural posterior. |
| **2** | **P0.2b — Diagnosticar `history/orders`**: obtener log del bridge MT5 para las consultas reproducibles EURUSD y USDCHF; identificar excepción/entrada causante y probar la corrección en test. | Endpoint devuelve respuesta 2xx válida para ambos símbolos y rango; esquema/IDs contrastan con `history/deals`; pruebas cubren los fallos encontrados. No registrar payloads sensibles en el roadmap. | HTTP 500 actual para ambos pares; causa todavía desconocida. Mantener P0.2 abierto mientras no haya causa y prueba. |
| **3** | **P0.2c/P1.3 — Cerrar el inventario contable sin heurística**: reejecutar conciliación de ciclos, órdenes, fills, outcomes y deals; separar filas exactas de legacy irrecuperable y explicar duplicados, faltantes y parcialidades. Obtener balance inicial y extractos de depósitos/retiros desde fuente verificable. | **100%** de filas del universo tienen estado y razón deterministas; **0 joins temporales**; filas conciliables enlazadas por IDs exactos; legacy irrecuperable excluido explícitamente de métricas; capital/PnL en EUR con diferencia no explicada **≤ €0,01** (o tolerancia distinta pre-registrada y justificada antes de calcular). | No convertir los 210 outcomes actuales en una sola muestra reconciliada. El filtro horario del broker aún no está validado. |
| **4** | **P1.1 — Crear snapshot H1 aceptable**: capturar EURUSD, GBPUSD y USDCHF desde fuente identificada, normalizar broker/UTC, revisar calendario y clasificar gaps. | Hash + reporte reproducible; duplicados, desorden, OHLC inválido y velas en formación en muestra aceptada = **0**; cada gap está clasificado con calendario/sesión de fuente verificable; captura finaliza ≤ **5 min** antes del replay y la última vela cerrada corresponde al calendario H1, sin stale bars inesperadas. | Los gaps semanales observados quedan explicados con 30/30 comparaciones contra el cierre NY informado. Sigue pendiente corroborar la transición DST de otoño en el servidor MetaQuotes-Demo; snapshot ya stale y no válido para un replay futuro. |
| **5** | **P1.2 — Congelar costes observados**: documentar por símbolo/fill bid, ask, spread, comisión, swap, fee, slippage y moneda/unidad; calcular coste neto por oportunidad elegible. | Cobertura de campos de coste necesarios **100%** en muestra usada; costes convertidos a EUR con fuente/tipo de cambio trazable; desviación entre PnL reconstruido y deal **≤ €0,01** por cierre o tolerancia pre-registrada; cualquier dato ausente excluye la fila con motivo explícito y bloquea potencia/edge si reduce `n_eff`. | El spread H1 es inválido como proxy de ejecución; 12 discrepancias de swap históricas siguen sin explicar. |
| **6** | **P0.3 — Sellar protocolo**: revisión independiente del pre-registro, incluyendo elegibilidad, calendario, costes, benchmark, splits y holdout nuevo. | Un artefacto versionado/hashado, firmado/revisado antes de abrir el holdout; ningún umbral se cambia después de ver predicciones. | Depende de cerrar decisiones de P1.1/P1.2 y fijar el nuevo conjunto no contaminado. |
| **7** | **P1.3/P2.1/P3.1 — Construir dataset común y sellarlo**; reemplaza, no repara, el P3.1 fallido. | Cada oportunidad tiene ID único, símbolo correcto, timestamp monotónico, features as-of sin fuga, estado de ejecución determinista y label H=20 madura; checks bloqueantes del auditor = **0**; benchmarks por estrato no vacíos; manifiesto incluye hashes de todos los inputs/outputs y se sella antes de ejecutar modelos. | Requiere pasos 1–6; no consultar el holdout ni usar outputs PPO/LLM al formar labels. |
| **8** | **P3.2→P4.1/P6.1/P7/P8**: potencia y evaluación independiente, después alineación/robustez. | `n_eff ≥ 100` por clase requerida o mínimo mayor que determine potencia pre-registrada; para cada modelo, LB IC 95% skill vs mejor benchmark >0 **y** LB IC 95% expectancy neta >0; cero contradicciones BUY/SELL OOS en aceptación; robustez por par/régimen y riesgo dentro de límites. | Cualquier insuficiencia = inconcluso/NO-GO; no se retunea ni reusa el mismo holdout. |
| **9** | **P9 — Solicitar decisión separada de shadow/promoción**. | Todos los gates P0–P8 aprobados con evidencia vigente, telemetría completa, rollback probado y autorización explícita. | Ninguna ejecución o despliegue de autoridad está autorizada por este roadmap. |

**Definiciones de estado:** `HECHO` = artefacto/acción reproducible disponible;
`PARCIAL` = evidencia incompleta; `PENDIENTE` = no iniciado; `BLOQUEADO` =
depende de otra tarea/fuente externa; `NO-GO` = no cumple un criterio de
aceptación. El código de salida 2 de los auditores significa «reporte emitido,
gate no superado», no fallo de ejecución. No se asignan fechas objetivo.

## Evidencia de partida y límites

- El horizonte canónico documentado es de **24 horas** y el diagnóstico direccional del entorno PPO usa barras ([`measurement.py`](../services/measurement.py), [`trading_env_v2.py`](../ml/trading_env_v2.py)); ninguno demuestra el objetivo nuevo de 20 horas reales. La conversión de “20 horas” a barras no es válida cuando hay cierres y huecos de mercado. Hay que generar una medición H=20 explícita.
- La inspección del contrato actual indica que PPO recibe una secuencia numérica y Qwen recibe también contexto H4/D1, noticias y otros campos ([`ai.py`](../routers/ai.py), [`qwen_client.py`](../services/qwen_client.py)). Por tanto, las trazas históricas no prueban que ambos hayan visto exactamente la misma información.
- El diagnóstico T-26 documenta fallos del bundle (20,25) en 24 casos seleccionados por clase: SELL en 7/24 dips de tendencia alcista y BUY en 19/24 cambios reales ([informe T-26](../../audits/t26-dip-vs-trend-2026-10-17.md)). Es un indicio concreto para investigar, no una muestra suficiente para estimar rendimiento general.
- El informe de viabilidad registra el experimento PPO `v3_edge` rechazado y resultados de +24 h ([informe de viabilidad](../../audits/2026-10-20-informe-viabilidad.md)); no demuestra edge para 20 h. El mismo informe tiene una contradicción aritmética en el test de permutación que debe reconciliarse antes de reutilizar sus conclusiones.
- T-28 quedó pre-registrado con `agreement ≥ 0,60` y `conflict ≤ 0,20` ([historial T-28 en README](../../README.md)), pero hay un camino de promoción documentado que no bloquea por T-28. Ese gate no satisface el requisito nuevo y no es una prueba de edge independiente.
- El replay de estrategia del 2026-10-22 contiene 2050 ciclos, pero informa solo 46 trades nuevos mapeados; C2–C7 y `bars_since_close` siguen pendientes y C1–C8 no están activados ([informe del replay](./2026-10-22-replay-estadistico-completo.md)). No se debe tratar el número de ciclos solapados como el tamaño efectivo de la muestra.
- La afirmación de saturación ~70 %, deriva compradora y fallo dip/cambio se registra como **hipótesis a verificar sobre el bundle, VecNormalize, datos y configuración actuales**, no como hecho reproducido por este plan.

## Backlog priorizado

El backlog conserva los diez gates P0–P9. La tabla resume su orden; el
desglose atómico inmediatamente posterior define la ejecución y la trazabilidad.
Estado de este desglose: **2026-10-23**. El estado vigente y el siguiente orden
ejecutable constan en «Estado ejecutivo vigente»; no se asignan fechas objetivo
ni se presuponen resultados no medidos.

| ID | Prioridad | Trabajo | Dependencias | Salida / gate |
|---|---|---|---|---|
| **P0 — Congelar protocolo y reconciliar evidencia** | P0 | Fijar H=20 horas transcurridas, universo de símbolos, instante de decisión, etiqueta, neutralidad/costes, exclusiones, splits cronológicos y métricas. Reconciliar la contradicción del test de permutación y retirar o corregir cifras no reproducibles. Verificar el hash actual del bundle, VecNormalize, contrato de observación, reward y switches de promoción. | Ninguna | Pre-registro versionado, inventario de claims y versiones, sin cambios en producción. |
| **P1 — Snapshot, ejecución y calidad del dato** | P0 | Construir un snapshot reproducible de velas H1 cerradas con procedencia, reloj de bróker/UTC, duplicados, huecos, frescura, spread/costes y madurez de la etiqueta a H=20. Reconciliar ciclos, fills, operaciones, PnL y capital entre replay, ledger y fuente bróker. No rellenar observaciones inválidas ni tratar una vela en formación como cierre. | P0 | Hash y reporte de calidad/reconciliación; cualquier defecto o diferencia sin explicar invalida la observación y detiene el replay. |
| **P2 — Contrato de entrada común PPO/LLM** | P0 | Definir un estado canónico por símbolo/ciclo, calculado una vez, con timestamp y hash. Derivar de él el tensor PPO y la representación Qwen y auditar los valores que realmente recibe cada uno. Noticias, memoria u otro contexto exclusivo no se puede presentar como “misma información”: queda fuera de esta comparación o pasa a estar disponible de forma equivalente y trazable para ambos. | P1 | Contrato versionado y replay que reconstruye los dos payloads desde el mismo snapshot; misma pregunta, dirección y horizonte. |
| **P3 — Etiquetas, muestra y benchmarks H=20** | P1 | Crear etiquetas no solapadas o tratar explícitamente la dependencia temporal; identificar el retorno a H=20 con endpoint cerrado y regla de tolerancia fijada antes de medir. Definir outcome-blind las clases “dip dentro de tendencia” y “cambio de tendencia confirmado”. Calcular tamaño efectivo/potencia y comparar con azar/base-rate condicional y benchmarks triviales congelados. | P0, P1 | Dataset, cálculo de potencia y benchmarks sellados antes de consultar salidas de modelos; muestra insuficiente = NO-GO, no extrapolación. |
| **P4 — Baseline PPO aislado** | P1 | Evaluar el bundle y VecNormalize exactos en el holdout sellado, sin veto Qwen, reentry, quorum ni selección posterior. Medir cobertura, BUY/SELL/HOLD, skill direccional y política a riesgo fijo neta de costes; desglosar por régimen y las dos clases. Reproducir la saturación/deriva indicada por el usuario. | P2, P3 | Baseline independiente que identifica si el fallo nace en datos, contrato, normalización, reward, acciones o política. Sin tuning sobre el holdout. |
| **P5 — Diseño experimental de PPO para el objetivo real** | P1 | Solo tras P4: pre-registrar una hipótesis causal por experimento para que PPO aprenda dirección a H=20 y distinga retroceso de reversión; asegurar paridad train/serve, VecNormalize y señal de tendencia disponible. No cambiar a la vez reward, features y arquitectura. | P1–P4 | Diseño walk-forward documentado. No se entrena ni modifica bundle hasta superar los gates aplicables y obtener autorización expresa posterior; el holdout consultado no se reutiliza. |
| **P6 — Evaluación independiente del LLM** | P1 | Reproducir Qwen con el mismo contrato, snapshot, horizonte, etiqueta, instante de entrada y exclusiones que PPO; medir su edge solo, calibración, cobertura y economía de la política. Excluir contexto extra no compartido de esta comparación. | P2, P3 | Evidencia propia del LLM; ninguna autoridad se hereda de su track record anterior ni de PPO. |
| **P7 — Alineación y gate universal** | P2 | Replay emparejado por snapshot y oportunidad de entrada/horizonte. Sustituir el uso de T-28 como proxy: cada ruta de promoción debe aplicar el mismo gate, incluida la ruta `drastic_improvement`. Medir contradicciones BUY/SELL, abstenciones, cobertura y edge individual. No hacer del desacuerdo un veto silencioso. | P4–P6 | **Cero contradicciones BUY/SELL en la muestra OOS de aceptación**; cero órdenes en cualquier contradicción de shadow; umbral de muestra/potencia congelado antes de abrir el holdout. Una divergencia invalida la calificación conjunta y reabre diagnóstico. |
| **P8 — Gate estadístico, robustez y riesgo de cartera** | P2 | Estimar intervalos con método que respete autocorrelación y comparaciones múltiples; calcular tamaño efectivo/potencia antes de evaluar. Validar ventanas cronológicas, regímenes y sensibilidad a EURUSD. Recalcular métricas con implementación independiente y validar exposición conjunta/correlacionada entre pares y escenarios de estrés contra límites vigentes. | P3–P7 | Cada modelo por separado: límite inferior del IC del skill frente al mejor benchmark preregistrado > 0; además, límite inferior del IC de expectancy neta a riesgo fijo > 0. PPO debe pasar ambos gates sin LLM. Expectancy negativa, fallo en dip/reversión, dependencia de un régimen/par, riesgo fuera de límites, muestra insuficiente o métrica irreproducible = **NO-GO**, aunque mejore relativamente. |
| **P9 — Shadow y promoción controlada** | P3 | Solo después de los gates: operar en shadow con logging completo, vigilancia de datos/drift y rollback previamente probado. No conceder autoridad automática por subir la muestra ni activar C1–C8 como atajo. | P7, P8 | Promoción manual y explícita solo con gates vigentes; cualquier deterioro o contradicción detiene nuevas entradas y retira la calificación hasta nueva evidencia. |

## Desglose ejecutable y trazabilidad del backlog

**Convención de evidencia:** ningún gate P0–P9 se considera aprobado por tener
un auditor, migración o suite verde. El PnL histórico disponible es descriptivo;
no se presenta como prueba de edge. La observación PPO de
holdout negativa con **n=16** es insuficiente para inferencia concluyente, pero
sí impide promoción: no se puede convertir en un caso de mejora relativa ni
reutilizar como holdout después de haberlo inspeccionado.

| Tarea | Trabajo atómico y criterio de aceptación | Dependencias | Responsable por rol |
|---|---|---|---|
| **P0.1 — Inventariar y preservar evidencia** | Registrar fuentes, fecha de corte, universo, definiciones y hashes disponibles para cada cifra de PnL, replay, modelo y evaluación. Separar explícitamente evidencia descriptiva de inferencial. Toda cifra sin datos crudos, manifiesto y procedimiento reproducible queda marcada «no reproducible / no usar para gate», sin borrar ni reescribir el histórico. Incluir la conclusión del test de permutación como inconclusa hasta recuperar o regenerar sus vectores crudos. | Ninguna | Investigador/a cuantitativo/a + responsable de datos |
| **P0.2 — Reconciliar ledger, fills, capital y PnL** | Conciliar, por símbolo y operación, ciclo/señal → orden → fill parcial/final → cierre → `trade_outcomes`/ledger → deals del bróker; después reconciliar PnL neto, balance/equity y movimientos de capital con base de moneda y reloj explícitos. Explicar el desfase entre **2050 ciclos** de replay y solo **46 operaciones nuevas mapeadas**: clasificación contable completa de ciclos y fills, incluidos no ejecutados, duplicados, parciales, cierres y excluidos. Aceptación: diferencias no explicadas = **0**; cualquier tolerancia y tratamiento de ajustes se documentan antes de calcular rentabilidad. | P0.1 | Responsable de datos/ledger + operaciones de trading; revisión independiente de finanzas/riesgo |
| **P0.3 — Pre-registrar protocolo y claims** | Fijar antes de abrir cualquier evaluación: H=20 transcurridas, momento de decisión, símbolo/universo, reglas de elegibilidad, labels, comparadores, splits y holdout nuevo, ventanas, métricas, efecto mínimo relevante, potencia, IC, corrección por múltiples pruebas, exclusiones y reglas de parada. Anotar hashes del bundle PPO, VecNormalize, código/contrato, configuración, switches de promoción y versión del prompt/modelo LLM disponibles. Aceptación: protocolo versionado, íntegro y revisado por una persona/rol no responsable de ajustar el candidato; ningún umbral se decide mirando el nuevo holdout. | P0.1 | Investigador/a cuantitativo/a; aprobación de riesgo y revisión independiente |
| **P1.1 — Capturar y validar snapshot H1** | Crear snapshot de velas cerradas con procedencia, hash, símbolo, zona/reloj de origen y conversión UTC/bróker; revisar huecos contra calendario, frescura, duplicados, orden, OHLC y madurez H=20. No imputar datos ni inferir zona horaria desde el CSV. Aceptación: reporte reproducible, todas las exclusiones justificadas y cero vela en formación/label inmadura en la muestra aceptada. | P0.3 | Ingeniería/responsable de datos de mercado |
| **P1.2 — Definir fills y costes completos** | Especificar para cada lado y símbolo fuente/unidad/instante de bid-ask y spread, comisión, swap/financiación, slippage, latencia y regla de fill/rechazo/parcial. Distinguir coste observado de proxy conservador; cuantificar cobertura y sesgo de datos faltantes. Spread cero de los exports no se interpreta como coste cero. Aceptación: coste reproducible por oportunidad y trade, reglas congeladas antes del replay; costes no observables o incompletos implican NO-GO económico, no sustitución silenciosa. | P0.2, P1.1 | Especialista de ejecución/operaciones + responsable de datos; revisión cuantitativa |
| **P1.3 — Validar muestra útil y asignación de operaciones** | Reconciliar el universo de ciclos con oportunidades únicas de decisión, órdenes y operaciones realizadas; explicar los 46 trades mapeados y no equiparar ciclos solapados con n. Establecer claves de emparejamiento y un reporte de pérdidas/exclusiones. Aceptación: cada fila tiene estado y motivo determinista; el recuento de cada transición cuadra con P0.2 y el tamaño efectivo se informa separadamente del conteo bruto. | P0.2, P1.1 | Responsable de datos + investigador/a cuantitativo/a |
| **P2.1 — Congelar decisión y contrato común** | Especificar y serializar una sola oportunidad de decisión con timestamp, precio/mercado disponible, símbolo, features comunes y hash. Verificar por replay que PPO y LLM reciben información con el mismo corte temporal, misma entrada y mismo horizonte; registrar payload real, versión, normalización y cualquier campo exclusivo. O se excluyen los campos privados del comparativo o se demuestra paridad causal y temporal para ambos. Aceptación: reconstrucción determinista de ambos payloads desde el mismo snapshot, sin fuga futura ni campos sin procedencia. | P1.1, P1.2, P1.3 | Ingeniería ML + ingeniería de datos; revisión de investigación |
| **P3.1 — Sellar labels y benchmarks** | Definir endpoint a 20 horas transcurridas, tratamiento de mercado cerrado/gaps, regla de retorno/neutralidad y etiquetas outcome-blind para dip/tendencia y cambio confirmado. Congelar benchmarks direccionales y base condicional por régimen con la misma elegibilidad. Aceptación: labels generables sin leer salidas de PPO/LLM y manifiesto sellado antes de evaluarlas. | P0.3, P1.1, P2.1 | Investigador/a cuantitativo/a distinto/a del ajustador del modelo |
| **P3.2 — Potencia y muestra efectiva** | Calcular tamaño efectivo con dependencia temporal, solapamiento H=20, agrupación por episodio/régimen y multiplicidad; definir efecto mínimo y muestra necesaria antes de abrir el holdout. Reportar n bruto, n efectivo, intervalo y potencia por símbolo/régimen/clase. Aceptación: cada corte de aceptación alcanza el mínimo preregistrado; si no, resultado «inconcluso/NO-GO», nunca se compensa con más ciclos correlacionados. | P0.3, P1.3, P3.1 | Bioestadística/estadística cuantitativa independiente |
| **P4.1 — Evaluar PPO aislado y preservar holdout** | Evaluar un único bundle y VecNormalize identificados con Qwen/vetos, reentry, quorum y selección posterior desactivados; igualar punto de entrada y H=20 con el protocolo. Publicar confusión/cobertura/abstención, skill vs benchmarks, expectancy neta, fills/costes, intervalos y breakdown por régimen/par/clase. El resultado previamente negativo, **n=16**, queda como evidencia exploratoria insuficiente y NO-GO; reservar un holdout OOS intacto para una sola evaluación confirmatoria. Aceptación: manifiesto/hash completo y reporte independiente; no hay tuning ni selección sobre holdout. | P2.1, P3.1, P3.2 | Investigador/a cuantitativo/a + ML engineer PPO; revisión independiente |
| **P5.1 — Diseñar experimento PPO, sin entrenar ni promover** | Solo después de revisar P4.1, formular una hipótesis causal y un cambio aislado por experimento (no mezclar reward, features y arquitectura); documentar paridad entrenamiento/serving, VecNormalize, señal de tendencia, objetivo H=20, seeds, folds y criterios de descarte. No ejecutar entrenamiento ni producir un bundle en este backlog; eso requiere gates aplicables superados y una autorización posterior explícita. Aceptación: plan preregistrado y revisable, sin tocar bundle activo, autoridad ni switches de producción. | P4.1, P8.1 (criterios y diseño estadístico) | ML engineer PPO + investigador/a cuantitativo/a; autorización de riesgo |
| **P6.1 — Medir LLM por separado** | Evaluar versión exacta de Qwen/prompt/modelo con la misma fila de oportunidad, entrada, H=20, label, exclusiones, costes y folds que el PPO; registrar salida cruda, dirección, abstención, latencia y versión. Medir calibración, cobertura, skill y expectancy neta independientemente. Aceptación: protocolo idéntico en las dimensiones comunes y reporte reproducible; evidencia previa no sustituye este test. | P2.1, P3.1, P3.2, P1.2 | Investigador/a cuantitativo/a + responsable de integración LLM; revisión independiente |
| **P7.1 — Reproducir replay emparejado y alineación** | Construir la tabla de comparación una fila por oportunidad común, sin desplazar señales entre ciclos ni mezclar entradas/horizontes. Contabilizar por separado acuerdos, contradicciones, abstenciones, no-ejecutables y cambios de fill; demostrar que el HOLD de seguridad ante conflicto no mejora artificialmente la métrica. Aceptación: conteos y resultados reconcilian con P1.3; ninguna contradicción BUY/SELL produce una orden en replay/shadow. | P4.1, P6.1 | Ingeniería de datos/replay + investigación cuantitativa |
| **P7.2 — Aplicar gate a toda ruta de promoción** | Auditar todas las rutas automáticas/manuales, incluida `drastic_improvement`, y definir un gate único que no acepte una mejora relativa si el candidato falla la expectancy neta absoluta o cualquier requisito P8. Aceptación: cobertura trazable de cada ruta de decisión; cualquier ruta que permita eludir los gates es NO-GO y no se activa. No se cambian switches como parte de esta tarea de investigación. | P0.3, P7.1, P8.1 | Ingeniería ML/plataforma + responsable de gobernanza de modelos |
| **P8.1 — Aprobar reglas estadísticas y anti-sobreajuste** | Congelar walk-forward cronológico con separación de desarrollo/validación/holdout, corrección de pruebas múltiples y política de una sola consulta confirmatoria al holdout. Toda selección/tuning queda confinada a desarrollo; un rechazo no autoriza cambiar umbral y volver a consultar el mismo holdout. Aceptación por modelo: límite inferior del IC de mejora de skill frente al mejor benchmark >0 **y** límite inferior del IC de expectancy neta a riesgo fijo >0, con potencia suficiente y reporte independiente; los criterios de IC/potencia quedan registrados antes de medir. | P0.3, P3.2, P4.1, P6.1 | Estadístico/a independiente + responsable de riesgo |
| **P8.2 — Probar robustez temporal, régimen y concentración** | Repetir evaluación en ventanas cronológicas y regímenes definidos sin mirar predicciones; informar contribución de EURUSD frente a otros símbolos y concentración por símbolo, sesión y período. Hacer sensibilidad preregistrada a costes, exclusiones y régimen, sin retocar el modelo sobre ventanas OOS. Aceptación: cada estrato necesario está suficientemente potenciado y supera los criterios preregistrados; edge concentrado en una sola ventana/régimen/par o deterioro bajo costes plausibles = NO-GO/inconcluso según protocolo. | P8.1, P7.1 | Investigador/a cuantitativo/a independiente |
| **P8.3 — Validar riesgo y exposición correlacionada** | Medir simultaneidad de posiciones y exposición por par/divisa/factor; estimar correlación con datos alineados y escenarios de estrés, incluyendo concentración compartida aunque los pares sean distintos. Contrastar el portafolio combinado con límites de riesgo ya vigentes; documentar metodología, ventanas y datos ausentes. Aceptación: no hay escenario/medición fuera de los límites vigentes ni exposición sin agregación; si faltan datos o el modelo de correlación no es estable, NO-GO, no se declara diversificación por símbolo. | P0.2, P1.2, P8.1 | Responsable de riesgo de cartera + operaciones; revisión independiente |
| **P9.1 — Abrir shadow únicamente tras aprobación de gates** | Tras P0–P8 aprobados, solicitar decisión explícita de paso a shadow. Definir telemetría de snapshot/payload/decisión/orden/fill/coste, control de drift, límite y rollback antes de iniciar. Aceptación: ninguna orden con autoridad del candidato; todas las señales se registran y toda contradicción impide orden. | P7.1, P7.2, P8.1–P8.3 | Operaciones + responsable de release/riesgo |
| **P9.2 — Revisar shadow y decidir promoción manual** | Comparar shadow con el protocolo congelado, auditar completitud/reconciliación, drift, economía neta y riesgo; documentar aprobación o NO-GO con evidencia. Aceptación: autorización manual separada, todos los gates aún vigentes y sin discrepancias abiertas; cualquier regresión/contradicción detiene entradas y retira calificación. Este plan no autoriza esa decisión ni promoción automática. | P9.1 | Responsable de riesgo/release + revisor/a independiente |

### Gates de no-go y riesgos de ejecución

- **NO-GO inmediato para promoción o autoridad:** PnL solo descriptivo o sin
  ledger/capital reconciliados; test de permutación sin datos crudos reproducibles;
  fills/costes incompletos; discrepancia de mapeo ciclos–operaciones sin resolver;
  oportunidad de entrada u horizonte no homogéneos; holdout consultado o
  muestra efectiva/potencia insuficiente; expectancy neta no positiva; skill sin
  ventaja estadística frente al benchmark; dependencia no explicada de régimen
  o EURUSD; exposición correlacionada no medida/fuera de límites; contradicción
  BUY/SELL ejecutable; o cualquier ruta de promoción que eluda gates.
- **No confundir fallo de evidencia con prueba de ausencia:** n efectivo pequeño
  (incluido el holdout PPO negativo n=16) no estima con precisión el edge
  verdadero. Sí basta para mantener NO-GO y obliga a diseñar evaluación nueva,
  pre-registrada y no contaminada.
- **Riesgos principales:** sesgo por selección/supervivencia; fugas temporales o
  labels inmaduras; sobreconteo por señales solapadas; concentración de
  EURUSD/períodos; ejecución teórica distinta de fills reales; coste cero
  aparente; capital o PnL mal reconciliado; cambio de régimen; y falsos
  positivos por probar muchas variantes. Cada riesgo debe tener evidencia de
  control o bloquear el gate; no se compensa con una mejora relativa.
- **Separación de fases obligatoria:** P0–P4 y P6–P8 son investigación y
  medición. P5 es diseño de experimento y no autoriza reentrenar el bundle
  activo. P9 es solo shadow después de aprobación formal de todos los gates.
  Hasta entonces no se promueve, no se altera la autoridad del sistema, no se
  activan C1–C8 ni se cambia ningún switch de producción como parte de este
  roadmap.

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

## Avance verificable — P0.2 (2026-10-23): Reconciliación de evidencia y estado de datos

Hallazgos críticos sobre la compatibilidad entre replay histórico y estado actual:

1. **Mismatch de `cycle_id` entre replay CSV y audit_log actual**:
   - Replay CSV (`doc/replay-cycles.csv`): 2050 filas, formato `cycle_id = ai_predict_EURUSD_1788375596` (export anterior, era 2026-09-02)
   - Audit_log actual: 7058 ciclos `ai_predict_cycle`, `cycle_id` en formatos `EURUSD_1792514700000` (nuevo) y `trace_90f2db52-69be-45` (UUID truncado, versión antigua)
   - **Resultado: cero superposición** entre los `cycle_id` del replay y los del audit_log actual. El replay CSV es un export estático anterior a las migraciones recientes de esquema.

2. **Falta de columna `symbol` en replay CSV**:
   - Aunque los `cycle_id` coincidieran, `replay-cycles.csv` no tiene columna `symbol`.
   - El cálculo de `bars_since_close` requiere unir `symbol` desde `audit_log` para consultar el último `filled_at` en `orders` por símbolo.

3. **Imposibilidad de calcular `bars_since_close` desde el CSV actual**:
   - No hay match de `cycle_id` entre replay (2050 rows) y audit_log.
   - Incluso si hubiera match, falta `symbol` en el CSV.
   - **Conclusión: `bars_since_close` no se puede computar del replay CSV actual; se requiere un run fresco del replay contra `audit_log` + `orders` en vivo.**

4. **`rejected_position_open = 0` confirmado** (P0.2 reconciliado):
   - `audit_log`: `data ? 'veto_reason'` = 0 filas para `event = 'ai_predict_cycle'`.
   - CSV replay: columnas `veto` y `veto_reason` vacías.
   - Órdenes `orders`: 89 totales / 89 `cycle_id` únicos / 87 `filled`.
   - `trade_outcomes`: 208 rows / PnL +356.06 € / 85 wins / 73 losses.
   - 42/45 `trade_id` del replay presentes en `trade_outcomes.id` (3 eliminados en higiene P2: IDs 76–79, 107–111; backups en `backups/higiene_p2_*.sql`).
   - Métricas reconciliadas de esos 42 trades: n=42, total PnL +14.94 €, avg +0.36 €, 13 wins ≥3 €, 19 losses.

5. **Estado de los criterios C1–C8**:
   - Siguen **OFF** (`STRATEGIST_VERSION=1.0.0`, config OFF).
   - No se activan sin `0 rejected_position_open` verificado + evidencia P0.3 pre-registrada.
   - Suite 1291 passed / 2 xfail confirmado (2026-10-23).

6. **Shadow ventana §6** (decisión pendiente Félix):
   - Abierta desde 2026-09-30 19:00 UTC; corte 7 días = 10-07 19:00 UTC.
   - 27/50 candidatas (regla "≥50" no se cumple); agujero 62h por EA 4014.
   - Opciones: (a) aceptar evidencia con 62h hole + C2 solo ON, (b) extender ventana a 30 días, (c) iterar P1 backlog (time-stop/breakeven, cooldown re-entrada).

## P0.3 — PRE-REGISTRO DEL PROTOCOLO (autor Félix: opcion C, 2026-10-23)

**Decisión Félix:** Opción **(c) iterar P1 backlog** (time-stop/breakeven, cooldown
re-entrada). No se extenderá la ventana shadow a 30 días ni se aceptará el
agujero 62h con C2 solo ON.

**Elementos congelados antes de abrir evaluación (read-only, no cambian producción):**

| Elemento | Estado congelado | Justificación |
|---|---|---|
| **Horizonte H** | 20h transcurridas (velas H1 cerradas) | Objetivo no negociable (línea 9) |
| **Universo** | EURUSD, GBPUSD, USDCHF (H1 completo) | Datos disponibles, spreads medidos |
| **Instante decisión** | Cierre vela H1 (`mt5_time` del ciclo) | Alineado con EMA M5 del EA |
| **Label outcome** | Retorno neto a H=20 (spread/comisión/swap/slippage incluidos) | Costes reales, T-31 `production_stops` |
| **Neutralidad** | `retorno ∈ [-costes, +costes]` → HOLD | Evita falsos edge |
| **Exclusiones** | News blackout (30m pre/15m post), spread > p95, candle en formación | Evita colguras |
| **Splits** | Train: 2026-01-01→07-31 \| Val: 08-01→09-30 \| Holdout: 10-01→ahora | Walk-forward chronológico |
| **Métricas primarias** | IC direccional vs benchmark congelado + expectancy neta (riesgo fijo 1%/trade) | Edge estadístico + económico |
| **Efecto mínimo** | Δ skill ≥ 0,02 vs benchmark; expectancy neta > 0 (IC 95% LB > 0) | E1c: efecto relevante |
| **Potencia** | n_eff ≥ 100 decisiones direccionales/clase | E1c: potencia 80%+ |
| **Corr. múltiple** | Holm-Bonferroni por régimen × par | Control FWER |
| **Regla parada** | 3 NO-GO consecutivos → bloqueo definitivo | Anti-bucle P8.1 |
| **Bundle PPO** | `bb70b582` (obs 20×25, Box(4,), VecNormalize) | Único bundle a evaluar |
| **VecNormalize** | Cargado antes `predict()` (checklist P2.1) | Contract fidelity |
| **Qwen** | Versión/prompt/modelo congelado (shadow OFF) | Comparabilidad |
| **Switches promo** | `drastic_improvement`, C1–C8, auto-retrain: **OFF** | No evalución hasta P8/P9 |

**Acciones ejecutadas (2026-10-23):**
- ✅ Replay fresco ejecutado contra `audit_log` + `orders` actual (no sobre CSV stale).
- ✅ Archivo generado: `fastapi/ops/replay-cycles-etiquetado-fresco.csv` (6778 ciclos con datos actuales).
- ✅ `bars_since_close` calculado para cada ciclo (mt5_time ↔ último fill UTC, offset broker +3h detectado T-31.9).
- ✅ `rejected_position_open = 0` confirmado: todos los rows reportan `"0"`.

**Próximos pasos (P1 backlog — opción C — COMPLETADO 2026-10-23):**
- ✅ `time-stop`: implementado en replay fresco (`HOLD_MAX_BARS=12`). 29/6778 ciclos (0.4%) triggerian time-stop al exceder 12 velas sin SL/TP.
- ✅ `breakeven`: heurística aplicada (regla ≥3€). 0/6778 en CSV puro (replay raw) pero lista para validación contra `trade_outcomes` PnL real.
- ✅ `cooldown_reentry`: implementado con umbral 6h (6 barreras H1). 6634/6778 (97.9%) ciclos permiten re-entrada.
- ✅ CSV re-etiquetado: `fastapi/ops/replay-cycles-etiquetado-fresco-p1.csv` con nuevas columnas `time_stop_triggered`, `breakeven_triggered`, `cooldown_reentry_allowed`, `p1_nota`.
- Próximo gate: P3.1 (sellar labels y benchmarks H=20) usando este dataset P1 enriquecido.

**Estado P1 backlog (revisión 2026-10-23):** el cierre comunicado bajo opción C queda
reabierto. La auditoría posterior del artefacto P3.1 encontró que no representa el
universo declarado y no permite evaluar labels, benchmarks ni muestra efectiva.
El CSV de replay no sustituye el snapshot de velas/costes exigido por P1.1/P1.2;
P1.1–P1.3 no se consideran cerradas hasta generar y reconciliar artefactos que
pasen los controles reproducibles descritos abajo.

**Corrección del registro P0.3:** la afirmación anterior de que las 6.778 filas
tenían `bars_since_close` calculado no supera la auditoría: 6.463 valores son
negativos y el CSV contiene 102 IDs duplicados y 619 discordancias de símbolo.
El cero registrado para `rejected_position_open` solo describe el campo del
CSV; no demuestra que el emparejamiento de ciclos sea correcto.

**Actualización P0.2 (consulta SQL read-only, 2026-10-23):** la BD devuelve
7.098 `ai_predict_cycle`, 88 órdenes `filled` (más 1 `pending` y 1 `cancelled`)
y 209 `trade_outcomes` con PnL agregado **+347,21 €**. Estos conteos difieren
del corte anterior escrito en este documento (7.058/89/208 y +356,06 €) y no
se han conciliado entre sí. El máximo `trade_outcomes.exit_time` aparece como
2026-10-23 15:46:11+00, posterior al reloj host 2026-10-23 13:21 UTC; por ello
no se debe tratar ese timestamp como UTC real sin aplicar y verificar el
dominio de reloj del bróker. P0.2 queda abierto; los cortes históricos se
conservan, no se combinan como una sola medición.

## P3.1 — SELLADO DE DATOS Y BENCHMARKS (2026-10-23)

**Protocolo:** congelado; **dataset/benchmarks:** congelación invalidada por la
auditoría de integridad del 2026-10-23. No modificar el CSV ni el manifiesto
originales: conservarlos como evidencia del intento fallido.

- Artefacto declarado: `replay-cycles-etiquetado-fresco-p1.csv` (6.778 filas).
- Manifiesto declarado: `fastapi/ops/P3.1_manifest.json` (v1.0), pero sin hash
  del dataset y con todos los benchmarks en `n=0`.
- La distribución de decisiones coincide con el manifiesto, pero las 6.778
  filas son HOLD; no hay una sola etiqueta direccional.
- El CSV contiene solo EURUSD pese al universo declarado EURUSD/GBPUSD/USDCHF;
  tiene 102 `cycle_id` duplicados, 619 discrepancias entre símbolo e ID y 6.463
  valores negativos de `bars_since_close`.
- La cobertura cronológica efectiva es Train=0, Val=1.799 y Holdout=4.979. La
  muestra no contiene el período Train declarado.
- No contiene columna `label`; no permite estimar muestra direccional ni pasar
  a P3.2.

**Estado:** P3.1 **NO-GO**. P3.2 bloqueado hasta regenerar los datos de origen,
corregir la asignación temporal/simbólica y producir un nuevo artefacto con
labels y benchmarks calculados. La auditoría no repara ni sobrescribe los
artefactos congelados.

**Próximos pasos (P3.2 — Potencia), solo tras superar la auditoría:**
- Calcular n_eff por clase direccional y por régimen (dip/cambio)
- Alcanzar n_eff ≥ 100 decisiones por clase (criterio P3.2)
- NO-GO si n_eff insuficiente: regresar a P1 backlog o rediseñar evaluación

## Avance verificable — auditor de manifiestos de replay (2026-10-23)

Se añadió `services/replay_manifest_audit.py`, una herramienta offline que
comprueba la integridad entre manifiesto y CSV, universo/símbolos, distribución
de acciones, unicidad y coherencia de IDs, timestamps, `bars_since_close`,
labels, cobertura de splits y muestras de benchmarks. Falla con código 2 si
cualquier gate bloqueante no se cumple. No escribe datos ni se conecta a
producción.

Resultado sobre `ops/P3.1_manifest.json`: **NO-GO**, con los defectos arriba
detallados. SHA-256 observados: manifiesto
`1a3d4c3aca04d9654b8bd35cf9c511d151b3ffe684fce4e47cf6dbc2e1aa7953`; dataset
`5d61ecc16e87b16e22d26a39fa5a54fc643292de0f544b2a075a05cf4c68f0a2`. El hash
del dataset no está fijado dentro del manifiesto.

Prueba:
`cd fastapi && python -m services.replay_manifest_audit ops/P3.1_manifest.json`.
Suite focalizada: auditor de manifiesto + snapshot + reglas de arquitectura,
**18 passed**. Regresión en
`tests/test_replay_manifest_audit.py`. Ningún cambio de modelo, switch,
autoridad, datos de producción o ejecución de órdenes.

## Avance verificable — reconciliación read-only P0.2 (2026-10-23)

Se añadió `ops/reconcile_replay_sources.py`. Lee ciclos, decisiones, órdenes,
fills, resultados y snapshots de equity en una transacción PostgreSQL
`REPEATABLE READ READ ONLY`; no modifica la base de datos. El estado
`UNRECONCILED` produce código 2 intencionalmente.

Snapshot UTC `2026-10-23T13:29:58.253730+00:00`: **P0.2 sigue NO-GO**.

- `ai_predict_cycle`: 7.101 filas / 6.988 IDs únicos; 113 filas duplicadas,
  12 IDs con decisiones contradictorias, 306 sin `mt5_time` y 1 símbolo fuera
  del universo congelado.
- Órdenes: 90; 75 encuentran ciclo, 15 no, 2 se enlazan a un ciclo repetido;
  88 órdenes `filled` no tienen `mt5_order_id`.
- `trade_outcomes`: 209 y PnL registrado de +347,2100; 121 carecen de
  `deal_ticket` y 121 carecen de `position_id` (el reporte no asume que sean
  las mismas filas). El esquema no representa la relación
  orden→resultado: `orders.mt5_order_id` y `trade_outcomes.position_id`/
  `deal_ticket` son identificadores distintos y no hay una FK compartida.
  El PnL es descriptivo, no una conciliación financiera.
- Los 74 eventos `order_filled` tienen `order_db_id`.
- Equity: 1.761 snapshots; último balance 508,94 y equity 513,26. Sin balance
  inicial verificado ni extractos de depósitos/retiros, el PnL no se compara
  con la variación de balance/equity.

Este corte es distinto de los inventarios anteriores y no se mezcla con ellos.
No se usa el emparejamiento temporal exploratorio como clave de reconciliación.
La suite focalizada (reconciliación, auditor de manifiesto, snapshot y reglas de
arquitectura) da **21 passed**. P0.2 no se cierra; P3.1 continúa NO-GO y P3.2
sigue bloqueado. No se cambió producción, autoridad, modelo, switches ni
órdenes.

## Avance de implementación — enlace exacto orden→cierre (2026-10-23)

Se preparó un cambio de código para sustituir el enlace heurístico por identidad
explícita:

- `trade_outcomes.order_db_id` es una FK nullable a `orders.id`; la migración
  [20261023_trade_outcome_order_link.sql](../db/migrations/20261023_trade_outcome_order_link.sql)
  también añade `orders.mt5_position_id`. No hay backfill histórico ni joins
  por tiempo/símbolo.
- El EA v14.2 requiere `order_db_id` en la respuesta de `/orders/prepare`, lo
  conserva en el comentario de la orden (`AIQ:<id>`) y lo transmite al cierre,
  junto con el ticket de apertura y el `position_id`.
- El backend solo registra el enlace si la orden existe y coincide en símbolo
  y dirección. Si no coincide, conserva el resultado sin FK y emite error
  explícito. Un `deal_ticket` ya existente con otro enlace responde HTTP 409;
  el estado anterior no se sobrescribe.
- `mark_order_filled` actualiza la fila por `orders.id` cuando se recibe el ID
  exacto y persiste los identificadores MT5. Se conserva temporalmente la ruta
  de EA previo a v14.2 por compatibilidad; queda marcada en log como legacy y
  no certifica reconciliación.

**Estado de ejecución (2026-10-23):** la migración se aplicó a la BD activa
después de crear y validar un backup `pg_dump`. La FK está validada; la columna
es nullable y **no hubo backfill**. El backend actualizado se construyó y
desplegó; `/health` respondió 200 y el arranque confirmó conexión a PostgreSQL y
MT5, además de cargar PPO y VecNormalize. El EA v14.3 sigue solo en el árbol
local en ese corte: el host no dispone de compilador MQL5 y el broker aún no
había verificado la persistencia del comentario `AIQ:<id>`. La actualización de
despliegue posterior se documenta más abajo.

La suite completa dio **1308 passed, 2 xfailed**. Reconciliación read-only tras
la migración: `UNRECONCILED` / `INCOMPLETE`; **0/209 outcomes enlazados y
209/209 sin `order_db_id`**, más los huecos pendientes de ciclos y órdenes.
No se asignaron relaciones históricas por heurística. Por tanto la migración y
el despliegue del backend no cierran P0.2 ni autorizan el gate económico.
La última fila de `equity_snapshots` es `2026-10-23T10:57:27Z`, mientras que
el endpoint de salud consultó equity `504.89` a las `13:57Z`; al no ser cortes
temporales coincidentes, no se comparan como reconciliación de capital.

## Avance — robustez del outbox EA (2026-10-23)

La fuente v14.3 mueve los conflictos HTTP 409 permanentes del outbox activo a
`trade_filled_conflicts.jsonl` solo si pudo persistirlos; si falla, el cierre
permanece pendiente. El límite de flush ahora acota **intentos HTTP** (máximo
10, no solo respuestas exitosas). Para reescribir pendientes, primero escribe
un archivo temporal y solo después intenta reemplazar el original con
`FileMove(..., FILE_REWRITE)`; si el reemplazo falla, conserva el original.
La API de `FileMove` se contrastó con la documentación oficial de MQL5. Las
pruebas de contrato no sustituyen la compilación ni el replay en MT5. El usuario
confirmó posteriormente el despliegue de v14.3; la comprobación broker figura
más abajo.

## Avance parcial — P1.1, snapshot H1 y calidad del feed (2026-10-23)

Se capturó en modo read-only el feed H1 de EURUSD y USDCHF a
`2026-10-23T14:41:06Z`. El offset observado en el timestamp del servidor fue
**+3 h**; se trató el sello de las velas como reloj de bróker aun cuando el
payload lo etiqueta con `Z`. Se conservaron los payloads brutos y hashes en el
artefacto local no versionado `h1-market-snapshot-20261023-latest.json`
(SHA-256 del artefacto:
`2ff207824e8020c6b4c79892eb4326d69fcd3aed2b08c192cdb6a75d18c0d7e4`).
De 1.200 velas por símbolo, 1.199 estaban cerradas según la regla aplicada
`open_UTC + 1h <= captured_at_UTC`; la última vela cerrada común tenía apertura
`13:00Z` y cerró a `14:00Z` (41 minutos antes de la captura). No hubo duplicados
ni OHLC inválido. Los 20 intervalos >1 h (10 por símbolo, todos de 49 h) son
repeticiones de viernes 23:00 a lunes 00:00 en los sellos brutos del reloj del
bróker: consistentes con el cierre semanal, sin huecos intrasesión >1 h
observados. No se obtuvo el calendario oficial de sesiones del bróker, así que
la clasificación aún no está verificada contra la fuente de sesiones.

**Bloqueo de costes:** `spread=0` en 1.200/1.200 velas EURUSD y 1.177/1.200
USDCHF. Este campo no permite reconstruir spread efectivo; no se interpreta
como coste cero ni se usa para declarar expectancy neta. Los CSV H1 locales
anteriores terminan el 12-10-2026. A las `17:05Z` la captura ya tenía más de
tres horas de antigüedad desde su última vela cerrada; requiere recaptura
inmediatamente antes del replay. P1.1 sigue abierto hasta verificar sesiones,
procedencia y frescura al inicio de replay; P1.2 sigue NO-GO por cobertura
insuficiente de spread/slippage y madurez de la muestra.

## Avance — conciliación read-only de deals MT5 (2026-10-23)

Se consultó `history/deals` para EURUSD y USDCHF con filtro solicitado desde
`2026-08-01T00:00:00Z` hasta `2026-10-23T14:47:35Z`; la interpretación del
filtro horario por el servidor del bróker no se ha verificado. El API devolvió
576 deals (516 EURUSD, 60 USDCHF) y los campos `commission`, `swap` y `fee`
estaban presentes en todos. Se emparejaron 87 outcomes con deals de cierre por
`deal_ticket` exacto (79 EURUSD, 8 USDCHF). De esos 87, 75 PnL almacenados
coinciden con `profit + swap` dentro de €0,01; los otros 12 son EURUSD con
swap no nulo y registran el `profit` bruto, sin swap. `commission` y `fee`
fueron cero en los 87 cierres coincidentes. Esto identifica una discrepancia
histórica de contabilización, pero no constituye una estimación completa de
costes netos ni autoriza corregir filas antiguas.

La coincidencia outcome→deal no resuelve el enlace deal→orden interna: de 90
órdenes, solo una tiene `mt5_order_id` y ninguna tiene `mt5_position_id`; los
74 eventos `order_filled` consultados no incluyen IDs del bróker. Tampoco se
halló un comentario con formato exacto `AIQ:<id>` en los 576 deals. La consulta
read-only a `history/orders` devolvió HTTP 500 para EURUSD. Por tanto, 87/209
outcomes están vinculados a un deal por ticket en este corte; los 122 restantes
siguen sin verificar, no se consideran ausentes del bróker. No se aplica
backfill por símbolo/tiempo ni otra heurística. P0.2 continúa incompleto y
P1.2 sigue NO-GO: faltan costes históricos de spread/slippage y una relación
exacta con las órdenes internas.

El resumen saneado, sin IDs ni PnL por operación, está fuera del repositorio en
`broker-history-cost-audit-20261023.json` (SHA-256
`c742782294b9bab35829e6e229cfda05e545c3ab16e27b98d9e863aab5cbcadf`).
No se escribieron datos en PostgreSQL ni se alteraron órdenes o deals.

## Avance — auditor reproducible de enlaces broker↔órdenes (2026-10-23)

Se añadió `ops/audit_broker_order_links.py`. En una transacción PostgreSQL
`REPEATABLE READ READ ONLY` y consultas HTTP de lectura, compara únicamente
identificadores exactos: `deal_ticket`, `position_id`, ticket de orden MT5 y el
comentario completo `AIQ:<orders.id>`. No une por hora/símbolo, no escribe
relaciones y emite solo conteos agregados. El comando para repetirlo es:

`docker exec trading-fastapi python -m ops.audit_broker_order_links --from-date 2026-08-01T00:00:00Z`

Corte `2026-10-23T17:05:33Z`: 578 deals, 91 órdenes y 210 resultados. Hay 88
coincidencias exactas outcome→deal por `deal_ticket`, 1 ticket no encontrado
en la respuesta y 121 resultados sin ticket. Las 88 coincidencias son cierres
con los cuatro campos de PnL/costes presentes: 86 PnL almacenados coinciden con
`profit` bruto; 76 con `profit + swap + commission + fee`; en 12 cierres con
swap no nulo el valor almacenado coincide con profit bruto pero no incorpora
swap. La herramienta no determina qué versión histórica del EA originó esos
registros ni autoriza corregirlos.

No hay confirmación broker↔orden interna: 0 resultados tienen `order_db_id`;
de 91 órdenes, 0 tienen un `mt5_order_id` positivo válido y 0 tienen
`mt5_position_id`. Un `mt5_order_id` está poblado pero no es un entero positivo
válido. Ningún deal coincidió con ticket de orden interno, posición interna o
comentario exacto. El endpoint `history/orders` devolvió HTTP 500 tanto para
EURUSD como USDCHF; ambas respuestas tenían el mismo tamaño/hash, pero no se
dispone de logs del bridge para determinar la causa. El significado temporal
del filtro de historial sigue sin verificar. P0.2 permanece NO-GO.

El reporte agregado, sin IDs ni valores por operación, está fuera del
repositorio en `broker-order-links-audit-20261023-latest.json` (SHA-256
`25efb0c905a25f3ed2fbcfcb0f2a8a28e0fe77150d54b6ed1befbf1ca17b1fbc`). La prueba
focalizada del auditor dio **7 passed**. No se cambió ningún dato de
producción; el código de auditoría solo lee la BD y MT5.

## Actualización — despliegue EA v14.3 y comprobación en MT5 (2026-10-23)

Félix confirma que EA **v14.3 está compilado y desplegado** en el terminal vivo
que alimenta el endpoint de MT5. El log compartido dice
`2026.10.23 18:57:45.812 [AI Terminal] Iniciado v14.3` y muestra que conecta
al backend. La hora no incluye zona horaria y no se dispone aquí del binario,
hash de compilación ni log de MetaEditor; el despliegue se registra como
confirmación del usuario, no como compilación reproducida en este host.

Se ejecutó el auditor read-only con una ventana amplia
`2026-10-23T14:00Z`–`2026-10-23T17:17Z`, que cubre las conversiones candidatas
de esa hora local a UTC/horario del bróker. El servidor devolvió tres deals,
los tres USDCHF; **ninguno contenía el comentario exacto `AIQ:<id>`**. Dos
deal tickets coinciden con outcomes existentes, pero el filtro temporal del
bróker no está verificado y el reporte no expone el sello temporal por deal:
no se afirma que esos deals hayan ocurrido después del arranque. No se observó
un enlace broker→orden verificable en este corte. La comprobación decisiva queda
pendiente hasta que el terminal produzca un fill/deal cuya hora posterior al
arranque pueda confirmarse y cuyo comentario se compare con el `order_db_id`
exacto. No hacer backfill por tiempo/símbolo.

Reporte agregado sin IDs/tickets en
`broker-order-links-post-v14_3-20261023.json` (SHA-256
`5ca860017938e81a40d2e98dcb4127e3b62d37a7c78f5013be2c522769787a73`).
P0.2 permanece NO-GO mientras las órdenes/resultados no compartan identificador
exacto; el despliegue por sí solo no cierra esa conciliación.

## Reauditoría read-only — fuentes y enlaces (2026-10-23 17:30 UTC)

Se volvieron a ejecutar `ops.reconcile_replay_sources` y
`ops.audit_broker_order_links` dentro de `trading-fastapi`. Ambos consultaron
PostgreSQL en transacción `REPEATABLE READ READ ONLY`; el segundo también hizo
GET de historial de deals. Ambos finalizaron con código **2**: emitieron
reporte, pero el gate continúa sin aprobar. No se escribieron datos ni se
abrieron/cerraron órdenes.

- **Ciclos:** 7.198 filas, 7.085 IDs únicos y 113 filas repetidas; 306 sin
  `mt5_time`, 1 fuera del universo congelado y 12 IDs con decisiones
  contradictorias.
- **Órdenes/fills:** 91 órdenes; 15 sin ciclo de auditoría y 2 con ciclo
  repetido; 89 órdenes `filled` sin `mt5_order_id` válido. Los 75 eventos
  `order_filled` sí contienen `order_db_id`.
- **Outcomes/capital:** 210 outcomes; 121 sin `deal_ticket` y sin
  `position_id`; los 210 siguen sin `order_db_id`. Conciliación de capital
  sigue `NOT_ESTABLISHED`; no se aportó balance inicial ni extractos.
- **Deals:** 578 en la respuesta consultada (516 EURUSD, 62 USDCHF), cero con
  comentario exacto `AIQ:<id>`. Persisten 88 outcomes con ticket exacto, 1
  ticket sin coincidencia y 121 sin ticket. En los 88 cierres comparables,
  76 PnL coinciden con `profit + swap + commission + fee`; 12 omiten swap no
  nulo. Estos conteos no prueban que un deal sea posterior al arranque de
  v14.3: la semántica del filtro temporal del broker sigue sin verificarse.

La causa de `history/orders` **no pudo diagnosticarse**: los logs locales de
`trading-fastapi` no contienen esa ruta ni su excepción y no hay un contenedor
MT5 bridge local visible. Hace falta el log del bridge para las peticiones de
EURUSD/USDCHF; no se atribuye una causa sin esa evidencia. Tampoco se acredita
un fill natural posterior al despliegue de v14.3.

Observaciones de operación durante la inspección, no cambios ni aprobación de
modelo: el monitor reportó drift **critical** para EURUSD (`max_psi=5.3098`,
feature `sma20`, 17:23:39Z); el backup remoto GCS falló por falta de
Application Default Credentials, aunque el log indicó copia local verificada.
No se inició retraining, promoción ni cambio de autoridad.

## Avance P0.2c — clasificación determinista de fuentes internas (2026-10-23 17:38 UTC)

`ops.reconcile_replay_sources` ahora asigna estado y razón a cada fila de
`ai_predict_cycle`, `orders`, `order_filled` y `trade_outcomes`. Solo usa
identificadores exactos (`cycle_id` y `order_db_id`); no hace joins temporales
ni intenta enlazar deals por hora/símbolo. El resumen expone totales,
clasificaciones, razones y SHA-256 por fuente, sin imprimir IDs por fila.

Corte read-only `2026-10-23T17:38:49Z`:

| Fuente | Filas | Estado(s) | Razones principales |
|---|---:|---|---|
| `ai_predict_cycle` | 7.202 | 6.595 elegibles; 607 unresolved | 175 filas en IDs repetidos, 30 con decisión conflictiva, 306 sin `mt5_time`, 146 decisiones inválidas, 1 fuera del universo. Las razones pueden solaparse. |
| `orders` | 91 | 90 unresolved; 1 awaiting fill | 89 sin `mt5_order_id` y sin `mt5_position_id`, 15 sin ciclo exacto y 2 con ciclo ambiguo; las razones pueden solaparse. |
| `order_filled` | 75 | 75 enlazados a orden interna | 75/75 con coincidencia exacta de `order_db_id` y consistencia disponible de ciclo/símbolo. Esto no verifica el deal del broker. |
| `trade_outcomes` | 210 | 210 unresolved | 121 sin `deal_ticket`, 121 sin `position_id` y 210 sin `order_db_id`. |

Las cuatro fuentes tuvieron **0 filas sin estado o razón**. Digests de las
clasificaciones:

- ciclos: `c39d16fed684ea0eb0cd616d39b62e07675a20233a2387130ee847322aa734a2`
- órdenes: `1b8188ad92046e0e38ed718930a031c8586c1196feb2ba9be368e8e21dc53375`
- fills: `1c98c32d2d394d7cd3954e70ea1c790b2c5ec08522ba64daba6a79c4acf7130a`
- outcomes: `e33dd512b4985baf08f6f53b2b9e2e509a5c7a3f296c63c039af0ea733f7a0fd`

Esto cierra únicamente la asignación determinista de estado/razón para las
filas internas observadas; **no** las vuelve conciliadas ni cierra P0.2. Siguen
faltando el fill natural verificable de v14.3, logs de `history/orders`,
extractos de capital y explicación de las filas unresolved. Ejecución del
auditor: código 2 (`UNRECONCILED`, reporte emitido). Pruebas focalizadas:
**13 passed**; diagnósticos Pylance y sintaxis: sin errores. No se escribió en
PostgreSQL, no se cambió producción y no se abrió ninguna operación.

## Avance P1.1 — snapshot H1 de tres pares (2026-10-23 19:40 UTC)

Se capturó de forma read-only el endpoint MT5
`/api/v1/market/candles/latest` para EURUSD, GBPUSD y USDCHF (`timeframe=H1`,
`count=1200`). El offset fue detectado en +3h mediante
`services.market_time.detect_utc_offset_hours`. Se excluyó la vela que seguía
formándose según la regla broker→UTC de apertura + 1h ≤ hora de captura. Quedan
1.199 velas cerradas por símbolo; el último inicio cerrado corresponde a
18:00Z. La captura no se utilizó para replay.

El auditor de exportaciones devuelve para cada par `valid=true`,
`invalid_timestamp_rows=0`, `duplicate_timestamps=0`,
`out_of_order_timestamps=0`, `invalid_ohlc_rows=0` e
`invalid_spread_rows=0`. También informa **10 gaps por símbolo**, todos con
duración máxima de 49h y `calendar_review_required=true`. Con el offset broker
medido de +3h, cada cierre de vela del viernes y reapertura del domingo fue
convertido a `America/New_York`: **30 de 30 comparaciones** (10 intervalos × 3
pares) caen exactamente en viernes 17:00 y domingo 17:00 EDT. Esto clasifica
los gaps observados como cierre semanal, consistente con la convención
MetaQuotes-Demo aportada por Félix. El muestreo +3h en MetaQuotes-Demo del
14-09-2026 reportado en [MQL5](https://www.mql5.com/en/forum/516135) es
corroboración empírica, no política oficial: la publicación también plantea
explícitamente la pregunta de zona horaria/DST.

El OpenAPI del bridge no documenta una ruta de sesiones/calendario;
`symbol/info` expone campos `session_open`/`session_close`, pero no el horario
semanal. La URL [`CALENDAR_URL` de `services/news_filter.py`](../services/news_filter.py)
es un feed semanal de eventos económicos Forex Factory/faireconomy, no un
horario del broker. La regla general de DST estadounidense está publicada por
[NIST](https://www.nist.gov/pml/time-and-frequency-division/popular-links/daylight-saving-time-dst):
en 2026 termina el **1 de noviembre**. La muestra finaliza el 23 de octubre y
solo contiene observaciones EDT; no demuestra que MetaQuotes-Demo cambie su
offset ese día. Además, [AGENTS.md](../../AGENTS.md) documenta +2h desde el
25 de octubre, fecha distinta que requiere reconciliación: las capturas actuales
no observan ninguno de los dos cambios. No codificar un offset/calendario fijo;
confirmar el offset medido en observaciones broker posteriores a ambas fechas.
P1.1 sigue **PARCIAL / NO-GO PARA REPLAY** porque la captura ya no
cumple la frescura ≤5 min para un replay futuro. No se inició replay. El detalle
de cada comparación y las limitaciones de evidencia constan en el reporte
hashado de auditoría.

El campo spread no es utilizable como coste: las 1.200 filas son cero en
EURUSD y GBPUSD; USDCHF tiene 1.177/1.200 en cero. P1.2 permanece NO-GO.

Artefactos privados de sesión, fuera del repositorio:

- Snapshot completo y payloads originales: `h1-market-snapshot-20261023-1940Z.json`,
  SHA-256 `b0021fc79ecd2e4a7aecac701cc79c426c579c194c612175f1c1ac2dfca83591`.
- Reporte agregado del auditor: `h1-market-snapshot-audit-20261023-1940Z.json`,
  SHA-256 `184e52390102daee00ce236d7aeb0e80f37c79b4809f97d9905f2998469664dc`.
- Exportaciones TSV de auditoría:
  - EURUSD: `db7bd4a4c1603b8223bea70f4a1155a72a9f86e4058f21e3e9434f64ef5207de`
  - GBPUSD: `cd6ad755814cc40a046972382151f1704999f49691c454674e1032275a9757f4`
  - USDCHF: `baf273fbb8eb854ccee5cb6adc1dbc2907261850116358158150f4b364f5e4be`

## Reglas cuantitativas de evaluacion

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

## Actualización posterior — salvaguarda de enlace exacto (2026-10-24)

**P0.2 — lectura de aplicación a 2026-10-24 13:20 UTC:** el reconciliador
read-only clasificó 7.282 ciclos, 92 órdenes, 76 fills y 211 outcomes; cero
filas quedaron sin estado/razón. Siguen unresolved 614 ciclos, 90 órdenes y
210 outcomes. Los 76/76 fills tienen `order_db_id` exacto enlazado a una orden
interna; 210/211 outcomes carecen de `order_db_id`. El estado general sigue
`UNRECONCILED`; el PnL registrado es €347,29, pero el capital no está
conciliado.

**Cambio de aplicación:** se eliminó el emparejamiento heredado de la orden
viva más reciente por `(symbol, type)`. Si falta `order_db_id`, el servicio
emite una advertencia y no actualiza ninguna orden; con ID, la transición
conserva los guardas exactos de ID, símbolo, tipo y estado vivo. Esto cierra
el riesgo de mutar una orden equivocada, pero no repara outcomes históricos ni
demuestra la identidad de un fill natural del EA.

**Validación:** `pytest tests/test_orders_lifecycle.py
tests/test_trade_filled_webhook.py -q` → **37 passed**. No hubo escrituras a la
BD ni operaciones de prueba. La consulta de `history/orders` pertenece al
bridge remoto, no a esta aplicación; queda como dependencia externa del gate
contable, no como tarea de modificación en este repositorio. El siguiente
paso de aplicación sigue siendo validar un fill natural con `order_db_id` y
referencias exactas de broker; no generar trades para obtener esa evidencia.

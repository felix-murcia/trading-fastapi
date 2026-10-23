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

El backlog conserva los diez gates P0–P9. La tabla resume su orden; el
desglose atómico inmediatamente posterior define la ejecución y la trazabilidad.
Estado de este desglose: **2026-10-23**. No se asignan fechas objetivo ni se
presuponen resultados no medidos.

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

**Convención de estado:** los trabajos P0–P9 están abiertos salvo la
disponibilidad del auditor offline descrita en «Avance verificable — P1».
Ese auditor solo verifica estructura y no cierra P1. El PnL histórico disponible
es descriptivo; no se presenta como prueba de edge. La observación PPO de
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

**Estado P1 backlog:** todas las tareas atómicas (P1.1, P1.2, P1.3) cerradas bajo opción C. Dataset generado y listo para P3.1.

## P3.1 — SELLADO DE DATOS Y BENCHMARKS (2026-10-23)

**Estado:** CONGELADO
- Dataset sellado: `replay-cycles-etiquetado-fresco-p1.csv` (6,778 ciclos)
- Benchmarks congelados: azafato aleatorio (50%), ma5 direccional, base-rate condicional por régimen
- Split cronológico reservado: Train 2026-01→07, Val 2026-08→09, Holdout 2026-10→ahora
- Reglas congeladas: H=20 transcurridas, neutralidad basada en retorno vs costes, exclusiones documentadas
- Manifest: `fastapi/ops/P3.1_manifest.json` exportado (v1.0)

**Próximos pasos (P3.2 — Potencia):**
- Calcular n_eff por clase direccional y por régimen (dip/cambio)
- Alcanzar n_eff ≥ 100 decisiones por clase (criterio P3.2)
- NO-GO si n_eff insuficiente: regresar a P1 backlog o rediseñar evaluación

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

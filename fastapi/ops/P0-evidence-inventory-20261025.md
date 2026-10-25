# Inventario de evidencia P0 — corte 2026-10-25 12:07 UTC

## Veredicto de alcance

- **P0.1 — GO para el inventario documental:** las fuentes relevantes para el
  gate H=20, sus hashes, dominio temporal, limitaciones y uso admisible están
  catalogados abajo. “GO” cierra solo el trabajo de inventario; no valida el
  contenido económico de los datos.
- **P0.3 — NO-GO:** el preregistro existente no es ejecutable ni revisable como
  protocolo H=20 definitivo. La instrucción del LLM sigue formulando una
  probabilidad a 24h, no está fijada la identidad del modelo servido, no hay
  costes completos ni holdout nuevo y falta revisión independiente.
- **P0 — NO-GO:** no se puede cambiar el gate global mientras sigan abiertos
  P0.2/P1 y P0.3. Ningún estado se eleva por decisión editorial.

## Registro de fuentes y afirmaciones

Los hashes se calcularon sobre los bytes presentes en este corte. Los artefactos
en `~/.copilot/session-state/.../files/` son snapshots privados fuera del
repositorio; sus nombres y hashes se registran para identificar la evidencia,
pero este commit no los copia ni los convierte en fuentes operativas.

| ID | Fuente / SHA-256 | Dominio y corte | Afirmación admisible | Restricción / estado |
|---|---|---|---|---|
| E-01 | [`2026-10-22-punto-inflexion-ppo-llm-20h.md`](./2026-10-22-punto-inflexion-ppo-llm-20h.md), hash previo a este addendum: `ce3a1e7c2b4ee5444e2e8668eca80fa19fd54f86a0fbe67bd9d1fb10d61dab36` | Documento de protocolo, UTC | Define objetivo H=20, gates y secuencia de trabajo. | Es una especificación histórica versionada; no es por sí sola aprobación ni evidencia de resultados. |
| E-02 | [`2026-10-20-informe-viabilidad.md`](./2026-10-20-informe-viabilidad.md), `2b0e2d1fe0bdbc24cb8505e72650d5020ad56182bbb9b2bd38fc2a1031d84ae5` | Informe histórico; reloj/coste dependen de la fuente subyacente | Conserva métricas descriptivas del período estudiado. | El informe reconoce que faltan los vectores de 20.000 permutaciones y que el máximo simulado (+465,11 €) excede el PnL observado (+373,88 €). El 99,95 % y la conclusión de superar el azar están retirados. Se excluyen formalmente este test, sus p-valores y porcentajes de todos los gates de decisión; no se reinterpretan como evidencia positiva ni negativa. |
| E-03 | [`ppo_trading_bot_v3.zip`](../ml/ppo_trading_bot_v3.zip), `4cd9abb6bc94e33952afb627cf134d4535ee61677929902cfe8db9e406ea44a7`; [`ppo_trading_bot_v3_vec_norm.pkl`](../ml/ppo_trading_bot_v3_vec_norm.pkl), `d429c3d440d365502c54e9b47f6c3a1db356d662af18e5974c7c3207c065c8a5`; [`ppo_trading_bot_v3.manifest.json`](../ml/ppo_trading_bot_v3.manifest.json), `0c7ff8585815d6d29afa318c416f2d49f833cf1bec5e13c234e479601fbbe681` | Bundle/normalizador identificados en el repositorio; manifest creado 2026-10-19 | Identifica el artefacto PPO presente y su contrato `(20,25)`. Los hashes del bundle y VecNormalize coinciden con los declarados dentro del manifest. | No demuestra edge H=20. El manifest registra `gate.ok=false` (`absolute_floor_sum_reward`) y a la vez `promotion_path.drastic.promote=true`; esa metadata no prueba autorización de producción y requiere revisión de la ruta/semántica antes de usarla como evidencia de promoción. |
| E-04 | [`ai.py`](../routers/ai.py), `bfd915bd0f6d0005d633d3577e6bc6d5319fd681143f3453510b5cfe65816a80`; [`qwen_client.py`](../services/qwen_client.py), `636e2be82d9369048b61919c940daede15bcbb2a612d681409074959f648300b`; [`trading_env_v2.py`](../ml/trading_env_v2.py), `177d1ee99c81261339e529bf73f30eb39f6ea8d44db6408ee8f27c81c05b9dcf`; [`train_ppo_v3.py`](../ml/train_ppo_v3.py), `0bc954c9a091ac45ce634c115e959d815327713dfef9ba7292432d0552dd0e35` | Código del commit `81b2fd2` | Fija la versión de código observada para inferencia, prompt Qwen, entorno y entrenamiento. | El prompt de Qwen pide `bias_prob` para que el precio esté más alto **en 24h**. La petición enviada a la API no fija el campo `model`; no se registró la identidad/versionado del modelo servido. Por tanto, la entrada común H=20 y el hash de modelo LLM no están acreditados. No se usa el prompt actual como prueba de paridad H=20. |
| E-05 | [`config.py`](../config.py), `e177f6aef649423ac1b63288cae7930f0b7b14c63e5ae035b57fa83a11d444a6`; runtime inspeccionado 2026-10-25 12:07 UTC | Configuración de backend | `retrain_enabled=true`, `retrain_auto_approve=false`, `retrain_promote_drastic=false`, `retrain_report_only=true`, `qwen_skip_while_open=true`. | Son los valores runtime observados en este corte, no una congelación histórica. No certifican por sí solos los estados C1–C8 ni la identidad del LLM. |
| E-06 | [`P3.1_manifest.json`](./P3.1_manifest.json), `1a3d4c3aca04d9654b8bd35cf9c511d151b3ffe684fce4e47cf6dbc2e1aa7953`; [`replay-cycles-etiquetado-fresco-p1.csv`](./replay-cycles-etiquetado-fresco-p1.csv), `5d61ecc16e87b16e22d26a39fa5a54fc643292de0f544b2a075a05cf4c68f0a2` | Replay histórico; el manifiesto delimita holdout hasta 2026-10-23 | El auditor actual reproduce que el artefacto tiene 6.778 filas declaradas y reales. | Auditor ejecutado en este corte: `valid=false`, salida 2; solo EURUSD real frente a tres declarados, 6.778 HOLD, 102 IDs duplicados, 619 discordancias cycle/símbolo, 6.463 `bars_since_close` negativos, cero labels direccionales y Train vacío. No se reutiliza ni se corrige para aceptación. El holdout histórico ya fue inspeccionado y no es un holdout confirmatorio nuevo. |
| E-07 | Snapshot H1 privado `h1-market-snapshot-20261023-1940Z.json`, `b0021fc79ecd2e4a7aecac701cc79c426c579c194c612175f1c1ac2dfca83591`; reporte `h1-market-snapshot-audit-20261023-1940Z.json`, `184e52390102daee00ce236d7aeb0e80f37c79b4809f97d9905f2998469664dc`; TSV EURUSD/GBPUSD/USDCHF: `db7bd4a4c1603b8223bea70f4a1155a72a9f86e4058f21e3e9434f64ef5207de` / `cd6ad755814cc40a046972382151f1704999f49691c454674e1032275a9757f4` / `baf273fbb8eb854ccee5cb6adc1dbc2907261850116358151460f4b364f5e4be` | Captura MT5 2026-10-23 19:40Z; timestamps convertidos broker→UTC según offset medido en ese corte | Certifica estructura del snapshot y la clasificación documentada de 30/30 intervalos semanales contra 17:00 New York durante EDT. | Ya no es fresco para replay; no acredita DST de otoño ni el offset de 2026-10-25. El spread no sirve como coste (ceros en EURUSD/GBPUSD y mayoría de USDCHF). Solo evidencia estructural/histórica. |
| E-08 | Auditoría privada de costes `broker-history-cost-audit-20261023.json`, `c742782294b9bab35829e6e229cfda05e545c3ab16e27b98d9e863aab5cbcadf` | Deals MT5 consultados hasta 2026-10-23 14:47Z; semántica del filtro temporal no verificada | Comisión, fee y swap aparecen en 576 deals consultados; 87 coincidencias exactas por deal ticket fueron evaluadas. | No proporciona spread/slippage de entrada completos; hay discrepancias de swap (12 cierres en la auditoría agregada). No permite coste neto H=20 con cobertura 100%. |
| E-09 | Auditoría privada de enlaces `broker-order-links-audit-20261023-latest.json`, `25efb0c905a25f3ed2fbcfcb0f2a8a28e0fe77150d54b6ed1befbf1ca17b1fbc` | Snapshot 2026-10-23 17:05:33Z; filtro broker no verificado | Evidencia el estado de conciliación consultado: 210 outcomes, 88 matches exactos por ticket, 121 sin deal ticket, 0 con FK `order_db_id`; 0 comentarios exactos `AIQ:<id>`. | No prueba identidad de un fill posterior a v14.3 ni concilia capital. No hacer backfill por tiempo/símbolo. `history/orders` queda como dependencia externa del equipo del bridge, no como modificación de esta aplicación. |
| E-10 | [`market_time.py`](../services/market_time.py), `fed06ddad43bcb0e4b01d6268a21d87774e0f65e8c686c9e7b446c791039cedf`; [`timebase.py`](../services/timebase.py), `a88225d46b1a2476d3c16e114c3b7442e2c7aa10f7f6e5262fe823c2226cb950` | Lógica de reloj versionada en el código | Define la conversión de dominios y rechazo de timestamps/velas no contemporáneos. | Health observado en vivo en este corte: `degraded`, MT5 conectado, `broker_clock.validated=false`, `persisted=false`, `offset_h=null`. No hay base para declarar offset detectado ni iniciar replay con ese reloj. |

## Decisiones de uso para gates

1. El test de permutación histórico queda **excluido formalmente** del gate H=20:
   no se afirmará significación, no se regenerará desde datos reconstruidos y
   no se reutilizarán sus porcentajes. La ausencia de sus vectores no bloquea
   P0 por sí sola una vez registrada esta exclusión.
2. Solo se admiten como afirmaciones decisorias las que enlacen a un artefacto
   con hash y tengan el uso permitido indicado arriba. Las métricas históricas
   sin datos/coste/lineage adecuados quedan como descriptivas o “no usar para
   gate”.
3. Este inventario no convierte snapshots stale, outcomes sin identidad exacta,
   costes parciales, resultados del manifest fallido ni el manifest PPO en
   evidencia de edge H=20.
4. La siguiente versión de preregistro requiere: contrato LLM explícito H=20 y
   versión exacta del modelo; hashes de código/config/bundle/normalizador;
   costes y elegibilidad medibles; split temporal nuevo no inspeccionado;
   switches C1–C8 documentados; firma/revisión independiente previa a evaluar.

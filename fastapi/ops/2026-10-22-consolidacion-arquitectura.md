# Consolidación de arquitectura — auditoría con evidencia (2026-10-22)

Orden de Félix: «parar y revisar toda la lógica parcheada, caminos muertos,
complejidad, etc. y volver a CLEAN CODE, SOLID y HEXAGONAL». Este documento
es la evidencia; no hay afirmaciones sin número detrás.

## 1. Método (y un falso positivo que hay que declarar)

- **Topología y llamadores:** `codegraph` (índice AST del repo), no `grep`.
- **Complejidad y deuda:** medición AST reproducible, congelada en
  `fastapi/tests/architecture_baseline.py`.
- **Dinero:** el P/L REAL del bróker (`trade_outcomes`) y las órdenes
  (`orders`) como patrón de verdad, no la teoría.

El primer barrido se hizo con un script propio (AST + regex). Dijo «30
funciones públicas muertas» y «13 violaciones de capas». **Era falso en 9 de
las 30**: los endpoints de FastAPI se referencian por decorador y
`position_sizing.derive_order_from_bb` se invoca cualificado, no por nombre.
También me delató a mí: `min_lot_for_protection` seguía «vivo» porque **yo
había dejado su import huérfano** en `routers/ai.py` al sustituirlo por el
contrato. Corregido (regla 3 de `CLAUDE.md`: lo que tu cambio deja sin uso,
se borra). Se registra porque una auditoría que no declara sus falsos
positivos no es auditabla.

## 2. Hallazgos

| # | Área | Hallazgo | Sev. | Evidencia | Estado |
|---|------|----------|------|-----------|--------|
| H-C | Dinero | **USDCHF convertía el pip con tres fórmulas distintas**: contrato 12,17 € · guardián de riesgo 8,90 € · bróker 10,83 € | CRÍTICA | deal 58603377390 (trade_outcomes 285): 10,0 p × 0,04 lot = +4,33 € | **corregido** |
| H-D | Dinero | `order_manager._total_risk_eur` reimplementaba la conversión a mano (tercer motor) | ALTA | `pips * pip_value_per_lot(...) / rate` | **corregido** (delega en `expected_close_eur`) |
| H-E | Dinero | `min_lot_for_protection` duplicaba I2 del contrato (suelo Safe-4) | MEDIA | 0 usos en producción | **borrado** |
| H-F | Dinero | `derive_order` = segundo motor de sizing, sin llamadores | MEDIA | sólo 2 tests | **borrado** |
| H-G | Dinero | `MIN_ENTRY_REWARD_EUR` = segunda verdad del mínimo de entrada | MEDIA | sólo un test | **retirado** |
| H-H | Dinero | **DOS pipelines vivos con DOS motores de dinero**: `/api/v1/ai` (contrato) y `/v1/smc` → `simple_pipeline` (`derive_order_from_bb`, riesgo fijo `sl_risk_usd`, `rr_min`) | CRÍTICA | `main.py:250-253`; `settings.simple_pipeline_enabled=True`; `smc_signals` = **0 filas** | **resuelto 2026-10-22 — camino de señal retirado** |
| H-I | Complejidad | `predict_direction()` = **1230 líneas** y 49 llamadas distintas; `/predict` = 2526 líneas | ALTA | AST | congelado (no crece) |
| H-J | Capas | `ml/trading_env_v2.py` importa `services.position_sizing` (dominio→aplicación) | MEDIA | `:52` | deuda declarada |
| H-K | Errores | **82 `except Exception`** en runtime (AGENTS.md: «No silent failures») | ALTA | AST | congelado (no crece) |
| H-L | Higiene | 25 imports sin uso en runtime | BAJA | AST | **borrados**; un test lo impide |
| H-M | Higiene | `pip_value_per_lot("XAUEUR")` = 10,90 «aprox» (heurística dentro del dinero) | MEDIA | `position_sizing.py:78` | **resuelto 2026-10-22 — XAUEUR eliminado** |
| H-N | Geometría | El contrato no bloquea un R:R malo pero posible (SL 45 p / TP 40 p ⇒ 0,89) | ALTA | diseño declarado | abierto — decisión de dinero |
| H-O | Legado | `ml/archive/train_ppo_v2.py` (375 líneas) sin referencias; documentado en README | BAJA | `grep` = 0 | **resuelto 2026-10-22 — borrado** |

## 3. Corregido hoy (con números)

### H-C · La conversión de USDCHF estaba mal en los dos sitios

Contrato: 1 pip USDCHF = 0,0001 CHF × 100 000 USD = **10 CHF** (no 10 USD).
CHF→EUR exige **EURCHF = USDCHF × EURUSD** — dos conversiones, no una.

Medición con el deal del bróker (TP exacto, sin slippage):

```
trade_outcomes 285 · deal 58603377390 · SELL 0,04 lot · TP 10,0 p ⇒ +4,33 €
  ⇒ realizado = 10,825 €/pip/lote

10/precio            (contrato)        12,166  → +12,4 %
10/EURUSD            (guardián)         8,900  → −17,8 %
10/(precio × EURUSD) (correcto)        10,828  →  +0,0 %
```

Consecuencia real: el contrato aprobaba TPs que en EUR pagaban un ~11 % menos
de lo calculado (el modo de fallo de la orden 80, más pequeño) y el guardián
de riesgo **infravaloraba un 17,8 %** la exposición en USDCHF.

Corregido en la fuente única (`expected_close_eur`), con el deal citado en el
código y en el test. El guardián ahora **lee la misma función** en vez de
reimplementarla. `pip_value_per_lot("USDCHF")` sigue devolviendo 10,0 porque
su contrato es «pips en la divisa de cotización» (10 CHF), no euros.

**Verificación en vivo** (primer ciclo USDCHF post-deploy, 11:12:39 UTC):

| | antes | después | bróker |
|---|---|---|---|
| `ppl_eur` | 12,179 | **10,866** | 10,825 |
| `risk_eur` (0,02 lot) | 9,69 | **8,30** | — |

### Código muerto de dinero, borrado

| Pieza | Líneas | Por qué estaba muerta |
|---|---|---|
| `position_sizing.min_lot_for_protection` | 27 | el contrato cumple I2 (riesgo ≥ 4 €) al decidir el lote |
| `position_sizing.derive_order` | 30 | segundo motor de sizing; sólo lo llamaban 2 tests |
| `core.rules.MIN_ENTRY_REWARD_EUR` | 12 | el mínimo de entrada es I3 del contrato (EUR netos) |
| 25 imports sin uso | — | ruido que hace creer que hay dependencias que no existen |

### Las reglas de `CLAUDE.md`, ejecutables

`fastapi/tests/test_architecture_rules.py` (6 tests) convierte la prosa en
puertas mecánicas. No arregla la deuda histórica: **la congela** en
`architecture_baseline.py` y exige que sólo encoja.

1. **Dinero, una sola puerta** — `expected_close_eur` sólo la usan el
   contrato (decide) y el guardián (lee); `evaluate_trade` sólo la llama
   `/predict`; nadie importa `min_lot_for_protection` ni el pip en divisa de
   cotización.
2. **Una sola definición** de `expected_close_eur`, `pip_value_eur_per_lot` y
   `evaluate_trade` (no puede reaparecer un segundo motor).
3. **Capas** — `core/` (dominio) no importa infraestructura; `ml/` no importa
   `routers`/`db`; de `services` sólo la deuda declarada (H-J).
4. **Tamaño** — ninguna función nueva de 80+ líneas; las 31 existentes no
   pueden crecer (la mayor, `predict_direction`, 1230).
5. **Errores** — ningún `except Exception` nuevo (base: 82).
6. **Higiene** — cero imports sin uso en runtime.

## 4. Lo que NO se ha tocado (y por qué)

- **H-H (dos pipelines) — RESUELTO (2026-10-22).** Félix decidió **retirar**
  el camino de señal (opción a): `POST/GET /signal`, `simple_pipeline`,
  `derive_order_from_bb`, `derive_order_from_candle_open`, sus 14 claves de
  config y la tabla `smc_signals`. Evidencia de que no se usaba: **1 sola
  orden** con prefijo `smc_` en toda la historia (`orders` id 1,
  2026-09-15, `cancelled`) y `smc_signals` = 0 filas. Las lecturas
  (`/analytics`, `/news`, `/audit`) las usa el dashboard y se quedan.
- **H-I (`predict_direction` 1230 líneas).** Extraerlo sin rediseñar el ciclo
  es un trabajo de varios días y el test de arquitectura ya impide que crezca.
  Orden propuesto: (1) cadena de vetos → módulo puro con tests; (2) bloque de
  dinero → ya extraído (contrato); (3) contexto Qwen/memoria; (4) decisión.
- **H-K (82 `except Exception`).** No se pueden borrar a ciegas: cada uno
  tapa un camino de degradación. Se congelan y se atacan por módulo, leyendo
  qué se degrada y declarándolo.
- **H-N (R:R).** Es dinero: bloquear R:R < 1 o SL > 2×ATR cambia qué se
  opera. Necesita medición previa y decisión de Félix.

## 5. Evidencia de la suite

```
pytest tests/ -q  →  1298 passed, 2 xfailed
```

Incluye el caso real de la orden 80 (rechazado) y el deal 285 del bróker
(conversión USDCHF reproducida con +0,03 % de error).


## 6. Cierre de los tres puntos abiertos (2026-10-22)

Félix decidió: **retirar** el camino de señal SMC, **borrar** XAUEUR y
**borrar** `ml/archive/train_ppo_v2.py`. Ejecutado; detalle y tabla de líneas
en README §2026-10-22 (2).

| Punto | Acción | Evidencia |
|---|---|---|
| H-H | `POST/GET /v1/smc/signal`, `simple_pipeline`, los dos `derive_order_from_*`, 14 claves de config y tabla `smc_signals` retirados | `orders` con prefijo `smc_` = **1** (id 1, `cancelled`, 2026-09-15); `smc_signals` = **0 filas** |
| H-M | XAUEUR eliminado de `PIP_SIZE`, `order_validator` y la conversión; `pip_value_per_lot` falla ruidosamente fuera de `_PIP_10_QUOTE` | `test_symbol_sin_regla_de_pip_falla_ruidoso` |
| H-O | `ml/archive/train_ppo_v2.py` (375 l.) y su directorio, borrados | `grep` = 0 referencias |

Suite: **1288 passed + 2 xfailed** (1051 líneas borradas en 28 ficheros).
Deuda que sigue congelada y con dueño: `predict_direction` 1230 líneas (H-I),
82 `except Exception` (H-K), tope de distancia SL / R:R mínimo (H-N, decisión
de dinero que exige medición previa) y `ml/` → `services` (capa).
`/close` y `/news-check` quedan declarados como candidatos: con el motor
retirado sólo actúan sobre el ledger `smc_` heredado.

**Verificación en vivo (deploy 11:43 UTC):** `/v1/smc/signal` → 404 (POST y
GET); openapi expone sólo `close`, `news-check`, `audit`, `news`, `analytics`;
`smc_signals` eliminada de PostgreSQL (0 filas); ciclos post-deploy EURUSD
11:45:41 (`contract_ok=true`, `reentry_veto`) y USDCHF 11:47:01
(`contract_ok=true`, `ppl_eur` 10,861, `risk_eur` 9,38 ≤ 9,89 = 2 % de
494,47 €); `/health` OK.

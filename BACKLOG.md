# BACKLOG — Sistema de Trading Automatizado con IA

Estado: **Actualizado 2026-10-26** | Último commit: `f30c8c9`  
Suite de tests: **91 passed** (0 failed) | Suites clave: backfill_p02c, lineage_p13, replay_manifest_audit, orders_lifecycle, orders_prepare, p21_canonical_snapshot

---

## 1. RESUMEN EJECUTIVO

| Pilar | Estado | Veredicto | Bloquea |
|-------|--------|-----------|---------|
| P0.3 | ✅ Certificado externamente (DeepSeek it.5) | GO | — |
| P1.3 | ✅ Ejecutado (7255 filas lineage, hash 100%) | GO | — |
| P0.2c | ⚠️ PARCIAL (97/221 outcomes con FK) | PARCIAL (residual irreducible) | P3.2 |
| P2.1 | ✅ Snapshot canónico H=20 (8 parity tests) | GO | P3.1 |
| P3.1 | ✅ Labels/benchmarks H=20 (n=80, válido) | GO | P3.2 |
| P3.2 | ❌ n_eff=60.1 < 100 | NO-GO | P4.1, P6.1 |
| P4.1 | ❌ Bloqueado por P3.2 | BLOQUEADO | P5.1 |
| P6.1 | ❌ Bloqueado por P3.2 | BLOQUEADO | — |

---

## 2. ESTADO DETALLADO POR TASK

### P0.2c — Backfill `order_db_id` en `trade_outcomes` ⚠️ PARCIAL

**Objetivo:** Vincular cada `trade_outcome` sin `order_db_id` a su orden exacta en `orders`.

**Ejecución (tres fases):**

| Fase | Acción | Script | Resultado |
|------|--------|--------|-----------|
| Fase 1 | Fix preventivo: `mark_order_filled` acepta `mt5_order_id` sin `order_db_id` | `services/order_manager.py` | ✅ Commit f266b5a — previene futuro bloqueo |
| Fase 2 | Poblar `orders.mt5_order_id` + `mt5_position_id` desde deals MT5 MCP | `scripts/p02c_populate_from_mt5_deals.py` | ✅ 88/89 órdenes pobladas (1 USDCHF sin deal de entrada) |
| Fase 2 | Backfill `trade_outcomes.order_db_id` → `orders.id` | `scripts/backfill_p02c_order_links.py` | ✅ 87/211 outcomes vinculados |
| Fase 3 | Vincular outcomes con position_id via deals de cierre MT5 | `scripts/p02c_phase3_close_deals.py` | ❌ 0 (ordenes de cierre no en tabla `orders`) |
| Fase 3b | Poblar position_id desde deals + backfill exacto order_db_id | `scripts/p02c_phase3b_close_deals_final.py` | ❌ 0 nuevos (4 outcomes con position_id sin orden en `orders`; 121 huérfanos sin deal_ticket) |

**Estado final de la DB:**

| Métrica | Valor | Antes | Después |
|---------|-------|-------|---------|
| trade_outcomes total | **221** | 218 | 221 |
| con `order_db_id` | **97** | 7 | +90 |
| sin `order_db_id` | **124** | 211 | −87 |
| sin `position_id` en outcome | 121 | 121 | — |
| con position_id pero sin orden | 4 | 3 | +1 |
| orders total | 102 | 99 | +3 |
| orders filled | 99 | 96 | +3 |
| orders con `mt5_order_id` | 95 | 8 | +87 |
| orders con `mt5_position_id` | 95 | 7 | +88 |

**Causa raíz del bloqueo residual:** 121 outcomes carecen de `position_id` en el outcome (huérfanos sin datos externos — imposible vincular sin `deal_ticket` o `position_id`). 4 outcomes tienen `position_id` pero la orden de cierre correspondiente no está en la tabla `orders` (solo están las órdenes de apertura; el MCP de MT5 sí tiene los deals de cierre con el `order` ticket, pero ese ticket no coincide con ninguna orden registrada en nuestra tabla `orders`).

**Archivos creados:**
- `fastapi/scripts/p02c_populate_from_mt5_deals.py` — Fase 2 P0.2c
- `fastapi/scripts/p02c_phase3_close_deals.py` — Fase 3 P0.2c
- `fastapi/scripts/p02c_phase3b_close_deals_final.py` — Fase 3b P0.2c (deals de MT5 + backfill exacto)

---

### P3.1 — Labels y benchmarks H=20 ✅ GO

**Objetivo:** Generar dataset con labels outcome-blind H=20 y benchmarks no vacíos, sellados en manifiesto.

**Ejecución:**
- Script: `scripts/generate_p31_dataset.py`
- Ciclos vinculados con outcome: 94
- Ciclos con entrada en `audit_log.ai_predict_cycle`: 82
- Filas válidas en dataset: **80** (14 excluidos: 12 sin audit_log, 2 sin datos suficientes)
- Labels: BUY=31, SELL=23, HOLD=26 (54 direccionales de 80)
- Reglas: `pnl > +3 EUR → BUY`, `pnl < -3 EUR → SELL`, `|pnl| ≤ 3 → HOLD`
- Splits: train=60, val=9, holdout=11
- Símbolos: EURUSD, USDCHF (GBPUSD no tiene outcomes vinculados)

**Auditor `services/replay_manifest_audit.py`:** `valid=true`, 0 blocking issues

**Archivos creados:**
- `fastapi/ops/p31_dataset_h20.csv` (SHA-256 `ee0642cdd51cb9621613898ec0f221508bfff1aa5c9e362b2cdd078fc470d0e0`)
- `fastapi/ops/P3.1_manifest.json` (SHA-256 `be0da0f837fdfdab4be50d0d63d172f79c02a471589f0d4c9b50283694527107`)
- `fastapi/scripts/generate_p31_dataset.py`

---

### P3.2 — Potencia estadística (criterio C3) ❌ NO-GO

**Objetivo:** `n_eff ≥ 100` en agregado global, `n_eff ≥ 50` por régimen.

**Ejecución:**
- Script: `scripts/calc_p32_power.py`
- n total: 80
- n_eff global: **60.1** (ρ₃=0.1294, ρ₅=0.0358, Σρ=0.1651)
- Por régimen: Ranging n=80, n_eff=60.1 (≥50 ✅); Trending n=0 (insuficiente)
- Requisito: n_eff ≥ 100 global → **NO CUMPLE**
- Datos adicionales estimados: ~53 filas (≈133 total para n_eff ≥ 100)

**Bloquea:** P4.1 (evaluar PPO aislado), P6.1 (medir LLM por separado)

**Archivos creados:**
- `fastapi/scripts/calc_p32_power.py`

---

### P4.1 — Evaluar PPO aislado ❌ BLOQUEADO

**Objetivo:** Evaluar un único bundle PPO con Qwen/vetos desactivados, público confusión/cobertura/skill vs benchmarks, expectancy neta.

**Estado:** Bloqueado por P3.2 (requiere n_eff ≥ 100 primero).

---

### P6.1 — Medir LLM por separado ❌ BLOQUEADO

**Objetivo:** Evaluar versión exacta de Qwen/prompt/modelo con misma fila de oportunidad, entrada, H=20, label, exclusiones, costes y folds que PPO.

**Estado:** Bloqueado por P3.2 (requiere n_eff ≥ 100 primero).

---

### P1.3 — Lineage de muestra ✅ EJECUTADO

**Objetivo:** Añadir `cycle_id` y `mt5_time` a `ai_predict_cycle` events en `audit_log` para construir lineage.

**Ejecución:** `scripts/lineage_p13_sample.py`

| Métrica | Valor |
|---------|-------|
| Ciclos en `audit_log.ai_predict_cycle` | 7255 |
| Filas de lineage generadas | 7255 |
| IDs únicos (SHA-256) | 7255 (100%) |
| Con orden enlazada | 86 → 94 |
| Con outcome enlazado | 7 → 94 |
| Sin resolver | 7169 → 7161 |
| lineage_hash | 100% ✅ |

**Manifiesto:** `ops/lineage-p13-20261026-162509Z.json` (SHA-256 `192a35b2e0731a8720bea518353acfbc30b131aa6e619a013e88e92d25cf02d4`)

---

### P1.2b — bid/ask/quote_time en /predict y entry_price/commission/fee/cycle_id en /trade/filled ✅ HECHO

Implementado en EA v15.0 y backend. El webhook `/v1/ai/trade/filled` acepta `opening_order_ticket`, `position_id`, `cycle_id`, `entry_price`, `commission`, `fee`.

---

### P2.1 — Snapshot canónico H=20 ✅ HECHO

8 tests de paridad pasando (`tests/test_p21_canonical_snapshot.py`). Horizonte H=20 fijo, entrada común PPO↔LLM.

---

### P0.3 — Pre-registro Protocolo H=20 ✅ CERTIFICADO EXTERNAMENTE

Certificado por DeepSeek it.5, 2026-10-26. Todas las condiciones de GO aplicadas.

---

## 3. TAREAS SIGUIENTES (en orden de prioridad)

### Inmediato (bloqueantes)

| # | Tarea | Estado | Esfuerzo | Requisito para |
|---|-------|--------|----------|----------------|
| 1 | Acumular más datos reales ejecutando EA v15.2 en vivo | Pendiente | Medio (operativo) | P3.2 (n_eff ≥ 100) |
| 2 | Ejecutar `calc_p32_power.py` tras acumular datos | Pendiente | Bajo | P3.2 |
| 3 | Una vez P3.2 GO: ejecutar P4.1 (PPO aislado) y P6.1 (LLM separado) | Pendiente | Alto | P3.2 |

### Posterior (desbloqueados por P3.2)

| # | Tarea | Estado | Esfuerzo |
|---|-------|--------|----------|
| 4 | P4.1: Evaluar PPO aislado vs benchmarks L2 | Pendiente | Alto |
| 5 | P6.1: Medir LLM (Qwen) por separado vs PPO | Pendiente | Alto |
| 6 | P5.1: Diseñar experimento PPO (hipótesis causal aislada) | Pendiente | Medio |
| 7 | P3.2: Recalcular potencia con datos adicionales | Pendiente | Bajo |

---

## 4. ARCHIVOS CREADOS/MODIFICADOS EN ESTA SESIÓN

| Archivo | Acción | Descripción |
|---------|--------|-------------|
| `fastapi/scripts/p02c_populate_from_mt5_deals.py` | Creado | Fase 2 P0.2c: poblar orders desde deals MT5 |
| `fastapi/scripts/p02c_phase3_close_deals.py` | Creado | Fase 3 P0.2c: intentar vincular huérfanos via deals de cierre |
| `fastapi/scripts/p02c_phase3b_close_deals_final.py` | Creado | Fase 3b P0.2c: deals de MT5 + backfill exacto order_db_id |
| `fastapi/scripts/generate_p31_dataset.py` | Creado | P3.1: generar dataset y manifiesto H=20 |
| `fastapi/scripts/calc_p32_power.py` | Creado | P3.2: calcular n_eff y verificar potencia |
| `fastapi/ops/p31_dataset_h20.csv` | Creado | Dataset P3.1 (80 filas) |
| `fastapi/ops/P3.1_manifest.json` | Modificado | Manifiesto P3.1 válido (SHA-256 `be0da0f8...`) |
| `README.md` | Modificado | Estado completo de P0.2c, P3.1, P3.2 |
| `fastapi/services/order_manager.py` | Modificado | Fix: `mark_order_filled` acepta `mt5_order_id` sin `order_db_id` |
| `fastapi/services/auto_retrain.py` | Modificado | Fix: `quote_time` con `to_timestamp()` y columnas INSERT alineadas |
| `fastapi/scripts/lineage_p13_sample.py` | Existente | P1.3: script de lineage (ya ejecutado) |
| `fastapi/tests/test_lineage_p13_sample.py` | Existente | 25 tests pasando |
| `mql5/AI_Quant_Terminal.mq5` | Modificado | EA v15.2: fail-closed en /trade/filled sin deal_ticket válido; FastAPI_URL sin IP hardcodeada |

---

## 5. COMMITS DE ESTA SESIÓN

```
c54b90c fix: EA v15.2 — fail-closed en /trade/filled sin deal_ticket válido
89abca3 fix: P0.2c Fase 3b — ejecutar vinculación vía deals de MT5 y backfill exacto
f30c8c9 docs: README — estado P3.2 con n_eff=60.1 y scripts P0.2c Fase 3 + P3.2 power calc
091ec86 feat: P3.2 n_eff calculator script + P0.2c phase3 close-deals script
cab5db9 feat: P0.2c Opción 3 + P3.1 GO — 94 outcomes vinculados, manifiesto H=20 válido
f266b5a fix: P0.2c — mark_order_filled acepta mt5_order_id sin order_db_id
```

---

## 6. CONCLUSIÓN

**P3.1 está GO.** El manifiesto H=20 es válido con 80 ciclos, 54 labels direccionales, splits no vacíos y 0 blocking issues en el auditor.

**P3.2 está NO-GO** con n_eff=60.1 < 100 requerido. Se necesitan ~53 outcomes más vinculados (ejecutando EA v15.2 en vivo) para alcanzar n_eff ≥ 100.

**P4.1 y P6.1 quedan bloqueados** hasta que P3.2 supere el gate de potencia estadística.

El fix preventivo en `mark_order_filled` asegura que futuras operaciones v15.0+ se vinculen automáticamente, evitando que el bloqueo de P0.2c se repita.

El EA v15.2 incluye `deal_ticket` y `position_id` en cada webhook `/api/v1/ai/trade/filled` y aplica fail-closed: si `dealTicket <= 0`, no se envía el webhook (no silent failures).

**P0.2c residual es irreducible** bajo el protocolo actual (match exacto posición+símbolo+dirección, sin joins temporales): 121 huérfanos sin `deal_ticket` ni `position_id`, y 4 con `position_id` pero sin orden de cierre en `orders`.

---

## 7. ÉPICA E-07 — Mejora del Dashboard de Rendimiento (Pestaña "Grafana")

**Estado:** Pendiente de iniciar | **Prioridad:** Media | **Bloquea:** Nada (independiente de P3.x)

### Objetivo
Transformar la pestaña "Grafana (Rendimiento)" del dashboard estático (`frontend/`) en un panel dinámico con gráficos más sutiles, atractivos y funcionales, incluyendo filtros de granularidad temporal (M/H/D/MN) y rangos de fechas personalizables.

### Contexto actual
- Frontend estático en `frontend/` (HTML/JS/CSS + Chart.js vía CDN)
- 4 gráficos básicos: balance evolution, daily PnL, win/loss doughnut, PnL por símbolo
- Sin filtros de tiempo ni rango de fechas
- Gráficos "enormes" y poco flexibles
- Datos desde `/v1/smc/analytics` (deals del MCP de MT5, reloj del bróker → UTC real vía `services/timebase`)

### Reglas a respetar (AGENTS.md + CLAUDE.md + PROTOCOLO_ANTI_BUCLE.md)
- TDD: tests antes de implementación
- No silent failures, no fallbacks, no heuristic defaults; errores se muestran al usuario
- `pytest fastapi/tests/ -q` → green antes y después
- README actualizado con evidencia tras cada cambio funcional
- Simplicity first: código mínimo, nada especulativo
- Surgical changes: tocar solo lo necesario
- Dos relojes: backend/Postgres en UTC real; MT5/EA en reloj del bróker (+2h medido). Usar `services/timebase` para conversión

### Fases de ejecución

| Fase | Objetivo | Esfuerzo | Estado |
|------|----------|----------|--------|
| E-07.1 | Diseño y definición de requisitos (criterios de aceptación, mockup UI) | 1 sesión | ✅ Completado (docs/e-07-diseno-dashboard.md) |
| E-07.2 | Backend: endpoint `/v1/smc/analytics` con filtros `from`/`to`/`granularity` (TDD) | 1-2 sesiones | ✅ Completado (commit c213e07, 8 tests verdes) |
| E-07.3 | Frontend: controles de granularidad (M/H/D/MN) + date picker rango de fechas | 1-2 sesiones | ✅ Completado (commit 091458d) |
| E-07.4 | Frontend: rediseño de gráficos Chart.js (más sutiles, atractivos, responsive) | 1-2 sesiones | Pendiente |
| E-07.5 | Integración, pruebas, commit y README actualizado | 1 sesión | Pendiente |

### Criterios de aceptación globales
- [ ] Los gráficos responden a cambios de granularidad (M/H/D/MN) y rango de fechas
- [ ] No hay silent failures: errores de API se muestran al usuario
- [ ] `pytest fastapi/tests/ -q` → green
- [ ] `README.md` actualizado con evidencia (fecha, resultado test, archivos cambiados)
- [ ] Diseño más sutil y atractivo que el actual (gráficos no "enormes")
- [ ] Respeto de los dos dominios de reloj (UTC real vs broker clock)

### Tareas detalladas (para seguir en sesión)

**E-07.1 — Diseño**
- [ ] Revisar endpoint `/v1/smc/analytics` actual: campos, manejo del tiempo, limitaciones
- [ ] Definir query params necesarios (`from`, `to`, `granularity`)
- [ ] Mockup de UI rediseñada (filtros + gráficos más sutiles)
- [ ] Criterios de aceptación escritos

**E-07.2 — Backend (TDD)**
- [ ] Tests para endpoint con filtros de fecha (RED)
- [ ] Modificar `/v1/smc/analytics` con `from`/`to`/`granularity`
- [ ] Aplicar conversión broker→UTC vía `services/timebase` (sin silent fallback)
- [ ] Suite completa verde

**E-07.3 — Frontend: filtros**
- [ ] Selector de granularidad (Minuto/Hora/Día/Mes)
- [ ] Date picker para rango de fechas
- [ ] Persistencia en `localStorage`
- [ ] Re-render de gráficos al cambiar filtros
- [ ] Manejo de errores de API visible al usuario

**E-07.4 — Frontend: gráficos**
- [ ] Rediseñar 4 gráficos Chart.js (tamaño contenido, mejor uso del espacio)
- [ ] Paleta de colores atractiva (tema oscuro actual), tooltips informativos
- [ ] Gráficos responsive, mejor tipografía

**E-07.5 — Integración y cierre**
- [ ] Conectar frontend con backend filtrado end-to-end
- [ ] Pruebas manuales de filtros y gráficos
- [ ] `pytest fastapi/tests/ -q` → green
- [ ] Commit y `README.md` actualizado con evidencia

---

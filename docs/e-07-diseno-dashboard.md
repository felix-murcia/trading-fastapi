# Diseño E-07.1 — Dashboard de Rendimiento (Pestaña "Grafana")

**Estado:** Diseño aprobado (Fase E-07.1)  
**Fecha:** 2026-10-27  
**Autor:** trading-auditor + agente implementador  
**Basado en:** recomendaciones del subagente trading-auditor, AGENTS.md, CLAUDE.md, PROTOCOLO_ANTI_BUCLE.md

---

## 1. Revisión del endpoint actual (`/v1/smc/analytics`)

| Aspecto | Estado actual |
|---------|---------------|
| Fuente de datos | `market_port.get_deals()` del MCP de MT5 (reloj del bróker) |
| Conversión de tiempo | `services.timebase.broker_offset_hours()` → UTC real |
| Agrupación | Por día UTC (`date_str`) |
| Campos devueltos | `daily_pnl`, `wins`, `losses`, `by_symbol`, `evolution`, `kpis` |
| Filtros actuales | Ninguno (siempre devuelve todos los deals) |
| Error handling | Retorna `{"error": ...}` si no hay velas H1 o fallo de LOOP_ERRORS |
| Silent failures | No hay (errores se propagan y retornan en JSON) |

**Limitaciones para el diseño de filtros:**
- Los datos vienen del MCP de MT5, no de `trade_outcomes` en PostgreSQL directamente
- El endpoint no acepta parámetros de query actualmente
- Para filtrar por rango de fechas, el backend debe filtrar los deals después de obtenerlos del MCP

---

## 2. Query params propuestos para `/v1/smc/analytics`

| Param | Tipo | Valor por defecto | Descripción |
|-------|------|-------------------|-------------|
| `from` | string (ISO date) | `null` (sin filtro) | Fecha inicio del rango (inclusive), UTC real |
| `to` | string (ISO date) | `null` (sin filtro) | Fecha fin del rango (inclusive), UTC real |
| `granularity` | enum | `"D"` | `"M"` (minuto), `"H"` (hora), `"D"` (día), `"MN"` (mes) |
| `symbol` | string | `null` (todos) | Símbolo para filtrar (EURUSD, USDCHF, etc.) |

**Reglas de negocio:**
- Si `from` > `to` → error 400 (no silent fallback)
- Si `granularity` no es válido → error 400
- Sin `from`/`to` → se devuelven todos los datos disponibles (comportamiento actual)
- Las fechas se interpretan como UTC real; el offset broker→UTC se aplica antes del filtrado

---

## 3. Mockup de UI rediseñada (descripción)

### Pantalla principal — Pestaña "Rendimiento" (antes "Grafana")

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Trading Dashboard                                                    [Cargar]│
│  Monitoreo en tiempo real de rendimiento                              [API URL][Token]│
├─────────────────────────────────────────────────────────────────────────┤
│  FILTROS                                                              │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐     │
│  │ Desde: [____]│ │ Hasta: [____]│ │ Granularidad:│ │ Símbolo: [▼]│     │
│  │ Fecha       │ │ Fecha       │ │ M/H/D/MN    │ │ (todos)     │     │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘     │
│  [Reset filtros]                                                        │
├─────────────────────────────────────────────────────────────────────────┤
│  KPIs (fila 1, ancho completo)                                          │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐                                   │
│  │ Win  │ │ Profit│ │ Max  │ │ Trades│                                   │
│  │ Rate │ │ Factor│ │ DD   │ │ Hoy │                                   │
│  │ 58%  │ │ 1.45  │ │ -4.2%│ │ 12  │                                   │
│  └──────┘ └──────┘ └──────┘ └──────┘                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  Gráficos (grid responsive)                                             │
│  ┌──────────────────┐ ┌──────────────┐                                  │
│  │ Equity curve     │ │ Daily PnL    │                                  │
│  │ con drawdown     │ │ (últimos 30d)│                                  │
│  │                  │ │              │                                  │
│  └──────────────────┘ └──────────────┘                                  │
│  ┌──────────────────┐ ┌──────────────┐                                  │
│  │ PnL por símbolo  │ │ PnL por hora │                                  │
│  │ + exposición     │ │ del día      │                                  │
│  └──────────────────┘ └──────────────┘                                  │
│  ┌──────────────────┐ ┌──────────────┐ ┌──────────────┐                │
│  │ SL vs TP         │ │ PPO vs Qwen  │ │ Win rate     │                │
│  │ distribución     │ │ vs Regla     │ │ por régimen  │                │
│  └──────────────────┘ └──────────────┘ └──────────────┘                │
└─────────────────────────────────────────────────────────────────────────┘
```

### Reglas de diseño visual (según CLAUDE.md: simplicity first)

- Máximo 10 paneles totales
- Sin gráficos 3D, sombras pesadas, gradientes excesivos ni animaciones continuas
- Paleta de 4–5 tonos coherentes con el tema oscuro actual (`--bg-color: #0b0f19`, `--primary: #3b82f6`)
- Gráficos responsive, mejor tipografía (Inter ya cargada)
- En móvil: máximo 2 paneles visibles sin scroll

---

## 4. Qué gráficos se implementan en la primera iteración (E-07.2 a E-07.4)

| Gráfico | Tipo Chart.js | Datos fuente | Prioridad | Notas |
|---------|---------------|--------------|-----------|-------|
| Equity curve con drawdown | Línea + área rellena | `evolution` + cálculo de running max | Crítica | Reemplaza *balance evolution* |
| Daily PnL | Barra vertical | `daily_pnl` | Alta | Simplificado, últimos 30–90 días |
| Win/Loss + KPIs | Doughnut pequeño + 4 tarjetas | `wins`, `losses`, `kpis` | Alta | Simplifica *doughnut actual* |
| PnL por símbolo + exposición | Barra horizontal doble | `by_symbol` + count por símbolo | Alta | Mejora *PnL por símbolo actual* |
| PnL por hora del día | Barra 24 h | `evolution` (hora extraída) | Media | Nuevo |
| SL vs TP distribución | Barra apilada 100 % | Requieren `sl_hit`, `tp_hit` en deals | Media | Puede no estar en deals del MCP |

**Gráficos condicionales (se omiten si los datos no existen):**
- #7 PPO vs Qwen vs Regla — requiere campo de decisión del modelo en `trade_outcomes` (no existe hoy)
- #8 Win rate por régimen — requiere régimen de mercado por trade (no existe hoy)

---

## 5. Criterios de aceptación — Fase E-07.1 (Diseño)

| ID | Criterio | Estado |
|----|----------|--------|
| E-07.1.1 | Documento de diseño creado con mockup de UI, query params, y lista de gráficos priorizados | ✅ Hecho (este archivo) |
| E-07.1.2 | Query params definidos: `from`, `to`, `granularity`, `symbol` | ✅ Hecho |
| E-07.1.3 | Reglas de error definidas (400 si `from` > `to` o `granularity` inválido) | ✅ Hecho |
| E-07.1.4 | Respeto de dos relojes: UTC real para backend, bróker para MT5, conversión vía `services/timebase` | ✅ Documentado |
| E-07.1.5 | Sin silent failures: errores de API se muestran al usuario | ✅ Documentado |
| E-07.1.6 | Máximo 10 paneles en el dashboard | ✅ Documentado |
| E-07.1.7 | Criterios de aceptación de E-07.2 a E-07.5 definidos en BACKLOG.md | Pendiente (próximo paso) |

---

## 6. Criterios de aceptación — Fases E-07.2 a E-07.5 (para seguir en sesión)

### E-07.2 — Backend (TDD)
- [ ] Tests escritos para `/v1/smc/analytics` con `from`/`to`/`granularity` (RED → GREEN)
- [ ] Endpoint acepta query params y filtra deals por rango de fechas
- [ ] Endpoint agrupa por granularidad seleccionada (M/H/D/MN)
- [ ] Conversión broker→UTC vía `services/timebase` sin silent fallback
- [ ] Error 400 si `from` > `to` o `granularity` inválido
- [ ] `pytest fastapi/tests/ -q` → green

### E-07.3 — Frontend: filtros
- [ ] Selector de granularidad (M/H/D/MN) en la UI
- [ ] Date picker para rango de fechas (desde/hasta)
- [ ] Selector de símbolo (multi-selección, default: todos)
- [ ] Persistencia de filtros en `localStorage`
- [ ] Re-render de gráficos al cambiar filtros
- [ ] Botón "Reset filtros"
- [ ] Errores de API mostrados al usuario (no silent failures)

### E-07.4 — Frontend: gráficos
- [ ] Equity curve con drawdown (reemplaza *balance evolution*)
- [ ] Daily PnL simplificado (últos 30–90 días)
- [ ] Win/Loss + 4 tarjetas KPI (simplifica *doughnut*)
- [ ] PnL por símbolo + exposición (mejora *PnL por símbolo*)
- [ ] Gráficos responsive y con mejor tipografía
- [ ] Máximo 10 paneles en pantalla

### E-07.5 — Integración y cierre
- [ ] Frontend conectado a backend con filtros end-to-end
- [ ] Pruebas manuales de filtros y gráficos
- [ ] `pytest fastapi/tests/ -q` → green
- [ ] Commit de todos los cambios
- [ ] `README.md` actualizado con evidencia (fecha, resultado test, archivos cambiados)

---

## 7. Advertencias (del auditor trading-auditor)

1. Máximo 10 paneles — si se añade uno nuevo, se retira otro
2. Sin joins temporales (protocolo P0.2c: match exacto solo)
3. Sin mezclar relojes — usar `services/timebase` para conversión broker→UTC
4. Sin silent defaults — datos faltantes = estado vacío explícito
5. #7 y #8 requieren campos que pueden no existir — si faltan, se omiten y se anotan como hallazgo de auditoría
6. n_eff = 60.1 < 100 (P3.2 NO-GO) — desgloses finos llevan advertencia de baja potencia estadística
7. No saturar móvil — máximo 2 paneles visibles sin scroll

---

## 8. Archivos afectados (previstos)

| Archivo | Cambio previsto |
|---------|-----------------|
| `fastapi/routers/smc.py` | Añadir query params `from`, `to`, `granularity`, `symbol` a `/v1/smc/analytics` |
| `fastapi/tests/` | Tests para endpoint con filtros (TDD) |
| `frontend/js/api.js` | Enviar params de filtro en llamadas al backend |
| `frontend/js/app.js` | Estado de filtros, persistencia `localStorage`, re-render |
| `frontend/js/ui.js` | Nuevos gráficos Chart.js, layout de filtros, KPIs |
| `frontend/css/style.css` | Estilos para filtros, layout grid, KPI cards |
| `frontend/index.html` | Controles de filtro en el header |
| `BACKLOG.md` | Marcar E-07.1 como completado, actualizar estado de fases |
| `README.md` | Evidencia tras implementación (fase E-07.5) |

---

## 9. Próximo paso

Iniciar **Fase E-07.2 — Backend (TDD)**:

1. Escribir tests para `/v1/smc/analytics` con `from`/`to`/`granularity` (RED)
2. Implementar endpoint con filtros (GREEN)
3. Ejecutar suite completa: `pytest fastapi/tests/ -q`

¿Apruebas este diseño para proceder con E-07.2?

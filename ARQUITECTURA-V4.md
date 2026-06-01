# ARQUITECTURA SISTEMA DE TRADING FOREX v4.0

**Versión:** 4.0  
**Fecha:** 2026-06-01  
**Estado:** Diseño aprobado — pendiente de implementación

---

## DIAGNÓSTICO DEL SISTEMA ACTUAL (v3.x)

### Problemas estructurales

| # | Categoría | Problema | Severidad |
|---|-----------|----------|-----------|
| 1 | Arquitectura | n8n mezcla orquestación con lógica de negocio: nodos `code` contienen validación de riesgo, normalización y decisión | CRÍTICA |
| 2 | Determinismo | Los LLM están en el camino crítico de ejecución — respuesta malformada o lenta bloquea todo | CRÍTICA |
| 3 | Idempotencia | No hay `cycle_id` global. Si el trigger se dispara dos veces, se emiten dos órdenes | CRÍTICA |
| 4 | Fallos silenciosos | `try-catch` en `Log Result` devuelve valores por defecto sin abortar la ejecución | ALTA |
| 5 | Acoplamiento | `Analizar Pares` determina el mejor par Y prepara datos para el LLM — dos responsabilidades en un nodo | ALTA |
| 6 | Trazabilidad | No hay `trace_id` por ciclo de decisión — imposible reconstruir por qué se colocó una orden | ALTA |
| 7 | Validación | Rangos de precio hardcoded en múltiples nodos JavaScript — fuente de verdad duplicada | MEDIA |
| 8 | Recuperación | No hay diferencia entre error recuperable y error fatal — todo se trata igual | MEDIA |
| 9 | Modularidad | Los 4 agentes Jetson siempre se ejecutan todos — no hay selección adaptativa | MEDIA |
| 10 | Seguridad | Credenciales MT5 y Telegram van directamente en nodos HTTP sin capa de validación | MEDIA |

---

## PRINCIPIOS RECTORES

1. **n8n solo orquesta.** No valida, no calcula, no decide. Solo llama y enruta.
2. **FastAPI es el guardián.** Toda validación, normalización y control de riesgo ocurre aquí.
3. **MT5 solo ejecuta.** Nunca recibe señales sin haber pasado por FastAPI.
4. **Un `cycle_id` por ciclo.** Permite reproducibilidad, auditoría y prevención de duplicados.
5. **Fallos son explícitos.** Un nodo que falla detiene el flujo — no continúa con valores por defecto.
6. **Los LLM son asesores, no árbitros.** Su output es una señal que debe pasar validación determinista antes de ejecutar.

---

## DIAGRAMA DEL FLUJO

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 0: TRIGGER                                                              │
│                                                                               │
│  [Cron: cada N min] ──→ [Generar cycle_id] ──→ [Check Mercado Abierto]      │
│                                                    │                          │
│                              cerrado: abortar ←───┤                          │
│                              abierto: continuar ──→│                          │
└────────────────────────────────────────────────────┬─────────────────────────┘
                                                      │
                                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 1: RECOLECCIÓN DE DATOS (paralelo)                                      │
│                                                                               │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │  Precios MT5      │  │  Noticias Jina   │  │  Calendario Económico    │   │
│  │  EURUSD/GBPUSD/  │  │  (Forex News)    │  │  (Jina Calendar)         │   │
│  │  USDJPY/USDCHF   │  │                  │  │                          │   │
│  └────────┬─────────┘  └────────┬─────────┘  └────────────┬─────────────┘   │
│           │                     │                          │                  │
│           └─────────────────────┴──────────────────────────┘                 │
│                                 │                                             │
│                    [Merge: market_snapshot]                                   │
│                                                                               │
│  PARALELO INDEPENDIENTE:                                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │  Posiciones MT5 (abiertas + pendientes)                               │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────┬────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 2: VALIDACIÓN DE CONTEXTO  →  FastAPI POST /v1/context/validate        │
│                                                                               │
│  Input:  { cycle_id, market_snapshot, positions }                            │
│  Output: { valid: bool, reason, session, volatility_ok, news_risk }          │
│                                                                               │
│  Verifica:                                                                   │
│  · Sesión de mercado activa (Londres / NY / Overlap)                         │
│  · Sin noticias HIGH impact en próximos 30 minutos                           │
│  · Volatilidad ATR-1H dentro de rango operable                               │
│  · Exposición actual no supera límite de riesgo                              │
│  · cycle_id no fue procesado anteriormente (idempotencia)                    │
│                                                                               │
│  valid=false  ──→  [Log Skip] ──→ FIN                                        │
└─────────────────────────────────────┬────────────────────────────────────────┘
                                       │ valid=true
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 3: ANÁLISIS TÉCNICO  →  FastAPI POST /v1/analysis/pairs                │
│                                                                               │
│  Input:  { cycle_id, candles_per_pair }                                      │
│  Output: { best_pair, price, scores[], technical{} }                         │
│                                                                               │
│  Proceso (determinista, sin LLM):                                            │
│  · Calcular RSI, SMA-9/20/50/200, ATR por par                               │
│  · Detectar S/R, FVG, BOS/CHoCH                                             │
│  · Score por fortaleza de divisa + técnicos                                  │
│  · Seleccionar best_pair con criterios objetivos                             │
│  · Precio del best_pair = fuente única de verdad del ciclo                  │
└─────────────────────────────────────┬────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 4: SEÑALES LLM (paralelo en n8n)                                       │
│                                                                               │
│  ┌─────────────────┐  ┌───────────────────┐  ┌────────────────────────┐     │
│  │  Agente Técnico │  │  Agente Fundamental│  │  Agente Sentimiento    │     │
│  │  (Mistral/Local)│  │  (Mistral/Local)   │  │  (Mistral/Local)       │     │
│  └────────┬────────┘  └─────────┬──────────┘  └──────────┬─────────────┘     │
│           │                     │                         │                   │
│           └─────────────────────┴─────────────────────────┘                  │
│                                 │                                             │
│                        [Merge + Parse JSON]                                   │
│                Output: { signals[], confidence[] }                            │
│                                                                               │
│  Regla: si un agente devuelve JSON malformado → signal=neutral, conf=0        │
│  Los LLM son asesores. Su output entra en el Voting Engine, no ejecuta solo. │
└─────────────────────────────────────┬────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 5: DECISIÓN Y RIESGO  →  FastAPI POST /v1/risk/evaluate                │
│                                                                               │
│  Input:  { cycle_id, best_pair, price, technical, llm_signals, positions }   │
│  Output: { action, entry, sl, tp, volume, confidence, reason }               │
│                                                                               │
│  Proceso (100% determinista):                                                │
│  · Voting engine: mayoría ponderada de señales LLM                          │
│  · Validar entry/SL/TP contra símbolo correcto                              │
│  · Calcular volumen: capital × 1% / (sl_pips × pip_value)                   │
│  · Verificar R/R ≥ 1.5                                                       │
│  · Rechazar si ya hay posición abierta en el par                             │
│  · Rechazar si confidence < 0.7 o señal = neutral                            │
│                                                                               │
│  action=skip  ──→  [Log Skip] ──→ FIN                                        │
└─────────────────────────────────────┬────────────────────────────────────────┘
                                       │ action=buy|sell
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 6: PRE-EJECUCIÓN (última guardia)  →  FastAPI POST /v1/order/prepare   │
│                                                                               │
│  Input:  { cycle_id, symbol, type, entry, sl, tp, volume }                   │
│  Output: { approved: bool, order_payload, rejection_reason }                 │
│                                                                               │
│  Validaciones finales:                                                       │
│  · Precio entry dentro del rango válido del símbolo                          │
│  · BUY: sl < entry < tp  |  SELL: tp < entry < sl                           │
│  · SL no menor de 5 pips (50 pips para JPY)                                 │
│  · Volumen en [0.01, 0.05]                                                   │
│  · No existe orden duplicada (mismo symbol + entry ± 1 pip)                 │
│  · Registra en DB como "pending_execution" ANTES de enviar a MT5            │
│                                                                               │
│  approved=false  ──→  [Log Rechazo + Telegram] ──→ FIN                       │
└─────────────────────────────────────┬────────────────────────────────────────┘
                                       │ approved=true
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 7: EJECUCIÓN MT5                                                        │
│                                                                               │
│  [n8n HTTP] POST /mt5/order  (payload firmado por FastAPI)                   │
│  [n8n HTTP] GET  /mt5/order/{id}  ──→ confirmación obligatoria               │
│                                                                               │
│  Si MT5 rechaza o no confirma en 10s:                                        │
│  ──→ [Log Error] + [Telegram PRIORIDAD ALTA] + [Abortar]                     │
└─────────────────────────────────────┬────────────────────────────────────────┘
                                       │ status=PLACED|FILLED
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CAPA 8: AUDITORÍA Y NOTIFICACIÓN                                             │
│                                                                               │
│  FastAPI POST /v1/audit/log                                                   │
│  Registra: cycle_id, todos los inputs, decisión, order_id, fill_price,       │
│            timestamp de cada etapa. Append-only.                              │
│                                                                               │
│  [Telegram] Notificación estructurada:                                       │
│  · Par, tipo, entry, SL, TP, volumen                                         │
│  · Confianza LLM, motivo de decisión, cycle_id                              │
│                                                                               │
│  PARALELO INDEPENDIENTE: Limpieza de órdenes antiguas (> 48h)               │
│  FastAPI POST /v1/orders/cleanup                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## ESTRUCTURA DE WORKFLOWS n8n

```
WF-01: Main Orchestrator          ← único workflow con trigger Cron
  ├── WF-02: Market Data           ← subworkflow paralelo
  │     ├── WF-02a: EURUSD Fetcher
  │     ├── WF-02b: GBPUSD Fetcher
  │     ├── WF-02c: USDJPY Fetcher
  │     ├── WF-02d: USDCHF Fetcher
  │     ├── WF-02e: News Fetcher
  │     └── WF-02f: Calendar Fetcher
  ├── WF-03: MT5 Position Reader   ← subworkflow paralelo
  ├── WF-04: LLM Agents            ← subworkflow paralelo
  │     ├── WF-04a: Agente Técnico
  │     ├── WF-04b: Agente Fundamental
  │     └── WF-04c: Agente Sentimiento
  └── WF-05: Order Cleanup         ← subworkflow independiente (no bloquea main)
```

**Regla de modularidad:** Cada subworkflow tiene contrato de input/output documentado.
Ningún subworkflow accede a `$node["X"]` de otro workflow.

---

## CONTRATOS DE ENDPOINTS FASTAPI

```
POST /v1/context/validate
  In:  { cycle_id, prices, news, calendar, positions }
  Out: { valid, reason, session, risk_flags[] }

POST /v1/analysis/pairs
  In:  { cycle_id, candles: { EURUSD:[], GBPUSD:[], USDJPY:[], USDCHF:[] } }
  Out: { best_pair, price, scores, technical }

POST /v1/risk/evaluate
  In:  { cycle_id, best_pair, price, technical, llm_signals[], positions[] }
  Out: { action, entry, sl, tp, volume, confidence, reason }

POST /v1/order/prepare
  In:  { cycle_id, symbol, type, entry, sl, tp, volume }
  Out: { approved, order_payload, rejection_reason }

POST /v1/order/confirm
  In:  { cycle_id, mt5_order_id, status, fill_price }
  Out: { logged }

POST /v1/audit/log
  In:  { cycle_id, event, data }
  Out: { id }

POST /v1/orders/cleanup
  In:  { max_age_hours: 48 }
  Out: { cancelled[], errors[] }
```

---

## REGLAS DE OPERACIÓN

**OP-01:** Operar solo durante sesiones activas:
- Londres: 08:00–17:00 UTC
- Nueva York: 13:00–22:00 UTC
- Overlap Londres-NY: 13:00–17:00 UTC (sesión preferida)

**OP-02:** Máximo 1 orden abierta (pendiente o activa) por par en cualquier momento.

**OP-03:** Máximo 2 órdenes abiertas en total, independientemente del par.

**OP-04:** No operar en los 30 minutos anteriores ni los 30 minutos posteriores a cualquier noticia de impacto ALTO del par operado.

**OP-05:** No operar si el ATR-1H del par es mayor que 2 veces el ATR-1H promedio de los últimos 20 períodos (mercado sobrevolatilizado).

**OP-06:** Cada ciclo de decisión tiene un `cycle_id` único generado al inicio. El ciclo no se repite ni reutiliza.

**OP-07:** Volumen por orden: entre 0.01 y 0.05 lots. Fuera de este rango se rechaza la orden.

**OP-08:** Riesgo máximo por operación: 1% del capital disponible. Rango aceptable: 0.5%–1%. No superar 1% en ningún caso.

---

## REGLAS DE VALIDACIÓN

**VAL-01:** `entry`, `sl` y `tp` deben ser números positivos, distintos entre sí, y mayores que cero.

**VAL-02:** Dirección coherente con el tipo de orden:
- BUY: `sl < entry < tp`
- SELL: `tp < entry < sl`

Si no se cumple, la orden se rechaza.

**VAL-03:** El precio de `entry` debe estar dentro del rango esperado del símbolo.
Rangos de referencia (guardarraíles duros, revisables trimestralmente):
- EURUSD: [1.05, 1.20]
- GBPUSD: [1.20, 1.45]
- USDJPY: [130.0, 175.0]
- USDCHF: [0.82, 1.05]

Nota: si el mercado se acerca a los límites, revisar y actualizar antes de que generen falsos positivos. Como alternativa futura: validación relativa (±3% del precio medio de los últimos 5 días hábiles).

**VAL-04:** Stop Loss mínimo: 5 pips para pares no-JPY. 50 pips para pares JPY (USDJPY). Stop Loss máximo: 50 pips (500 pips para JPY). Fuera de estos límites se rechaza la orden.

**VAL-05:** Ratio Riesgo/Beneficio mínimo: 1.5. Es decir, `tp_pips / sl_pips >= 1.5`. Si el ratio es menor, la orden se rechaza.

**VAL-06:** El volumen calculado debe estar en el rango [0.01, 0.05] lots. Si el cálculo basado en el 1% de riesgo produce un valor fuera de este rango, se trunca al límite correspondiente y se registra en el log.

**VAL-07:** El `cycle_id` no debe existir en la base de datos con estado "executed" o "pending_execution". Si existe, la orden se rechaza silenciosamente y solo se registra en el log (comportamiento esperado de idempotencia, no es un error).

**VAL-08:** No debe existir ninguna orden pendiente para el mismo par con el mismo `cycle_id` o con el mismo precio de entrada (±1 pip de tolerancia). Si existe, se rechaza y se alerta por Telegram.

**VAL-09:** La confianza de la señal LLM consolidada debe ser >= 0.7 (70%). Por debajo de ese umbral, la señal se trata como neutral y el ciclo termina con `action=skip`.

**VAL-10:** El Voting Engine requiere que al menos 2 de los 3 agentes LLM emitan la misma señal direccional (BUY o SELL). Si no se alcanza mayoría, `action=skip`.

---

## REGLAS DE SEGURIDAD

**SEC-01:** Las credenciales de MT5 (login, password, server) nunca salen del servidor donde corre el EA o el script de conexión a MT5. FastAPI las gestiona internamente. n8n nunca las ve.

**SEC-02:** FastAPI solo es accesible desde la red interna. No debe estar expuesto a Internet directamente. Si se necesita acceso externo, se hace mediante VPN o túnel privado.

**SEC-03:** Cada request de n8n a FastAPI incluye dos headers obligatorios: `X-Cycle-ID` con el `cycle_id` del ciclo actual, y `X-Internal-Token` con un token fijo de autenticación interna. Sin estos headers, FastAPI rechaza el request con 401.

**SEC-04:** El `order_payload` que FastAPI entrega a n8n va firmado con HMAC-SHA256 usando una clave secreta compartida entre FastAPI y el EA. El EA verifica la firma antes de procesar cualquier orden. Una orden sin firma válida se descarta.

**SEC-05:** Los logs de auditoría son append-only. No existe endpoint de modificación ni borrado. Cualquier intento de modificar registros existentes debe ser bloqueado a nivel de base de datos.

**SEC-06:** Los prompts enviados a los LLM no incluyen credenciales, número de cuenta, capital real, ni ningún dato sensible. Solo incluyen datos de mercado estructurados (precios, indicadores, noticias).

**SEC-07:** Rate limiting en FastAPI: máximo 10 requests por minuto por origen (IP o token). Los endpoints de riesgo (`/v1/risk/evaluate`) y de preparación de orden (`/v1/order/prepare`) tienen límite independiente más restrictivo.

**SEC-08:** Si FastAPI no responde en 5 segundos, n8n aborta el ciclo completo y registra el evento como `"fastapi_timeout"`. No se asume ningún valor por defecto ni se continúa el flujo.

---

## REGLAS DE RECUPERACIÓN ANTE FALLOS

**REC-01 — Fallo en recolección de datos de precios:**
- Si falla 1 de los 4 fetchers de precio: usar el último precio conocido si tiene menos de 5 minutos de antigüedad.
- Si fallan 2 o más fetchers: abortar el ciclo con motivo `"data_fetch_error"`. No operar con datos incompletos.
- Si fallan los fetchers de noticias o calendario: continuar el ciclo pero activar flags de riesgo conservadores (tratar como si hubiera noticias de impacto alto pendientes).

**REC-02 — Fallo en FastAPI (cualquier endpoint):**
- Si FastAPI no responde o devuelve error 5xx: abortar el ciclo inmediatamente.
- No usar valores por defecto ni continuar con datos parciales.
- Motivo: FastAPI es la capa crítica de contexto y riesgo. Un fallo aquí no es recuperable en el mismo ciclo.
- El cron reintentará en el próximo intervalo programado.

**REC-03 — Fallo en un agente LLM:**
- Si 1 agente falla (timeout, JSON malformado, error de modelo): ese agente emite señal `neutral` con `confidence=0`.
- Si fallan 2 o más agentes: el Voting Engine no puede alcanzar mayoría de 2 de 3. El ciclo termina con `action=skip`.
- No reintentar en el mismo ciclo. El cron reintentará en el próximo intervalo.

**REC-04 — Fallo en ejecución MT5:**
- Si MT5 rechaza la orden (precio fuera de mercado, capital insuficiente, símbolo no disponible): registrar en DB como `"rejected_by_broker"` y enviar alerta por Telegram con el motivo.
- Si MT5 no confirma la recepción de la orden en 10 segundos: marcar el estado como `"unconfirmed"` y enviar alerta por Telegram. No reintentar automáticamente. Requiere verificación manual.

**REC-05 — Fallo en confirmación post-ejecución:**
- Si la confirmación no llega pero el comando de ejecución fue enviado: el sistema no sabe si la orden se colocó o no.
- Acción: enviar alerta Telegram con prioridad máxima indicando `"manual check required"` con el `cycle_id` y los datos de la orden.
- El sistema no opera en el siguiente ciclo hasta que un operador confirme el estado manualmente.

**REC-06 — Detección de duplicado:**
- Si el `cycle_id` ya existe en DB con estado ejecutado: ignorar silenciosamente. Solo registrar en log. No enviar alerta (es comportamiento esperado de idempotencia).
- Si se detecta una orden duplicada por par y precio (independiente del `cycle_id`): rechazar la orden y enviar alerta por Telegram.

**REC-07 — Precio fuera de rango:**
- Rechazar el precio. No usar valores por defecto para continuar con la ejecución.
- Abortar el ciclo con motivo `"invalid_price_data"`.
- Si este error ocurre 3 veces consecutivas en ciclos seguidos: enviar alerta Telegram indicando posible problema con el feed de precios. No operar hasta revisión manual.

---

## MEJORAS RESPECTO AL DISEÑO v3.x

| # | Área | v3.x | v4.0 |
|---|------|-------|-------|
| 1 | Precio del par | Bug: EURUSD asignado a todos los pares | FastAPI es fuente única de verdad: precio siempre corresponde al símbolo correcto |
| 2 | Idempotencia | Ninguna | `cycle_id` en todos los endpoints, verificado en DB |
| 3 | Duplicados | Array vacío en JS (frágil, sin registro) | FastAPI con lookup en DB por `(cycle_id, symbol, entry)` |
| 4 | Validación de riesgo | JavaScript en nodo n8n, no testeable | FastAPI con tests unitarios, versionado independiente |
| 5 | Análisis técnico | Mezclado con preparación de prompts LLM | FastAPI determinista, sin LLM, separado del flujo de señales |
| 6 | Fallo LLM | Bloquea o produce valores por defecto | Señal neutral explícita, el Voting Engine lo gestiona |
| 7 | Trazabilidad | Logs de texto dispersos en n8n | `cycle_id` unificado + DB de auditoría append-only |
| 8 | Recuperación | Fallos silenciosos (`try-catch` genérico con defaults) | Fallos explícitos con categorías y acciones definidas |
| 9 | Rangos de precio | Hardcoded en 3+ nodos JS distintos | Fuente única en FastAPI, revisables trimestralmente |
| 10 | Modularidad | Nodos con múltiples responsabilidades mezcladas | Un nodo n8n = una responsabilidad. Un endpoint FastAPI = una validación |
| 11 | Confirmación MT5 | Fire-and-forget (sin verificación) | Confirmación obligatoria con timeout y alerta si falla |
| 12 | Limpieza de órdenes | En flujo principal (bloquea ejecución) | Subworkflow paralelo e independiente |
| 13 | Agente Estadístico | LLM para cálculos deterministas (Sharpe, volumen) | Movido a FastAPI como cálculo puro, sin LLM |
| 14 | Riesgo por operación | 2% (excesivo para cuenta pequeña) | 1% fijo (rango 0.5%–1%) |

---

## LO QUE SE MANTIENE DEL DISEÑO ACTUAL

- Subworkflows de fetchers de precio (EURUSD, GBPUSD, USDJPY, USDCHF)
- Agentes LLM de Jetson (Técnico, Fundamental, Sentimiento) — se ajustan los contratos de input/output
- Notificación por Telegram
- Trigger Cron
- Modelo de 4 pares (EURUSD, GBPUSD, USDJPY, USDCHF)
- Fuente de noticias Jina y calendario económico Jina

---

## DECISIONES DE INFRAESTRUCTURA (confirmadas)

| Decisión | Respuesta | Implicaciones |
|----------|-----------|---------------|
| **FastAPI** | Contenedor Docker separado, mismo host, red interna Docker Compose | FastAPI y n8n en la misma `docker network`. n8n llama a FastAPI por nombre de servicio, no por IP. No se expone puerto al exterior. |
| **Conexión MT5** | Socket TCP | FastAPI mantiene una conexión socket persistente al EA. No hay overhead HTTP por orden. El EA debe estar corriendo con el socket abierto antes de que FastAPI intente conectar. |
| **Persistencia idempotencia** | PostgreSQL | PostgreSQL compartido entre FastAPI y el sistema de auditoría. Una sola instancia, misma red Docker. `cycle_id` se registra en tabla `cycles` con estado y timestamp. |
| **Capital base** | Equity dinámico leído desde MT5 | En cada ciclo, FastAPI consulta el equity actual al EA vía socket antes de calcular el volumen. El 1% se calcula sobre `equity_actual`, no sobre un valor fijo. |
| **Frecuencia del cron** | Cada 5 minutos | El ciclo completo (datos → validación → análisis → LLM → riesgo → orden) debe completarse en menos de 5 minutos. Si supera ese tiempo, el siguiente ciclo ya está disparando. Ver nota de timing abajo. |

### Nota de timing (ciclo de 5 minutos)

Con ciclo cada 5 minutos, el presupuesto de tiempo por etapa es:

| Etapa | Tiempo máximo |
|-------|--------------|
| Recolección de datos (paralelo) | 8s |
| FastAPI context/validate | 1s |
| FastAPI analysis/pairs | 2s |
| LLM Agentes x3 (paralelo) | 25s |
| FastAPI risk/evaluate | 1s |
| FastAPI order/prepare | 1s |
| Ejecución MT5 + confirmación | 5s |
| Auditoría + Telegram | 2s |
| **Total máximo** | **~45s** |

El ciclo tiene margen amplio. El cuello de botella es el LLM (25s). Si algún agente supera 30s, se le aplica timeout y emite señal neutral.

---

## CONFIGURACIÓN DOCKER COMPOSE (esquema)

```yaml
services:
  n8n:
    # configuración existente
    networks:
      - trading-net

  fastapi:
    build: ./fastapi
    networks:
      - trading-net
    environment:
      - DATABASE_URL=postgresql://user:pass@postgres:5432/trading
      - MT5_SOCKET_HOST=host-gateway   # MT5 corre en el host, no en Docker
      - MT5_SOCKET_PORT=9999
      - INTERNAL_TOKEN=${INTERNAL_TOKEN}
    ports: []   # sin puertos expuestos al exterior

  postgres:
    image: postgres:16
    networks:
      - trading-net
    volumes:
      - postgres_data:/var/lib/postgresql/data

networks:
  trading-net:
    driver: bridge
```

Nota: MT5/EA corre en Windows (host o VM), no en Docker. FastAPI se conecta al socket del EA usando `host-gateway` (IP del host desde dentro del contenedor) o una IP estática de la máquina Windows.

---

## ESQUEMA DE BASE DE DATOS (PostgreSQL)

```sql
-- Idempotencia de ciclos
CREATE TABLE cycles (
    cycle_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL,   -- 'processing' | 'skipped' | 'executed' | 'rejected' | 'error'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    skip_reason  TEXT,
    pair         TEXT,
    action       TEXT
);

-- Órdenes enviadas
CREATE TABLE orders (
    id           SERIAL PRIMARY KEY,
    cycle_id     TEXT NOT NULL REFERENCES cycles(cycle_id),
    symbol       TEXT NOT NULL,
    type         TEXT NOT NULL,    -- 'BUY' | 'SELL'
    entry        NUMERIC(10,5),
    sl           NUMERIC(10,5),
    tp           NUMERIC(10,5),
    volume       NUMERIC(6,3),
    mt5_order_id TEXT,
    status       TEXT NOT NULL,    -- 'pending' | 'placed' | 'filled' | 'rejected' | 'unconfirmed'
    fill_price   NUMERIC(10,5),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ
);

-- Auditoría append-only
CREATE TABLE audit_log (
    id         BIGSERIAL PRIMARY KEY,
    cycle_id   TEXT NOT NULL,
    event      TEXT NOT NULL,
    data       JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Índice para detección de duplicados
CREATE INDEX idx_orders_symbol_entry ON orders(symbol, entry) 
    WHERE status IN ('pending', 'placed');
```

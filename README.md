# Sistema de Trading Automatizado

Sistema de trading automatizado que conecta un Expert Advisor (MQL5) con MetaTrader 5 a través de una API FastAPI. El EA lee señales del indicador "Accurate Buy Sell System", aplica filtros técnicos (EMA, ADX) y envía las señales a FastAPI, que gestiona el sizing, validación, filtro de noticias fundamentales y ejecución de órdenes vía el MetaTrader MCP Server.

Pipeline directo: **EA (MQL5) → FastAPI → MetaTrader MCP Server**.
Sin orquestador externo, sin LLM.

## Arquitectura

```
MT5: Accurate Buy Sell System (buffers 2=BUY, 3=SELL)
        │
        ▼
EA AccurateBuySellBridge.mq5        cada 10s (OnTimer)
        │  ┌─ ManageTrailing()      trailing SL por cierre de vela
        │  ├─ CheckNewsExit()       POST /v1/smc/news-check (cada ~5 min)
        │  ├─ CheckEmaExit()        cierre por cruce EMA (con buffer) / ADX agotado
        │  └─ SendCurrentSignal()   POST /v1/smc/signal
        │
        │  Filtros del EA (vela cerrada, índice 1):
        │  ├─ EMA 50 H1:  BUY solo si close > EMA, SELL solo si close < EMA
        │  ├─ EMA 50 H4:  confirmación timeframe superior (desactivable)
        │  ├─ ADX(14) > 20: descarta mercado lateral
        │  └─ Deduplicación por signal_id (ABS_<symbol>_<dir>_<bartime>)
        │
        ▼
FastAPI (Python)                    POST /v1/smc/signal
        │  1. Símbolo soportado
        │  2. Precio presente en la señal
        │  3. Cooldown por símbolo (60 min)
        │  4. Filtro de noticias fundamentales (±15 min)
        │  5. SL = distancia flecha × SL_MULT, sin TP (salida por señal/EMA)
        │  6. Cerrar posición existente si hay flip de señal
        │  7. Validación geométrica + rango + duplicados (order_manager)
        │  8. Enviar orden a mercado (market order)
        │
        ▼
MetaTrader MCP Server               http://<MT5_HTTP_URL>/api/v1/...
        └─ REST API: precios, velas, órdenes, posiciones
```

## Gestión de Posiciones (EA)

### Trailing Stop por cierre de vela

En cada cierre de vela H1, el SL se desplaza proporcionalmente a los pips ganados
respecto al precio de entrada. Solo se mueve en dirección favorable, nunca en contra.

**Ejemplo BUY** — entry 1.32414, SL base 1.32041:
```
Vela 1 cierra en 1.32460 (+46 pips sobre entry) → SL sube 46 pips → 1.32087
Vela 2 cierra en 1.32390 (bajo entry)           → sin movimiento
Vela 3 cierra en 1.32500 (+86 pips sobre entry) → SL sube a 1.32127
```

El SL base se lee directamente de MT5 al abrirse la posición (refleja el SL real
calculado por FastAPI). No hay fase de breakeven separada: el SL cruza
automáticamente el precio de entrada cuando el profit supera la distancia original.

### Salida por EMA / ADX

El EA cierra posiciones vía `POST /v1/smc/close` con tolerancia configurable:

- **EMA cross + buffer**: el cierre debe estar `EmaExitBuffer` pips al otro lado de
  la EMA (default 10). Filtra roces puntuales que no son reversiones reales.
- **Confirmación de 2 velas** (`EmaExitConfirm2=true`): se requieren dos cierres
  consecutivos cruzando la EMA con buffer para ejecutar el cierre.
- **ADX < 20**: tendencia agotada (sin buffer, cierre inmediato)
- **ADX > 50**: tendencia sobreextendida (sin buffer, cierre inmediato)

**Flujo de salida EMA con ambas protecciones activas:**
```
Vela N+1: cierra 3 pips bajo EMA  → buffer no alcanzado, ignora
Vela N+2: cierra 12 pips bajo EMA → cruza buffer (1ª confirmación), espera
Vela N+3: cierra 15 pips bajo EMA → 2ª confirmación → CIERRA
    ó
Vela N+3: recupera y cierra sobre EMA → resetea contador, posición sigue abierta
```

## Filtro de Noticias Fundamentales

Bloquea operaciones y cierra posiciones alrededor de noticias de alto impacto (3 estrellas).

- **Fuente**: Forex Factory (mirror JSON `nfs.faireconomy.media`)
- **Cache**: eventos High Impact descargados cada 4h
- **Ventana de exclusión**: ±`NEWS_BLACKOUT_MINUTES` (default 15 min)
- **Mapeo automático**: noticia USD → afecta EURUSD, GBPUSD, USDJPY, XAUUSD, etc.

| Mecanismo | Dónde | Qué hace |
|-----------|-------|----------|
| Bloqueo de nuevas entradas | `simple_pipeline.py` paso 4 | Si hay blackout → `skip: news_blackout` |
| Cierre proactivo | `POST /v1/smc/news-check` | Cierra posiciones abiertas en pares afectados |

**Ejemplo — noticias a las 15:30:**
```
15:15  EA llama /news-check → FastAPI detecta "Core PCE" en 15 min → cierra posiciones USD
15:15  Señal BUY XAUUSD llega → bloqueada: "news_blackout:Core PCE Price Index"
15:45  Ventana expira → operativa normal
```

## Health Check del MCP Server

FastAPI ejecuta un loop en background que hace polling al MCP Server cada 30s.
Solo logea cuando hay cambio de estado:

```
[MCP-HEALTH] MT5 DISCONNECTED — Not connected to MetaTrader 5 terminal.
[MCP-HEALTH] MT5 CONNECTED — equity=97946.31
```

`GET /health` devuelve `"status": "degraded"` si el MCP no responde.

## Sizing y Riesgo

- **SL**: `distancia flecha × SL_MULT` (default 1.5). Mínimo: max(3 pips, 3× spread)
- **TP**: 0 — sin take profit fijo; salida por señal contraria, EMA/ADX, o news
- **Volume**: `SL_RISK_USD / (sl_pips × pip_value_per_lot)`, clamped a [MIN_VOLUME, MAX_VOLUME]
- **Órdenes**: market order directo (no pending)

**Ejemplo GBPUSD con SL_MULT=1.5:**
```
Arrow en 1.32165, entry en 1.32414 → base_dist = 24.9 pips
sl_dist = 24.9 × 1.5 = 37.4 pips → SL en 1.32040
volume  = $15 / (37.4 pips × $10) = 0.04 lotes
```

## Parámetros del EA (configurables sin recompilar)

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `FastAPI_URL` | — | URL del servidor FastAPI |
| `InternalToken` | — | Token de autenticación |
| `SendIntervalSec` | 10 | Frecuencia del timer en segundos |
| `EmaPeriod` | 50 | Período de la EMA |
| `AdxPeriod` | 14 | Período del ADX |
| `AdxMinLevel` | 20.0 | ADX mínimo para considerar tendencia |
| `UseEmaH4Filter` | false | Activar filtro EMA en H4 |
| `EmaExitBuffer` | 10 | Pips bajo la EMA para considerar cruce válido |
| `EmaExitConfirm2` | true | Exigir 2 velas consecutivas para cerrar por EMA |
| `DiagMode` | false | Logs detallados de filtros |

## Configuración (.env)

```
# FastAPI / seguridad
INTERNAL_TOKEN=...
HMAC_SECRET=...
POSTGRES_PASSWORD=...

# MetaTrader MCP Server
MT5_HTTP_URL=http://YOUR_MT5_MCP_IP:8000

# Pipeline
SIMPLE_PIPELINE_ENABLED=true
SIGNAL_COOLDOWN_MINUTES=60

# Sizing
SL_RISK_USD=15.0
SL_MULT=1.5        # multiplicador sobre distancia flecha (menor = SL más cercano, más volumen)
MIN_VOLUME=0.01
MAX_VOLUME=0.50

# Noticias
NEWS_FILTER_ENABLED=true
NEWS_BLACKOUT_MINUTES=15
```

## Protecciones

| # | Guard | Dónde |
|---|-------|-------|
| 1 | Filtros técnicos (EMA H1, EMA H4 opcional, ADX) | EA |
| 2 | Deduplicación por signal_id | EA + FastAPI |
| 3 | Cooldown por símbolo (60 min) | FastAPI |
| 4 | Blackout noticias fundamentales (±15 min) | FastAPI |
| 5 | Cierre proactivo pre-noticias | FastAPI (news-check) |
| 6 | Salida EMA con buffer + confirmación 2 velas | EA |
| 7 | Geometría (sl < entry < tp) | order_manager |
| 8 | Rango de precio válido | order_manager |
| 9 | Duplicado ±1 pip | order_manager |
| 10 | SL mínimo/máximo en pips | order_manager |

## Auditoría

Cada decisión queda en `audit_log` con `cycle_id`:

| Evento | Significado |
|--------|-------------|
| `simple_order_placed` | Orden enviada a MT5 |
| `simple_rejected` | Rechazada por validación |
| `simple_skip` | Descartada (cooldown, news_blackout, no_price) |
| `simple_position_closed` | Posición cerrada por flip de señal |
| `simple_mt5_error` | MT5 rechazó la orden |
| `position_closed_ema` | Cerrada por cruce EMA / ADX |
| `position_closed_news` | Cerrada proactivamente por noticia |

## Ficheros

```
mql5/
  AccurateBuySellBridge.mq5       EA principal: señales, filtros, trailing, exits

fastapi/
  main.py                         App FastAPI + health check MCP en background
  config.py                       Settings (pydantic-settings, .env)
  routers/smc.py                  Endpoints: signal, close, news-check
  services/simple_pipeline.py     Pipeline: señal → orden (8 pasos)
  services/position_sizing.py     SL/TP a riesgo monetario fijo (SL_MULT)
  services/order_manager.py       Validación final + registro en DB
  services/mt5_client.py          Cliente HTTP para MetaTrader MCP Server
  services/news_filter.py         Filtro de noticias (Forex Factory)
```

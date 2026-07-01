# Sistema de Trading Automatizado

Sistema de trading automatizado que conecta Expert Advisors (MQL5) con MetaTrader 5
a través de una API FastAPI. Los EAs detectan señales basadas en estructura de mercado
y liquidez usando indicadores nativos de MT5, y envían las señales a FastAPI, que
gestiona el sizing, validación, filtro de noticias y ejecución vía el MetaTrader MCP Server.

Pipeline directo: **EA (MQL5) → FastAPI → MetaTrader MCP Server**.
Sin orquestador externo, sin LLM.

## Expert Advisors

El sistema tiene dos EAs especializados, cada uno con su propia estrategia de entrada:

| EA | Estrategia | Pares |
|----|-----------|-------|
| `StructureBreakBridge.mq5` | Rotura de estructura + volumen | EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, NZDUSD |
| `LiquidityGrabBridge.mq5` | Captura de liquidez (stop hunt + reversión) | XAUUSD |

Ambos comparten la misma lógica de gestión: trailing stop por cierre de vela, salida
por EMA/ADX y filtro de noticias fundamentales.

## Arquitectura

```
StructureBreakBridge.mq5          LiquidityGrabBridge.mq5
(pares FX, H1)                    (XAUUSD, H1)
        │                                 │
        └──────────────┬──────────────────┘
                       ▼
            cada 10s (OnTimer)
            ├─ ManageTrailing()    trailing SL por cierre de vela
            ├─ CheckNewsExit()     POST /v1/smc/news-check (cada ~5 min)
            ├─ CheckEmaExit()      cierre por cruce EMA / ADX agotado
            └─ SendCurrentSignal() POST /v1/smc/signal
                       │
                       ▼
        FastAPI (Python)           POST /v1/smc/signal
            │  1. Símbolo soportado
            │  2. Precio presente en la señal
            │  3. Cooldown por símbolo (60 min)
            │  4. Filtro de noticias (vela H1 completa bloqueada)
            │  5. SL = base_dist × SL_MULT, sin TP
            │  6. Cerrar posición existente si hay flip de señal
            │  7. Validación geométrica + rango + duplicados
            │  8. Enviar orden a mercado
                       │
                       ▼
        MetaTrader MCP Server      http://<MT5_HTTP_URL>/api/v1/...
            └─ REST API: precios, velas, órdenes, posiciones
```

## Estrategias de Entrada

### StructureBreakBridge — Rotura de Estructura

Detecta cuando el precio cierra **por encima del máximo** (BUY) o **por debajo del mínimo**
(SELL) de las últimas N velas, confirmado por volumen elevado.

**Ventaja sobre indicadores externos**: la señal dispara al inicio del nuevo movimiento,
no al final. El gap entre señal y entrada es mínimo (solo la distancia desde el nivel
roto hasta el cierre de la vela de rotura).

```
Condiciones BUY:
  close[1] > max(high[2..N+1])     rotura del swing anterior
  volume[1] > avg(volume) × 1.5    participación institucional
  close[1] > EMA50[1]              tendencia alcista
  20 < ADX[1] < 50                 mercado en tendencia, no sobreextendido

SL anchor = low[1] de la vela de rotura
```

```
Ejemplo GBPUSD — swing previo en 1.32400, rotura a cierre 1.32450:
  entry   = 1.32452 (apertura siguiente vela, gap mínimo ~2 pips)
  base_dist = |1.32452 - 1.32200|  (entry - low de la vela de rotura)
  sl_dist = base_dist × 1.5
  SL      = entry - sl_dist
```

### LiquidityGrabBridge — Captura de Liquidez

Detecta "stop hunts": el precio hace un spike por encima/debajo de un swing previo
(barriendo los stops acumulados en ese nivel) y luego revierte cerrando al otro lado.
Indica que los institucionales recogieron liquidez y empujarán en la dirección contraria.

```
Condiciones SELL (bearish grab):
  high[1] > max(high[2..N+1])          spike sobre el swing previo
  close[1] < max(high[2..N+1])         cierra de vuelta por debajo
  (high[1] - close[1]) / range ≥ 0.5  mecha de rechazo ≥ 50% del rango
  (high[1] - prevHigh) ≥ 20 pips      spike mínimo significativo
  volume[1] > avg(volume) × 2.0        volumen elevado confirma institucional
  15 < ADX[1] < 65

SL anchor = high[1] (el extremo del spike — nivel que invalida el grab)
```

```
Ejemplo XAUUSD — swing previo en 2050, spike hasta 2065, cierre en 2042:
  bearish grab detectado: spike=15pts, wick=65%, vol=2.3×avg
  entry     = 2042 (BID en apertura siguiente vela)
  base_dist = |2042 - 2065| = 23 pts  (entry vs extremo del spike)
  sl_dist   = 23 × 1.5 = 34.5 pts
  SL        = 2042 + 34.5 = 2076.5    (por encima del spike)
```

## Gestión de Posiciones (común a ambos EAs)

### Trailing Stop por cierre de vela

En cada cierre de vela H1, el SL se desplaza proporcionalmente a los pips ganados
respecto al precio de entrada. Solo se mueve en dirección favorable, nunca en contra.

```
BUY entry 1.32452, SL base 1.32080:
  Vela 1 cierra 1.32500 (+4.8 pips) → SL sube 4.8 pips → 1.32128
  Vela 2 cierra 1.32430 (bajo entry) → sin movimiento
  Vela 3 cierra 1.32600 (+14.8 pips) → SL sube a 1.32228
```

Si la posición es cerrada por SL en MT5 (stop hit), el EA lo detecta
automáticamente en el siguiente tick y resetea el estado interno.

### Salida por EMA / ADX

El EA cierra posiciones vía `POST /v1/smc/close`:

- **EMA cross + buffer**: el cierre debe estar `EmaExitBuffer` pips al otro lado
  de la EMA. Filtra roces puntuales.
- **Confirmación 2 velas** (`EmaExitConfirm2=true`): dos cierres consecutivos
  cruzando la EMA con buffer. Si el precio recupera, se cancela la confirmación.
- **ADX < AdxMinLevel**: tendencia agotada → cierre inmediato
- **ADX > AdxMaxLevel**: tendencia sobreextendida → cierre inmediato

```
Vela N+1: cierra 3 pips bajo EMA  → buffer no alcanzado, ignora
Vela N+2: cierra 12 pips bajo EMA → 1ª confirmación, espera
Vela N+3: cierra 15 pips bajo EMA → 2ª confirmación → CIERRA
    ó
Vela N+3: recupera sobre EMA → resetea contador, posición sigue abierta
```

## Filtro de Noticias Fundamentales

Bloquea operaciones y cierra posiciones cuando hay una noticia High o Medium
impact en la vela H1 actual.

- **Fuente**: Forex Factory (mirror `nfs.faireconomy.media`) — impacto High + Medium
- **Cache**: eventos descargados y renovados cada 4h
- **Unidad de exclusión**: la vela H1 completa que contiene la noticia
- **Mapeo automático**: noticia USD → afecta todos los pares USD

| Mecanismo | Dónde | Qué hace |
|-----------|-------|----------|
| Bloqueo de nuevas entradas | `simple_pipeline.py` paso 4 | Noticia en vela actual → `skip: news_blackout` |
| Cierre proactivo | `POST /v1/smc/news-check` | Cierra posiciones en pares afectados |

```
Ejemplo — noticia a las 15:45 UTC:
  15:00  Vela H1 abre → FastAPI detecta noticia a las 15:45 (misma vela) → BLOQUEADO
  15:00  Señal BUY EURUSD → rechazada: news_blackout
  15:00  EA llama /news-check → cierra posiciones USD abiertas
  16:00  Vela siguiente sin noticias → operativa normal
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

- **SL**: `base_dist × SL_MULT` (default 1.5). `base_dist` = distancia entre entry y SL anchor
- **TP**: 0 — sin take profit fijo; salida por EMA/ADX, flip de señal o noticias
- **Volume**: `SL_RISK_USD / (sl_pips × pip_value_per_lot)`, clamped a [MIN_VOLUME, MAX_VOLUME]
- **Órdenes**: market order directo

## Parámetros de los EAs

### Comunes a ambos EAs

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `FastAPI_URL` | — | URL del servidor FastAPI (`http://<IP>:8090`) |
| `InternalToken` | — | Token de autenticación |
| `SendIntervalSec` | 10 | Frecuencia del timer (segundos) |
| `Timeframe` | H1 | Timeframe de operación |
| `EmaPeriod` | 50 | Período de la EMA |
| `AdxPeriod` | 14 | Período del ADX |
| `EmaExitBuffer` | 10 | Pips al otro lado de la EMA para salir |
| `EmaExitConfirm2` | true | Exigir 2 velas consecutivas para cerrar por EMA |
| `VolumePeriod` | 20 | Velas para calcular volumen medio |
| `DiagMode` | false | Logs detallados en el journal de MT5 |

### StructureBreakBridge (FX)

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `StructurePeriod` | 5 | Velas hacia atrás para determinar swing high/low |
| `VolumeMult` | 1.2 | Volumen mínimo = promedio × VolumeMult |
| `AdxMinLevel` | 20.0 | ADX mínimo de entrada |
| `AdxMaxLevel` | 50.0 | ADX máximo de entrada |

### LiquidityGrabBridge (XAUUSD)

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `LiquidityPeriod` | 10 | Velas para determinar el swing previo |
| `WickRatio` | 0.5 | Mecha mínima como fracción del rango total |
| `MinSpikePips` | 20 | Pips mínimos que debe superar el swing (tamaño del spike) |
| `VolumeMult` | 1.5 | Volumen mínimo = promedio × VolumeMult |
| `AdxMinLevel` | 15.0 | ADX mínimo (más permisivo para XAUUSD) |
| `AdxMaxLevel` | 65.0 | ADX máximo |
| `EmaExitBuffer` | 20 | Pips buffer EMA para salida (mayor en XAUUSD) |

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
SL_MULT=1.5
MIN_VOLUME=0.01
MAX_VOLUME=0.50

# Noticias (High + Medium impact)
NEWS_FILTER_ENABLED=true
```

## Protecciones

| # | Guard | Dónde |
|---|-------|-------|
| 1 | Volumen mínimo en vela de señal | EA (entry filter) |
| 2 | EMA H1 + ADX rango 20–50 | EA (entry filter) |
| 3 | No re-entrar en misma dirección con posición activa | EA |
| 4 | Detección de cierre externo por SL (auto-reset estado) | EA |
| 5 | Deduplicación por signal_id (una señal por vela) | EA + FastAPI |
| 6 | Cooldown por símbolo (60 min) | FastAPI |
| 7 | Blackout noticias — vela H1 completa bloqueada | FastAPI |
| 8 | Cierre proactivo pre-noticias | FastAPI (news-check) |
| 9 | Salida EMA con buffer + confirmación 2 velas | EA (exit) |
| 10 | Geometría (sl < entry para BUY, sl > entry para SELL) | order_manager |
| 11 | Duplicado ±1 pip | order_manager |
| 12 | SL mínimo/máximo en pips | order_manager |

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
  StructureBreakBridge.mq5    EA para FX: rotura de estructura + volumen
  LiquidityGrabBridge.mq5     EA para XAUUSD: captura de liquidez (stop hunt)
  AccurateBuySellBridge.mq5   EA legado (deprecado)

fastapi/
  main.py                     App FastAPI + health check MCP en background
  config.py                   Settings (pydantic-settings, .env)
  routers/smc.py              Endpoints: signal, close, news-check
  services/simple_pipeline.py Pipeline: señal → orden (8 pasos)
  services/position_sizing.py SL/TP a riesgo monetario fijo (SL_MULT)
  services/order_manager.py   Validación final + registro en DB
  services/mt5_client.py      Cliente HTTP para MetaTrader MCP Server
  services/news_filter.py     Filtro de noticias (Forex Factory High+Medium)
```

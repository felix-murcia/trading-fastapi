# Sistema de Trading Automatizado

Pipeline directo: **EA (MQL5) → FastAPI → MetaTrader MCP Server**.
Sin n8n, sin LLM, sin orquestador externo.

## Arquitectura

```
MT5: Accurate Buy Sell System (buffers 2=BUY, 3=SELL)
        │
        ▼
EA AccurateBuySellBridge.mq5        cada 10s (OnTimer)
        │  ┌─ ManageTrailing()      trailing stop + breakeven
        │  ├─ CheckNewsExit()       POST /v1/smc/news-check (cada ~5 min)
        │  ├─ CheckEmaExit()        cierre por cruce EMA / ADX agotado
        │  └─ SendCurrentSignal()   POST /v1/smc/signal
        │
        │  Filtros del EA (vela cerrada, índice 1):
        │  ├─ EMA 50 H1: BUY solo si close > EMA, SELL solo si close < EMA
        │  ├─ EMA 50 H4: confirmación de tendencia en timeframe superior
        │  ├─ ADX(14) > 20: descarta mercado lateral
        │  └─ Deduplicación por signal_id (ABS_<symbol>_<dir>_<bartime>)
        │
        ▼
FastAPI (Python)                    POST /v1/smc/signal
        │  1. Símbolo soportado
        │  2. Precio presente en la señal
        │  3. Cooldown por símbolo (60 min)
        │  4. Filtro de noticias fundamentales (±15 min)
        │  5. SL anclado a precio de flecha (fallback: apertura vela H1)
        │     SL emergencia = 3× distancia flecha, sin TP (salida por señal/EMA)
        │  6. Cerrar posición existente si hay flip de señal
        │  7. Validación geométrica + rango + duplicados (order_manager)
        │  8. Enviar orden
        │
        ▼
MetaTrader MCP Server               http://<MT5_HTTP_URL>/api/v1/...
        └─ REST API: precios, velas, órdenes, posiciones
```

## Filtro de Noticias Fundamentales

Bloquea operaciones y cierra posiciones alrededor de noticias de alto impacto (3 estrellas).

- **Fuente**: Forex Factory (mirror JSON `nfs.faireconomy.media`)
- **Cache**: eventos High Impact descargados cada 4h
- **Ventana de exclusión**: ±`NEWS_BLACKOUT_MINUTES` (default 15 min)
- **Mapeo automático**: noticia USD → afecta EURUSD, GBPUSD, USDJPY, XAUUSD, etc.

### Dos mecanismos complementarios

| Mecanismo | Dónde | Qué hace |
|-----------|-------|----------|
| Bloqueo de nuevas entradas | `simple_pipeline.py` paso 4 | Si hay blackout → `skip: news_blackout` |
| Cierre proactivo de posiciones | `POST /v1/smc/news-check` | Cierra posiciones abiertas en pares afectados |

El EA llama a `/news-check` cada ~5 min (30 ticks × 10s). Si el servidor cierra posiciones, el EA resetea su estado interno (trailing, breakeven).

### Ejemplo: noticias a las 15:30

```
15:15  EA llama /news-check → FastAPI detecta "Core PCE" en 15 min → cierra posiciones USD
15:15  Señal BUY XAUUSD llega → bloqueada: "news_blackout:Core PCE Price Index"
15:45  Ventana expira → operativa normal
```

### Variables de entorno

```
NEWS_FILTER_ENABLED=true        # activar/desactivar el filtro
NEWS_BLACKOUT_MINUTES=15        # ventana ±N minutos alrededor de la noticia
```

## Gestión de Posiciones (EA)

### Trailing Stop + Breakeven

Gestionado directamente por el EA en cada tick del timer:

1. **Breakeven**: cuando el profit alcanza `trailDist` (= distancia entry↔flecha), mueve SL a entry + 1 point
2. **Trailing**: una vez en breakeven, arrastra SL manteniendo `trailDist` por detrás del precio

### Salida por EMA / ADX

El EA cierra posiciones vía `POST /v1/smc/close` cuando:

- **EMA cross**: cierre de vela por debajo de EMA (BUY) o por encima (SELL)
- **ADX < 20**: tendencia agotada
- **ADX > 50**: tendencia sobreextendida

## Contrato del EA

### `POST /v1/smc/signal`

Header: `X-Internal-Token: <INTERNAL_TOKEN>`

```json
{
  "symbol":     "EURUSD",
  "entry_zone": true,
  "direction":  "buy",
  "zone_high":  1.15367,
  "zone_low":   1.15200,
  "timeframe":  "H1",
  "source":     "accurate_buy_sell",
  "signal_id":  "ABS_EURUSD_buy_1781172300"
}
```

- `zone_high` = precio de entrada (ask/bid actual)
- `zone_low` = precio de la flecha del indicador (ancla para SL)
- `signal_id` = clave de deduplicación (cycle_id)

### `POST /v1/smc/close`

```json
{
  "symbol": "EURUSD",
  "reason": "ema_cross"
}
```

### `POST /v1/smc/news-check`

Sin body requerido. Devuelve:

```json
{
  "action": "closed",
  "closed": [{"symbol": "XAUUSD", "event": "Core PCE Price Index", "minutes_until": 12.3}],
  "upcoming_events": [...]
}
```

## Sizing y Riesgo

- **SL**: distancia flecha × 3 (emergencia). Mínimo: max(3 pips, 3× spread)
- **TP**: 0 (sin take profit fijo; salida por señal contraria o EMA/ADX)
- **Volume**: `SL_RISK_USD / (sl_pips × pip_value_per_lot)`, clamped a [0.01, 0.50]
- **XAUUSD**: riesgo overrideado a $15

## Protecciones

| # | Guard | Dónde |
|---|-------|-------|
| 1 | Filtros técnicos (EMA H1/H4, ADX) | EA |
| 2 | Deduplicación por signal_id | EA + FastAPI |
| 3 | Cooldown por símbolo (60 min) | FastAPI |
| 4 | Blackout noticias fundamentales (±15 min) | FastAPI |
| 5 | Cierre proactivo pre-noticias | FastAPI (news-check) |
| 6 | Geometría (sl < entry < tp) | order_manager |
| 7 | Rango de precio válido | order_manager |
| 8 | Duplicado ±1 pip | order_manager |
| 9 | SL mínimo/máximo en pips | order_manager |

## Configuración (.env)

```
SIMPLE_PIPELINE_ENABLED=true
SIGNAL_COOLDOWN_MINUTES=60
SL_RISK_USD=15.0
SL_PCT=0.001
RR_MIN=1.0
MIN_VOLUME=0.01
MAX_VOLUME=0.50
NEWS_FILTER_ENABLED=true
NEWS_BLACKOUT_MINUTES=15
MT5_HTTP_URL=http://100.81.112.95:8000
```

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
  AccurateBuySellBridge.mq5       EA: señales, filtros, trailing, news-check

fastapi/
  main.py                         App FastAPI
  config.py                       Settings (pydantic-settings, .env)
  routers/smc.py                  Endpoints: signal, close, news-check
  services/simple_pipeline.py     Pipeline: señal → orden (8 pasos)
  services/position_sizing.py     SL/TP a riesgo monetario fijo
  services/order_manager.py       Validación final + registro en DB
  services/mt5_client.py          Cliente HTTP para MetaTrader MCP Server
  services/news_filter.py         Filtro de noticias (Forex Factory)
```

# Flujo Simple: señal → orden

Rediseño completo del sistema de trading. Sustituye al pipeline anterior
(WF-01..WF-07, agentes LLM, voting, debate), que queda despublicado.

## Principio

**El indicador es el único criterio.** Sin LLM, sin selección de "mejor par",
sin noticias, sin calendario, sin candles, sin consulta de precios: el precio
de entrada viene en la propia señal. Si no hay señal, no pasa nada.

## Arquitectura (push, sin cron)

```
MT5: Crystal Buy Sell Liquidity Indicator (objetos QT_L_B_* / QT_L_S_*)
        │  text=BUY/SELL + price
        ▼
EA SignalBridge ── POST /v1/smc/signal ──► FastAPI
                                              │ upsert smc_signals
                                              │ si SIMPLE_PIPELINE_ENABLED:
                                              ▼
                                  simple_pipeline.process_signal()
                                              │ 1. símbolo soportado
                                              │ 2. precio presente en la señal
                                              │ 3. cooldown por símbolo
                                              │ 4. SL/TP a dinero fijo (15€/30€)
                                              │ 5. order_manager.prepare (valida + registra)
                                              ▼
                                  mt5_client.place_order ──► MT5
```

n8n queda fuera del camino crítico. No hay workflow de orquestación.

## Contrato del EA (SignalBridge)

`POST /v1/smc/signal` con header `X-Internal-Token` y body:

```json
{
  "symbol":     "EURUSD",
  "entry_zone": true,
  "direction":  "buy",
  "zone_high":  1.15367,
  "zone_low":   1.15367,
  "timeframe":  "M5",
  "source":     "crystal_liquidity",
  "signal_id":  "QT_L_B_1781172300"
}
```

Mapeo desde el objeto del gráfico:
- `text=BUY → direction=buy` | `text=SELL → direction=sell`
- `price → zone_high` (es el precio de entrada de la orden)
- `name → signal_id` (clave de deduplicación)

Requisitos del EA:
- Enviar **solo la señal más reciente/activa** del gráfico, no los objetos
  históricos. Si enviara históricos, el cooldown limita el daño pero no lo elimina.
- `signal_id` garantiza que la misma señal **nunca** genera dos órdenes
  (idempotencia por `cycle_id = "smc_<signal_id>"`).

## Riesgo: dinero fijo, igual para todos los pares

- Volumen fijo: `FIXED_VOLUME` (default 0.05 lotes)
- SL: distancia tal que la pérdida = `SL_RISK_USD` (15)
- TP: `RR_MIN` × SL (2.0) → ganancia objetivo 30

Distancias resultantes con volumen 0.05:

| Par | pip value/lote | SL pips | TP pips |
|---|---|---|---|
| EURUSD | $10 | 30 | 60 |
| GBPUSD | $10 | 30 | 60 |
| USDJPY | ~$6.25 | 48 | 96 |
| USDCHF | ~$12.6 | 24 | 48 |
| XAUUSD | $10 | 30 ($3.00) | 60 ($6.00) |

## Protecciones

| # | Guard | Implementación |
|---|-------|----------------|
| 1 | Símbolo soportado | EURUSD, GBPUSD, USDJPY, USDCHF, XAUUSD |
| 2 | Precio presente | señal sin `zone_high` → skip |
| 3 | Misma señal no se repite | idempotencia `cycle_id` (signal_id) |
| 4 | Cooldown por símbolo | máx 1 orden cada `SIGNAL_COOLDOWN_MINUTES` (15) |
| 5 | Geometría + SL en rango + duplicado ±1 pip | `order_manager.prepare` |

Sin límite global de posiciones: la exposición queda acotada por el cooldown.

## Endpoint de entrada

**`POST /v1/smc/signal`**

- **Host:** `http://localhost:8090` (desde la red local) o `http://trading-fastapi:8000` (desde Docker)
- **Header:** `X-Internal-Token: <valor de INTERNAL_TOKEN en .env>`
- **Content-Type:** `application/json`

### Ejemplo BUY (curl)

```bash
curl -X POST http://localhost:8090/v1/smc/signal \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: tu_token_aqui" \
  -d '{
    "symbol":     "EURUSD",
    "entry_zone": true,
    "direction":  "buy",
    "zone_high":  1.15367,
    "zone_low":   1.15367,
    "timeframe":  "M5",
    "source":     "crystal_liquidity",
    "signal_id":  "QT_L_B_1781172300"
  }'
```

### Ejemplo SELL (curl)

```bash
curl -X POST http://localhost:8090/v1/smc/signal \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: tu_token_aqui" \
  -d '{
    "symbol":     "EURUSD",
    "entry_zone": true,
    "direction":  "sell",
    "zone_high":  1.15379,
    "zone_low":   1.15379,
    "timeframe":  "M5",
    "source":     "crystal_liquidity",
    "signal_id":  "QT_L_S_1781183400"
  }'
```

### Respuesta exitosa (`200 OK`)

```json
{
  "symbol":      "EURUSD",
  "entry_zone":  true,
  "direction":   "buy",
  "zone_high":   1.15367,
  "zone_low":    1.15367,
  "timeframe":   "M5",
  "source":      "crystal_liquidity",
  "received_at": "2026-06-11T10:32:45.123456"
}
```

La respuesta es siempre el estado guardado en `smc_signals`. La ejecución de la
orden es asíncrona: consulta los logs o `audit_log` para confirmar el resultado.

### Respuestas de error

| Código | Causa |
|--------|-------|
| `401`  | Token ausente o incorrecto |
| `422`  | Body malformado (campo requerido ausente o tipo incorrecto) |
| `500`  | Error interno — revisar `docker logs trading-fastapi` |

### Logs esperados tras una señal ejecutada

```
[SIZING] EURUSD buy entry=1.15367 sl_pips=30.0 sl=1.15067 tp=1.15967 vol=0.05
[SIMPLE] ORDER PLACED EURUSD buy ticket=12345 cycle=smc_QT_L_B_1781172300
```

Si la señal fue descartada (cooldown, duplicado, símbolo no soportado):
```
[SIMPLE] REJECTED EURUSD buy reason=cooldown_active
```

---

## Activación

Desactivado por defecto. En `.env`:

```
SIMPLE_PIPELINE_ENABLED=true
```

y rebuild: `docker compose build fastapi && docker compose up -d fastapi`

Parámetros opcionales: `FIXED_VOLUME`, `SL_RISK_USD`, `RR_MIN`, `SIGNAL_COOLDOWN_MINUTES`.

## Auditoría

Cada decisión queda en `audit_log` con `cycle_id = smc_<signal_id>`:
`simple_order_placed`, `simple_rejected`, `simple_mt5_error`, `simple_pipeline_error`.
Logs del contenedor: `[SIZING] ...` y `[SIMPLE] ORDER PLACED ...`.

## Ficheros

- `fastapi/services/simple_pipeline.py` — el flujo completo
- `fastapi/services/position_sizing.py` — SL/TP a dinero fijo
- `fastapi/routers/smc.py` — trigger push tras el upsert de la señal
- `fastapi/config.py` — `simple_pipeline_enabled`, `signal_cooldown_minutes`, `fixed_volume`

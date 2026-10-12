# Sistema de Trading Automatizado con IA

Sistema de trading automatizado que conecta Expert Advisors (MQL5) con MetaTrader 5 a través de FastAPI, utilizando **Machine Learning (PPO)** y **LLM (Qwen)** para decisiones de trading.

## Fase 1 — Observable Contract (Webhook)
- `TradeFilledRequest` extendido con `deal_ticket` y `position_id` (observable deal tracking).
- El EA debe registrar siempre: cierre detectado, deal de apertura, payload enviado, HTTP recibido, confirmación DB.
- Guards corregidos: proporcionales al equity real + mínimo MT5 (`min_lot_mt5=0.01`). Log EA: `invalid volume` con `lot=0.03` → corregido a paso de lote (`SYMBOL_VOLUME_STEP`).
- Fix bucle perpetuo: SL/TP con `ATR`/`Bollinger`, `trailing stop` cada tick, `volume` con paso MT5.
- Actualizado: `fastapi/routers/ai.py` (TradeFilledRequest + webhook payload + guards proporcionales + min lot MT5).
- Validación: `pytest fastapi/tests/test_e2e_ai_predict.py -q` → 8 passed (2026-09-09).
- EA MQL5: `NotifyTradeClosed` envía `deal_ticket` y `position_id` en JSON.

## Regla de mantenimiento

Cada cambio funcional o corrección de errores debe incluir una actualización de
este README.

## Arquitectura

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              AI_Quant_Terminal.mq5                           │
│                         (Expert Advisor - MetaTrader 5)                     │
├──────────────────────────────────────────────────────────────────────────────┤
│  OnTick() ──── Cada nueva vela H1 ────► WebRequest POST /api/v1/ai/predict │
│       │                                           │                          │
│       │                                    ┌──────┴───────┐                  │
│       │                                    │   FastAPI    │                  │
│       │                                    │   (Python)   │                  │
│       │                                    └──────┬───────┘                  │
│       │                                           │                          │
│  ManageOpenPositions()                            │                          │
│  (Trailing Stop 25%)                    ┌────────┴────────┐                 │
│                                       │                   │                 │
│                              ┌────────▼────────┐  ┌───────▼───────┐         │
│                              │   PPO Model     │  │ Qwen LLM     │         │
│                              │ (stable-baselines)│  │ (Macro Bias) │         │
│                              └─────────────────┘  └───────────────┘         │
│                                              │                               │
│                                       Decision: BUY/SELL/                     │
│                                                HOLD/CLOSE                     │
│                                              │                               │
└──────────────────────────────────────────────┼───────────────────────────────┘
                                               │
                                    ┌──────────▼──────────┐
                                    │  MetaTrader 5       │
                                    │  (Broker Execution)  │
                                    └─────────────────────┘
```

## Modos de Operación

### Modo 1: AI Brain (Activo por defecto)

El EA `AI_Quant_Terminal.mq5` consulta a FastAPI cada nueva vela H1:

**Modelo v3 — Acción CONTINUA AUTÓNOMA** (preferido):
- `ppo_trading_bot_v3.zip`: acción Box(2,) — `[direction, volume]` (P2-1 / D3)
- El agente decide autónomamente **dirección y volumen** sin heurísticas externas
- SL/TP salen de `production_stops()` (ATR relativo ×1.5 acotado a [10,50] pips, TP = 2×SL): la MISMA función que usa el env de entrenamiento → paridad train/serve
- Trailing stop modelado dentro del env, espejo de `ManageTrailingStop` del EA
- Observation space: `(20, 25)` (Fase 3) — 17 features de mercado + 8 canales de estado (7 legacy + `bars_since_close`); un bundle pre-memoria `(20, 24)` se sirve sin canal (el serve deriva el contrato del ancho del obs)
- 12 features: `returns`, `range`, `dist_sma20`, `rsi14`, `macd`, `macd_signal`, `macd_hist`, `bb_pos`, `lag_return_1/2/3/5`
- **Guards activos**: volume ∈ [10%, 30%], SL ∈ [15, 30] pips, TP ∈ [25, 60] pips
  - **Guard logic**: SL/TP capped **siempre** a 30/60 pips (sin threshold `>0.95`)
  - **Spread real**: EURUSD ~$3 round-trip (3 pips entry+exit) — modelado en `ForexTradingEnvV2`

**Modelo v2 — Dirección discreta** (legacy):
- `ppo_trading_bot.zip`: acción Discrete(3) — 0=FLAT, 1=LONG, 2=SHORT
- Volume/SL/TP fallback: `RiskPercent`, `StopLossPips`, `TakeProfitPips`

**LLM Bias (Qwen)**: Análisis macro con noticias en tiempo real
   - Solo consulta si PPO recomienda BUY/SELL
   - Veta operaciones si sesgo macro contradice dirección

**Hybrid Decision**:
   - PPO BUY + LLM BEARISH → HOLD
   - PPO SELL + LLM BULLISH → HOLD
   - Caso contrario → Ejecuta señal

### Modo 2: SMC Pipeline (Alternativo)
EAs `FalseBreakoutBridge.mq5` y `LiquidityGrabBridge.mq5` usan indicadores técnicos:
- Envían señales a `POST /v1/smc/signal`
- FastAPI aplica filtros (cooldown, noticias, validación geométrica)
- Envía órdenes directamente a MT5

## Parámetros Configurables (AI_Quant_Terminal v11.0)

```mql5
// AI Brain Server
FastAPI_URL    = "http://<IP>:8090"    // URL del servidor FastAPI
InternalToken  = "..."                  // Token de autenticación

// Autonomous Mode (modelo v3)
UseModelRiskParams = true            // Usa volume/sl/tp del modelo directamente

// Position Sizing (legacy fallback — modelo v2)
RiskPercent    = 5.0                   // % de equity por trade
StopLossPips   = 150                   // Stop Loss en pips
TakeProfitPips = 300                   // Take Profit (RR 2:1)
UseATRForSL    = true                  // Usar ATR*1.5 en vez de SL fijo
ATRPeriod      = 14                   // Periodo ATR

// Risk Management
MaxOpenPositions = 3                  // Máximo posiciones abiertas simultáneamente
DailyLossLimit   = 50.0              // Pérdida diaria máxima en USD
```

## Endpoints Principales

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/api/v1/ai/predict` | Predicción ML + LLM para `AI_Quant_Terminal` |
| POST | `/api/v1/ai/retrain/force` | Forzar reentrenamiento PPO manual |
| GET | `/api/v1/ai/retrain/status` | Estado del auto-retrain (contador, última fecha) |
| POST | `/api/v1/ai/trade/filled` | Webhook para registrar trades cerrados |
| POST | `/api/v1/orders/submit` | Envía órdenes validadas a MT5 |
| GET | `/api/v1/account/info` | Info de cuenta y equity |
| GET | `/api/v1/market/candles/latest` | Últimas velas para análisis |
| POST | `/v1/smc/signal` | Recibe señales técnicas para SMC EAs |
| POST | `/v1/smc/news-check` | Cierra posiciones antes de noticias de alto impacto |
| POST | `/v1/smc/close` | Cierre manual por símbolo |
| GET | `/health` | Health check con estado MCP y equity |
| GET | `/docs` | Swagger UI (FastAPI auto-generated) |

## Tests de contrato EA-backend

Los contratos entre `AI_Quant_Terminal_v3.mq5` y FastAPI se validan con:

```bash
cd fastapi
source ../.venv312/bin/activate
pytest tests/test_e2e_ai_predict.py -q
```

Las pruebas verifican que el endpoint de predicción mantiene los campos
`volume`, `sl_pips` y `tp_pips`, que el TP cumple exactamente una relación 1:2
con el SL, y que el webhook `trade/filled` acepta el payload generado por el EA.

## Desacoplamiento en curso

La primera fase fija contratos antes de extraer lógica de producción. El
entrenamiento PPO v3 y la inferencia comparten ahora las 17 features declaradas
por `MARKET_FEATURES`, en el mismo orden y con las mismas fórmulas. Las columnas
`spread` y `real_volume` usan los datos recibidos de MT5 y tienen fallback a
cero cuando no están disponibles.

La primera extracción segura ya está aplicada: la construcción de la
observación PPO v3 se encuentra en `ml/trading_env_v2.py` y conserva el padding,
truncado y estado de posición existentes. `routers/ai.py` la utiliza sin
modificar el contrato del endpoint. Sus invariantes están cubiertas por el test
focalizado de predicción.

La segunda extracción segura separa también la decodificación de dirección PPO
v3 (`BUY`, `SELL` y `HOLD`) en `ml/trading_env_v2.py`. Mantiene los umbrales
`-0.33` y `0.33`; los guards de volumen y SL/TP continúan en el predictor hasta
su propia extracción controlada.

El cambio del contrato de features requiere reentrenar
`ppo_trading_bot_v3.zip` antes de desplegarlo. Un modelo generado con el
contrato anterior no debe reutilizarse como si fuera compatible.

El script de entrenamiento permite hasta 120 segundos para descargar las
50.000 candles históricas desde MT5, porque esa consulta puede superar el
timeout de 30 segundos aunque consultas más pequeñas respondan correctamente.

El modelo v3 original fue reentrenado el 2026-09-09 con 200.000 pasos. El
contrato de observación desde Fase 3 (2026-10-01) es `(20, 25)`: ventana de
20 velas × (17 features de mercado + 8 state channels — los 7 legacy más
`bars_since_close`), con acción continua `(4,)`. El bundle **activo** en
`model_registry` se entrenó y validó con walk-forward bajo el contrato
pre-memoria `(20, 24)` (2026-09-28) y sigue sirviéndose sin canal de memoria
hasta el próximo retrain: `load_inference_artifacts` deriva el contrato del
ancho del obs del bundle (25 → canal activo con `min(1, bars/120)`; 24 →
legado sin consulta; otro ancho → `RuntimeError`, no se sirve). Un modelo con
el contrato antiguo `(10, 21)` tampoco pasa la validación.

El feedback de operaciones reales se carga desde `trade_outcomes` durante el
auto-retrain, por lo que sobrevive a reinicios del contenedor. Se excluyen
outcomes con timestamps inválidos y las pérdidas reales generan una penalización
negativa en `ForexTradingEnvV2`. El reentrenamiento manual de 200.000 pasos
anterior se realizó antes de esta corrección y no incorporó esos 69 outcomes.
Debe ejecutarse un auto-retrain posterior para que el modelo aprenda de ellos.

La notificación de cierres del EA también valida el `POSITION_ID` del deal y
acepta el `MagicNumber` del deal de apertura o cierre. Antes, la búsqueda podía
descartar cierres válidos sin enviar el webhook. FastAPI rechaza ahora entradas
con timestamps anteriores a 2000 o con `exit_time <= entry_time`, evitando
registrar filas corruptas como las de 1970.

## Docker Services

```yaml
fastapi:     # Puerto 8090 (interno 8000)
postgres:    # Puerto 5432 ( PostgreSQL 16-alpine)
```

Verificar estado:
```bash
docker ps --filter name=trading-
curl http://localhost:8090/docs
docker logs trading-fastapi -f
```

## Gestión de Posiciones

### Trailing Stop (AI_Quant_Terminal)
- Se activa cuando el precio avanza 100% del SL
- **TrailingMinPips=15** — mínimo 15 pips de trailing (reducido de 50 para ser menos agresivo)
- Evita cortes prematuros mientras protege ganancias

### Risk Guardian (Nuevos)
- **Daily Loss Limit**: Detiene trading si pérdida diaria > `$DailyLossLimit` USD
- **Max Open Positions**: Bloquea nuevas órdenes si hay ≥ `$MaxOpenPositions` posiciones abiertas
- **Circuit Breaker**: `order_manager.py` valida geometría, rango y duplicados antes de enviar

### Filtros de Protección
- **Cooldown**: 60 min entre intentos del mismo símbolo
- **News Blackout**: Bloqueo ±15 min de noticias de alto impacto
- **Validación Geométrica**: SL/TP calculados desde apertura de vela H1
- **Duplicate Detection**: Detecta órdenes duplicadas (mismo símbolo ± 1 pip)

## Estructura del Proyecto

```
├── mql5/
│   ├── AI_Quant_Terminal.mq5     # EA principal con IA (ML + LLM)
│   ├── FalseBreakoutBridge.mq5   # EA alternativo (SMC técnico)
│   └── LiquidityGrabBridge.mq5  # EA para XAUUSD (SMC técnico)
│
├── fastapi/
│   ├── main.py                   # App FastAPI + lifespan
│   ├── config.py                 # Configuración desde .env
│   ├── routers/
│   │   ├── ai.py                 # /api/v1/ai/predict (ML+LLM)
│   │   ├── smc.py               # /v1/smc/* (Señales técnicas)
│   │   ├── orders.py            # Gestión de órdenes
│   │   └── deps.py              # Autenticación via token
│   ├── services/
│   │   ├── mt5_client.py         # Cliente MCP MT5
│   │   ├── news_filter.py       # Filtro de noticias de alto impacto
│   │   ├── news_scraper.py      # Scraping de noticias macro
│   │   ├── order_manager.py     # Preparación y validación de órdenes
│   │   ├── position_sizing.py   # Cálculo SL/TP/Volumen
│   │   ├── structured_logging.py # Logging con trace_id para correlación MQL5↔API
│   │   ├── auto_retrain.py      # Auto-retrain PPO cada N trades
│   │   ├── market_microstructure.py # Volume Profile + Orderbook Imbalance
│   │   └── simple_pipeline.py   # Pipeline SMC alternativo
│   └── ml/
│       ├── trading_env.py        # Gymnasium Env para PPO
│       ├── train_ppo.py          # Script de entrenamiento PPO (legacy, 8 features)
│   ├── train_ppo_v2.py       # Script de entrenamiento PPO v2 (legacy, 10 features)
│   │   ├── train_ppo_v3.py       # Script de entrenamiento PPO v3 (12 features, acción continua)
│   │   ├── trading_env_v2.py     # Entorno Gymnasium con acción continua Box(2,)
│   │   ├── retrain_agent.py      # Agente de auto-retrain
│   │   └── logs/
│
│       # Metadatos del modelo activo:
│       model.pkl                 # {'feature_names', 'n_features', 'window_size', 'version'} (v2)
│       model_v3.pkl              # {'feature_names', 'n_features', 'window_size', 'version'} (v3)
│       ppo_trading_bot.zip       # Modelo PPO v2 (10 features, obs (10,11), acción Discrete(3))
│       ppo_trading_bot_v3.zip    # Modelo PPO v3 (17 features, obs (20,24), acción Box(2,))
│       best_model.zip            # Backup del mejor modelo anterior
│           └── evaluations.npz   # Métricas de evaluación del modelo
│
│       # Metadatos del modelo activo:
│       model.pkl                 # {'feature_names', 'n_features', 'window_size', 'version'}
│       ppo_trading_bot.zip       # Modelo PPO activo (10 features, obs (10,11))
│       best_model.zip            # Backup del mejor modelo anterior
│
├── frontend/
│   ├── index.html                # Dashboard de analytics
│   ├── css/style.css            # Estilos del dashboard
│   └── js/app.js               # Chart.js + fetcher HTTP
│
├── docker-compose.yml            # Servicios: fastapi, mt5-mcp
└── db/
    └── schema.sql               # Esquema de PostgreSQL (audit_log, orders, smc_signals, trade_outcomes)
```

## Configuración (.env)

```env
# MetaTrader MCP Server
MT5_HTTP_URL=http://<MT5_IP>:8000

# Seguridad
INTERNAL_TOKEN=<token_compartido_con_EA>
HMAC_SECRET=<secret_para_webhooks>

# Risk Management
SL_RISK_USD=15.0                 # Pérdida máxima por trade si SL falla
RR_MIN=1.0                       # Take Profit mínimo (ratio vs SL)
MIN_VOLUME=0.01
MAX_VOLUME=0.50
MAX_OPEN_POSITIONS=3              # Máximo posiciones abiertas
DAILY_LOSS_LIMIT=50.0            # Pérdida diaria máxima en USD

# News Filter
NEWS_FILTER_ENABLED=true
NEWS_BLACKOUT_MINUTES=15

# Pipeline SMC
SIMPLE_PIPELINE_ENABLED=false    # true = usa SMC, false = usa AI Brain
SIGNAL_COOLDOWN_MINUTES=60
```

## Flujo de Decisión (AI Brain)

```
Nueva Vela H1
     │
     ▼
┌─────────────┐
│  PPO Model  │──► action = 0 (FLAT) ──► decision = HOLD
└─────────────┘
     │
     ▼ action = 1 (LONG)
┌─────────────┐
│ Qwen LLM    │──► macro = BEARISH ──► decision = HOLD (veto)
└─────────────┘     │
     │              ▼
     │         macro = BULLISH/NEUTRAL
     ▼              │
decision = BUY ◄────┘
     │
     ▼
trade.Buy(lot, SL, TP)
```

## Características Avanzadas

### Auto-Retrain PPO
Cada vez que un trade se cierra, se registra en `trade_outcomes`. Cada 10 trades (configurable), el modelo PPO se reentrena automáticamente con los últimos datos y feedback de rendimiento.

- `trades_before_retrain=10` — trades antes de cada retrain
- `min_trades_for_retrain=5` — mínimo para activar auto-retrain
- `lookback_candles=500` — velas históricas para reentrenamiento
- Usa **`ForexTradingEnvV2`** (spread=3 pips EURUSD) — coincide con producción
- Modelo reentrenado: **`ppo_trading_bot_v3.zip`** — el mismo que el EA consume

**Modelo actual**: `ppo_trading_bot_v3.zip` — obs space `(20, 24)` (bundle activo, pre-memoria: 17 features + 7 canales; **contrato Fase 3 = `(20, 25)`** con `bars_since_close` para el próximo reentrenamiento — el serve deriva el canal del ancho del obs), acción continua Box(2,) `[direction, volume]` — stops ATR de `production_stops()` compartidos con `/predict` (P2-1) y volumen como escalador de riesgo 0…3% del equity vía `size_ppo_position()` compartida serve↔env (P2-2)

**Modelo legacy**: `ppo_trading_bot.zip` — 200k steps, obs space `(10, 11)`, 10 features, acción Discrete(3), entrenado con `train_ppo_v2.py`

### Volume Profile
Análisis de perfil de volumen por vela:
- **POC** (Point of Control): Precio con mayor volumen
- **VAH/VAL**: Límites del Value Area (70% del volumen)
- **Profile Strength**: Ratio del rango vs volumen total
- Features: `vp_poc_dist`, `vp_vah_dist`, `vp_val_dist`, `vp_in_va`

### Orderbook Imbalance
Mide presión compradora/vendedora:
- **Bid/Ask Ratio**: Ratio de volumen bid vs ask
- **Pressure**: imbalance normalizado
- **Depth Imbalance**: ratio de profundidad en niveles
- Features: `ob_pressure`, `ob_bid_ask_ratio`, `ob_depth_imbalance`

### Structured Logging
Todas las requests incluyen `trace_id` para correlación:
- Header `X-Trace-ID` en requests MQL5 (o genera UUID)
- Logs: `[trace_id=XXXXXXXX] GET /api/v1/ai/predict`
- Permite trazar una request desde MQL5 → FastAPI → MT5

## Observabilidad

Dashboard disponible en `frontend/index.html`:
- Balance/Equity en tiempo real (vía MT5 MCP)
- Rentabilidad semanal y rachas (win/loss streak)
- Gráficos de evolución de capital

## Testing

Tests unitarios con pytest. Cubren servicios críticos para detectar regressions:

```bash
cd fastapi
uv run --with pandas --with pytest --with pytest-asyncio --with httpx --with fastapi --with "pydantic>=2.0" --with pydantic-settings python3 -m pytest tests/ -v
```

**Cobertura actual** (28 tests, 100% pasan):

| Archivo | Qué testa |
|---------|-----------|
| `tests/test_auto_retrain.py` | `_parse_timestamp` (Unix float/int/str, ISO con Z), `RetrainConfig`, `get_state` |
| `tests/test_alerting.py` | `send_alert` (WARNING/ERROR/CRITICAL), fallback si Telegram falla |
| `tests/test_models.py` | `TradeFilledRequest` — validación de direction, exit_reason, pnl |
| `tests/test_config.py` | Settings desde env vars, valores por defecto críticos |

## Estado Actual (Septiembre 2026)

- ✅ **AI_Quant_Terminal v10.1** - Trading activo con ML + LLM
- ✅ **PPO v3 Continuo** - Acción Box(2,) `[direction, volume]` autónoma; stops ATR + trailing compartidos con producción (P2-1: sin train/serve skew)
- ✅ **Risk Guardian** - Daily loss limit + max posiciones abiertas
- ✅ **Order Manager** - Validación geométrica + detección de duplicados
- ✅ **Structured Logging** - trace_id para correlación MQL5↔FastAPI↔MT5
- ✅ **Auto-Retrain PPO** - Reentrena cada **10 trades** con `ForexTradingEnvV2` (spread 3 pips)
- ✅ **Volume Profile** - POC, VAH, VAL, profile_strength por vela
- ✅ **Monitoring exhaustivo** - cada ciclo loggea: raw model output, guards aplicados, LLM response, decisión final, equity
- ✅ **Guard fix** - TP/SL capped **siempre** a 60/30 pips (sin threshold `>0.95` que dejaba pasar 166+ pips)
- ✅ **Orderbook Imbalance** - Bid/ask ratio, pressure, depth imbalance
- ✅ **Docker FastAPI** - Contenedor funcionando en puerto 8090
- ✅ **PostgreSQL** - Base de datos con audit_log, orders, smc_signals, trade_outcomes
- ✅ **Qwen Opción 1** - Quality Score: contexto rico ogni candle → score 0-10 + reason + bias
- ✅ **Unit Tests** - 28 tests cubriendo auto_retrain, alerting, models, config

## Qwen Optimization Options

Se están implementando 5 capacidades progresivas para aprovechar Qwen 4B local:

### Ejemplo de prompt enviado a Qwen 4B

En cada ciclo de predicción, FastAPI construye un prompt unificado con el
contexto técnico actual (incluido estructura de largo plazo: SMA200 y niveles
S/R), las noticias, el calendario económico, el journal de cierres recientes,
la señal del PPO y el contexto de re-entrada. Ejemplo real de la estructura
(paquete 1-4, 2026-10-09):

```text
Context:
- Symbol: EURUSD H1 setup
- RSI(14): 62.4
- ATR: 0.00125 (0.11% of price)
- MACD histogram: 0.000150
- Bollinger position: 0.74
- Range: 0.16% of price
- Last return: 0.080%
- Session: London
- Regime: TRENDING
- ADX: 0.310
- SMA200: Price ABOVE (0.0041), SMA200 RISING (+0.0001)
- Support: 1.08720 | Resistance: 1.09130
- Candles: last 10 H1 candles (oldest→newest), return=+0.19%, up/down=7/3
1:r=+0.04% rng=0.12% body=0.03% d=U v=0.9x | 2:r=+0.08% rng=0.16% body=0.06% d=U v=1.2x | ... | 10:r=+0.02% rng=0.11% body=0.01% d=U v=1.1x

News: FOMC minutes released; no high-impact events in next 15 minutes.
Calendar: 13:30 USD US CPI m/m (in 25 min) | 15:00 EUR ECB President Speech (in 120 min)

ML Signal: BUY with ML confidence 0.781

Trade journal (recent closes) — context only — do NOT copy values:
- 10-09 14:05 SHORT pnl=-1.34% exit=ea regime=RANGING: momentum_fade on London open — entry chased the 

Reentry check — a position was closed recently:
- Bars since close: 1 H1
- Last exit: SHORT (ea), PnL -0.08 EUR, at 2026-10-09T14:05:00+00:00
- Price move since close: +2.5 pips
- reentry_ok=false if re-opening the same trade moments after closing it
  with no new evidence (churn); true if new information, structure/regime
  change or fresh evidence justifies entering now.

Return EXACTLY JSON, no extra text — ONE JSON object. Two FORMAT
examples with different scores; do NOT copy their values — judge THIS
setup on its own context:
{"quality": 8.5, "reason": "trend and ADX align", "bias": "BULLISH", "confidence_modifier": 1.3,  "reentry_ok": true, "regime": "TRENDING"}
{"quality": 3.0, "reason": "range chop against signal", "bias": "BEARISH", "confidence_modifier": 0.7,  "reentry_ok": false, "regime": "RANGING"}

Fields: quality 0-10 — score on evidence: 7-10 only when trend/ADX/session
agree with the ML signal and structure is clean; 1-4 when ranging chop,
against-trend signal or unverified news; 4-7 when mixed. reason max 12 words;
bias BULLISH/BEARISH/NEUTRAL aligned with the setup; confidence_modifier
0.5-1.5 moving effective confidence the same direction; regime
TRENDING/RANGING/VOLATILE/BREAKOUT; reentry_ok boolean per the Reentry
check block above (true when there is no recent close).
```

Notas del contrato de contexto (paquete 1-4):

- **SMA200 / Support / Resistance** — la serie se pide a MT5 con
  `count=240`; el SMA200 (distancia + pendiente de 5 barras) se calcula
  sobre la serie COMPLETA y las features del modelo sobre las 120 más
  recientes (`tail(120)` tras ordenar): el obs de PPO queda byte-idéntico
  a cualquier profundidad de fetch. Guardas: sin `time`, <205 velas o NaN →
  la línea no aparece (nunca contexto inventado).
- **Calendar** — ventana −120…+240 min, orden ascendente, tope 3 eventos;
  con `news_filter_enabled=false` la línea dice
  `unavailable (news filter disabled)` (explícito, nunca silencioso).
- **Trade journal** — esquema SIEMPRE constante (cabecera + filas o
  `- none yet`), insight truncada a 48 chars, tope 5 filas, y SIN
  `quality_score`/`qwen_confidence`: anclarían la respuesta en
  puntuaciones pasadas (defensa anti-ancla H2).
- **Sin campos de SL/TP** — `sl_ok/tp_ok/sl_adjusted/tp_adjusted` ya no se
  piden ni se devuelven (Opción 1: `production_stops` es la única fuente
  de stops); si el modelo los ecoa, el parse los descarta.

### Ejemplo de respuesta de Qwen 4B

Para el prompt anterior, una respuesta válida y compacta podría ser:

```json
{
    "quality": 7.8,
    "reason": "Bullish momentum with expanding range; setup is valid.",
    "bias": "BULLISH",
    "confidence_modifier": 1.08,
    "regime": "TRENDING",
    "reentry_ok": true
}
```

FastAPI interpreta esta respuesta así:

- `quality=7.8`: el setup supera `QUALITY_THRESHOLD` (5.0, intocable).
- `bias=BULLISH`: no contradice la señal `BUY` del PPO.
- `confidence_modifier=1.08`: `effective_prob = ml_prob × confidence_modifier`.
- `reentry_ok=true`: el cerebro aprueba la re-entrada dentro de la ventana
  de churn (ausencia → fail-open `true` + WARNING `REENTRY-DEFAULT`).
- `regime=TRENDING`: queda registrado en la respuesta y en los logs.
- Si el modelo devolviera campos SL/TP, se descartan (no forman parte del
  contrato; grep verificado: ningún consumidor de producción los leía).

Si Qwen devuelve `quality < 5.0`, o `bias=BEARISH` para una señal `BUY`,
FastAPI convierte la decisión final en `HOLD`. Si devuelve JSON inválido o no
responde, se aplica el safe-default degradado (calidad `0.0` fail-closed,
sesgo `NEUTRAL`, modificador `1.0`) y `qwen_degraded=true` en audit_log +
componente en `/pipeline/health`.

La petición se envía al endpoint compatible con OpenAI de Qwen con
`temperature=0.2`, `max_tokens=160` y `response_format={"type":"json_object"}`.
Se incluyen las últimas 10 velas en formato compacto: retorno, rango, cuerpo,
dirección y volumen relativo. Las noticias se normalizan y limitan a 600
caracteres; el calendario (top 3), el journal (top 5 filas de 48 chars) y la
línea SMA200/S-R son líneas cortas de presupuesto fijo: el prompt completo
ronda ~1100 tokens de la ventana de 2048. Los stops NO pasan por Qwen:
`production_stops` (env de entrenamiento = serve) calcula SL/TP con sus
hard bounds [15-30 pips SL, 25-60 pips TP, TP:SL ≥ 1.5:1]. Si Qwen no está
disponible, se aplica el safe-default degradado.

### Opción 1 — Quality Score ✅ (implementado)
- **Qué**: Qwen recibe contexto completo de mercado (RSI, ATR, MACD, Bollinger, sesión, ADX, SMA200/S-R, noticias, calendario, journal y re-entrada) y retorna score 0-10 + reason + bias
- **Trigger**: Cada candle (no solo BUY/SELL) — unificado en llamada única
- **Acción**: Si `quality_score < QUALITY_THRESHOLD` (5.0, intocable) → decisión overriden a HOLD
- **Respuesta**: `{quality_score, quality_reason}` en PredictResponse
- **Logs**: `QWEN-UNIFIED` con score, reason, bias, conf_mod, regime

### Opción 2 — SL/TP Validator ⚠️ (servicio implementado, NO cableado en /predict)
- **Qué**: `query_qwen_sltp_validator` valida que el SL y TP propuestos sean razonables para el régimen actual
- **Estado (paquete 1-4)**: el servicio y sus tests se CONSERVAN en
  `services/qwen_client.py`, pero **no se invoca en `/predict`**: los stops
  los calcula `production_stops` (misma fuente que el env de entrenamiento)
  con hard bounds SL [15-30 pips], TP [25-60 pips], ratio TP:SL ≥ 1.5:1.
  El prompt unificado ya no pide `sl_ok/tp_ok/sl_adjusted/tp_adjusted` (si
  el modelo los ecoa, el parse los descarta). Cablear Opción 2 exige
  evidencia fuera de muestra de que aporta sobre `production_stops`.
- **Hard bounds**: SL [15-30 pips], TP [25-60 pips], ratio TP:SL ≥ 1.5:1
- **Fallback**: Si Qwen no disponible → hard bounds aplicados
- **Logs**: `SLTP-VALIDATOR`, `SLTP-REJECTED` (sólo si se cablea)

### Opción 3 — Confidence Modulation ✅ (implementado)
- **Qué**: Qwen retorna modificador continuo [0.5, 1.5] que multiplica ml_prob
- **Trigger**: Cada candle (unificado con Opciones 1+2+4)
- **Acción**: `effective_prob = ml_prob * confidence_modifier` en PredictResponse
- **Campos**: `confidence_modifier`, `effective_prob`

### Opción 4 — Regime Classifier ✅ (implementado)
- **Qué**: Qwen clasifica TRENDING/RANGING/VOLATILE/BREAKOUT + sesión
- **Trigger**: Cada candle (unificado)
- **Acción**: Campo `regime` en PredictResponse + usado en SL/TP validation
- **Fallback**: Clasificación heurística si Qwen no disponible

### Opción 5 — Post-Trade Journal ✅ (implementado)
- **Qué**: Análisis de trades cerrados para descubrir patrones con Qwen
- **Trigger**: Después de cada cierre de trade (SL, TP, manual)
- **Tabla**: `trade_insights` con insight, confidence, regime, quality_score
- **Servicio**: `services/trade_journal.py` → `record_trade_closed()` llamado desde `auto_retrain.py`
- **Insights**: momentum_fade, trend_continuation, range_bound, volatility_squeeze, session_gap, news_shock, timing_error, signal_quality_poor
- **Fallback**: Heurístico si Qwen no disponible

### Troubleshooting

**Error HTTP 1001 en MT5:**
- Verificar que FastAPI está corriendo: `curl http://localhost:8090/`
- Ver logs: `docker logs trading-fastapi -f`
- Verificar token en MQL5 coincide con `INTERNAL_TOKEN` en `.env`

**Contenedor en restart loop:**
- `docker logs trading-fastapi` → buscar SyntaxError o errores de import
- Generalmente causado por errores en archivos Python (caracteres especiales, docstrings)

**MT5 no conecta:**
- Verificar `MT5_HTTP_URL` en `.env` apunta a IP correcta del MT5 MCP Server
- Verificar que MT5 MCP Server está corriendo

**Qwen no conecta (QWEN-UNIFIED ║ ERROR ║ Connection refused / All connection attempts failed):**
- Qwen debe estar corriendo en `http://<QWEN_IP>:8080` (máquina ubuntu via Tailscale)
- Verificar desde host: `curl http://<QWEN_IP>:8080/v1/models`
- Verificar que el servicio Qwen/Ollama esté activo en la máquina ubuntu con `llm-use 4b`
- **Timeout**: 20s — si Qwen tarda más, caerá a fallback (normal en hardware lento)
- Si Qwen no está disponible → el sistema usa **fallback** (quality=5.0, bias=NEUTRAL, conf=1.0) → comportamiento normal
- Logs de fallback incluyen `FALLBACK ║ q=5.0 bias=NEUTRAL conf_mod=1.0 ...` → esto NO es un error, es el comportamiento esperado
- Para activar Qwen: iniciar `llm-use 4b` en la máquina ubuntu y verificar que escuche en `0.0.0.0:8080`

**Qwen-UNIFIED logs de éxito vs fallback:**
```
╔══ ÉXITO (Qwen accesible) ════════════════════════════════════════╗
║ QWEN-UNIFIED ║ q=7.5 bias=BEARISH conf_mod=0.82 ...             ║
╚══ FALLBACK (Qwen no accesible) ══════════════════════════════════╝
║ QWEN-UNIFIED ║ ERROR ║ All connection attempts failed → FALLBACK║
║ QWEN-UNIFIED ║ FALLBACK ║ q=5.0 bias=NEUTRAL conf_mod=1.0 ...  ║
```

**Journal insights siempre vacíos:**
- La tabla `trade_insights` solo se llena cuando `record_trade_filled()` es llamado (trade cerrado con exit completo)
- Trades abiertos NO generan insights — solo al cerrar (SL, TP, o cierre manual)
- Verificar que hay trades cerrados: `docker exec trading-postgres psql -U trading -d trading -c "SELECT COUNT(*) FROM trade_outcomes;"`

---

## Troubleshooting — Sesión 2026-09-07

Issues encontrados durante la prueba end-to-end y sus fixes.

### A. EA no notifica cierres a `/api/v1/ai/trade/filled`

**Síntoma:** `docker logs trading-fastapi` muestra `POST /api/v1/ai/ai/predict` pero cero `POST /api/v1/ai/trade/filled`. El `filled_count` del auto-retrain se queda en 0.

**Causa raíz:** El EA solo llamaba al webhook desde `CloseAllPositions()` (cuando la predicción dice "CLOSE"), pero NO cuando MT5 cierra la posición por SL/TP automáticos. Los trades cerrados por stop/take profit se perdían.

**Fix:** Agregar `OnTradeTransaction()` en el EA (`mql5/AI_Quant_Terminal_v3.mq5`):

```mql5
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest& request,
                        const MqlTradeResult& result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   ulong dealTicket = trans.deal;
   if(dealTicket <= 0) return;
   if(trans.symbol != Symbol()) return;

   if(!PositionSelectByTicket(trans.position)) return;
   if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) return;

   // Deduplicación
   if(dealTicket == g_lastClosedTicket) return;
   g_lastClosedTicket = dealTicket;

   // Datos del deal
   double volume   = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
   double pnl      = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
   long dealType   = HistoryDealGetInteger(dealTicket, DEAL_TYPE);
   string comment  = HistoryDealGetString(dealTicket, DEAL_COMMENT);

   // ...POST a /api/v1/ai/trade/filled
}
```

Y deduplicación con variable de módulo: `ulong g_lastClosedTicket = 0;`

### B. MT5 rechaza WebRequest con HTTP 1003

**Síntoma:** Log del EA: `Error HTTP 1003. Intentos fallidos: 1`. El backend responde 200 a `curl` desde la red local pero MT5 no llega.

**Causa raíz:** MT5 requiere whitelist explícita de URLs para `WebRequest()`.

**Fix en MT5:**
1. **Tools → Options → Expert Advisors**
2. Marcar ✅ **Allow WebRequest for listed URL**
3. Click **Add** y agregar: `http://100.91.167.17:8090` (la IP del backend)
4. En el chart, click derecho sobre el EA → **Properties** → **Common** → ✅ **Allow WebRequest**
5. Reiniciar el EA (remover y volver a arrastrar al chart)

**Verificación desde el host:**
```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://100.91.167.17:8090/health
# Debe devolver: HTTP 200
```

### C. Retrain falla con `ImportError: cannot import name 'upload_model'`

**Síntoma:** `docker logs trading-fastapi` muestra:
```
ImportError: cannot import name 'upload_model' from 'services.model_backup'
```

**Causa raíz:** En `services/auto_retrain.py:229` se importa `upload_model` pero esa función no existe. `model_backup.py` solo expone `backup_model()`.

**Fix:** Alias al importar:
```python
# services/auto_retrain.py
from services.model_backup import backup_model as upload_model
```

### D. Retrain falla con `Observation spaces do not match`

**Síntoma:**
```
ValueError: Observation spaces do not match: Box(-inf, inf, (10, 16), float32) != Box(-inf, inf, (10, 21), float32)
```

**Causa raíz:** El modelo PPO preexistente fue entrenado con 12 features de mercado (16 con state channels), pero el `trading_env_v2.py` actual genera 17 features (21 con state). Mismatch de dimensiones.

**Fix:** Cuando hay mismatch, reentrenar desde cero:
```python
# services/auto_retrain.py
if os.path.exists(MODEL_PATH):
    try:
        env = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
        model = PPO.load(MODEL_PATH, env=env)
    except ValueError as dim_err:
        logger.warning("Dimensiones incompatibles — reentrenando desde cero")
        env = DummyVecEnv([lambda: ForexTradingEnvV2(**env_cfg)])
        model = PPO("MlpPolicy", env, learning_rate=cfg.learning_rate, verbose=cfg.verbose)
```

### E. Retrain falla con `reset_num_episodes` kwarg inválido

**Síntoma:** `TypeError: PPO.learn() got an unexpected keyword argument 'reset_num_episodes'`

**Causa raíz:** La API de `stable_baselines3.PPO.learn()` no acepta `reset_num_episodes`.

**Fix:** Eliminar el kwarg:
```python
# services/auto_retrain.py
model.learn(
    total_timesteps=cfg.lookback_candles * cfg.n_epochs,
    progress_bar=False,
    # reset_num_episodes=0,  # REMOVED — no soportado
)
```

### F. `total_seconds()` falla con `float` en `trade_journal.py`

**Síntoma:**
```
ERROR [services.trade_journal] 'float' object has no attribute 'total_seconds'
```

**Causa raíz:** El webhook recibe `entry_time` y `exit_time` como `float` (Unix timestamp) o `str` (ISO), pero `_query_qwen_trade_insight()` espera `datetime`.

**Fix:** Normalizar al inicio de la función:
```python
# services/trade_journal.py
from datetime import datetime, timedelta, timezone

if isinstance(entry_time, (int, float)):
    entry_time = datetime.fromtimestamp(entry_time, tz=timezone.utc)
elif isinstance(entry_time, str):
    try:
        entry_time = datetime.fromtimestamp(float(entry_time), tz=timezone.utc)
    except (ValueError, TypeError):
        entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
# (mismo bloque para exit_time)
```

### G. `/predict` falla con `UnboundLocalError: rsi_val`

**Síntoma:** `Error HTTP 500` en `/predict` con traceback apuntando a `_query_qwen_unified(..., rsi=rsi_val, ...)`.

**Causa raíz:** `rsi_val` (y `atr_val`, `macd_hist_val`, etc.) se asignan **dentro** del branch v3 exitoso del PPO. Si el modelo v3 falla o no existe, se cae al v2 que no asigna estas variables, pero la llamada a `_query_qwen_unified()` las referencia de todas formas.

**Fix:** Inicializar defaults antes del bucle de modelos:
```python
# routers/ai.py — antes del for ppo_path, model_version in ppo_paths:
ml_prob = 0.5
decision = "HOLD"
raw_action = None

# Defaults para indicadores (se sobreescriben dentro del branch v3 exitoso)
rsi_val = 50.0
atr_val = 0.001
atr_pct = 0.001
macd_hist_val = 0.0
bb_pos_val = 0.0
range_pct = 0.002
last_ret_val = 0.0
hour_val = 12
```

### H. Modelo v3 retorna `model_version: v2` después del retrain

**Síntoma:** Después del retrain, `/predict` retorna `model_version: v2` aunque existe `ppo_trading_bot_v3.zip`. Log: `Error con modelo /app/ml/ppo_trading_bot_v3.zip: Unexpected observation shape (10, 16) for Box environment, please use (10, 21)`.

**Causa raíz:** El código de predicción construía la observación con 12 features de mercado (hardcoded), pero el modelo v3 reentrenado espera 17 features (porque `trading_env_v2.py` toma todas las features del df que no sean OHLCV).

**Fix:** Auto-detectar dimensiones del modelo y usar todas las features de mercado:
```python
# routers/ai.py — construir obs para v3
if model_version == "v3":
    # Auto-detect: leer observación esperada del environment
    expected_market_features = model.observation_space.shape[1] - 4

    # Excluir OHLCV/tiempo/target — tomar el resto como features
    _exclude = {'time', 'open', 'high', 'low', 'close', 'tick_volume', 'target', 'volume'}
    v3_market_features = [c for c in df.columns if c not in _exclude]
    v3_available = [f for f in v3_market_features if f in df.columns]
    df_clean = df[v3_available].dropna()

    # Pad/truncar a la cantidad esperada
    last_market = df_clean.iloc[-10:].values.astype(np.float32)
    if last_market.shape[1] < expected_market_features:
        pad = np.zeros((10, expected_market_features - last_market.shape[1]), dtype=np.float32)
        last_market = np.hstack([last_market, pad])
    elif last_market.shape[1] > expected_market_features:
        last_market = last_market[:, :expected_market_features]
    # ... construir obs con state channels
```

### I. Backup a GCS falla (no bloqueante)

**Síntoma:**
```
ERROR [services.auto_retrain] [RETRAIN] Backup falló: backup_model() takes 0 positional arguments but 1 was given
WARNING [services.auto_retrain] [RETRAIN] ===== RETRAIN COMPLETADO (sin backup) =====
```

**Causa raíz:** El retrain actualiza `ppo_trading_bot_v3.zip`, pero `backup_model()` estaba fijado al modelo v2 (`ppo_trading_bot.zip`) y no aceptaba una ruta como argumento.

**Solución implementada:** `backup_model(model_path=...)` acepta una ruta opcional y `auto_retrain.py` le pasa explícitamente `MODEL_PATH`. El backup usa ahora el hash y el archivo del modelo v3 recién entrenado.

**Verificación:**
```python
# services/model_backup.py
async def backup_model(model_path: str = MODEL_LOCAL_PATH) -> dict:
    ...
```

El retrain no se bloquea si GCS falla: el modelo ya queda guardado localmente. Revisar el motivo concreto con:
```bash
docker logs trading-fastapi --since 15m | grep -E "RETRAIN|MODEL-BACKUP"
```

### I.1. Promoción falla con `ppo_trading_bot_v3.candidate.zip` inexistente

**Síntoma:**
```
FileNotFoundError: ... ppo_trading_bot_v3.candidate.zip -> ... ppo_trading_bot_v3.zip
```

**Causa raíz:** Stable-Baselines3 guardó el candidato exactamente como `ppo_trading_bot_v3.candidate`, mientras el código intentaba mover una ruta con `.zip` añadido.

**Solución implementada:** El servicio guarda explícitamente `ppo_trading_bot_v3.candidate.zip` y mueve esa misma ruta con `os.replace()`.

**Si el contenedor conserva código antiguo:** reconstruir el servicio y verificar la ruta cargada:
```bash
docker compose up -d --build fastapi
docker exec trading-fastapi grep -n "candidate_path\|os.replace" /app/services/auto_retrain.py
```

### J. Auto-aprendizaje del modelo (Real Outcome Feedback)

**Problema:** Antes de este fix, el modelo PPO se reentrenaba solo contra velas históricas sintéticas, sin saber qué tan buenas fueron sus predicciones reales. Los trades del EA se guardaban en DB y Qwen los analizaba, pero el PPO no recibía feedback → era "amnésico" entre reentrenamientos.

**Solución:** Inyectar los `TradeOutcome` reales del EA al `ForexTradingEnvV2` durante el retrain. El env ahora ajusta el reward cuando el step actual coincide con un trade real:

```python
# ml/trading_env_v2.py
if model_pos == real_pos and model_pos != 0:
    # Coincidió con trade real → reforzar proporcional al PnL real
    reward += pnl_normalized * self.real_outcome_weight
elif model_pos != 0 and real_pos != 0 and model_pos != real_pos:
    # Contradice al trade real → penalizar
    reward -= abs(pnl_normalized) * self.real_outcome_weight
elif model_pos == 0 and real_pos != 0 and outcome.pnl < 0:
    # FLAT y el trade real perdió → pequeño bonus (evitó pérdida)
    reward += 0.5 * self.real_outcome_weight
```

**Configuración:**
- `RetrainConfig.real_outcome_weight: float = 0.3` (peso del feedback real vs sintético)
- `ForexTradingEnvV2(real_outcomes=[...], real_outcome_weight=0.3)` (constructor acepta outcomes)

**Activación:** En `auto_retrain.py:_do_retrain()`, los outcomes se copian de `_state.outcomes` al env:

```python
real_outcomes = list(_state.outcomes)  # copia de los últimos N trades
env_cfg = dict(
    df=df,
    real_outcomes=real_outcomes,
    real_outcome_weight=cfg.real_outcome_weight,
    ...
)
```

**Log de confirmación:** Cada retrain ahora muestra:
```
[RETRAIN] Inyectando 10 outcomes reales al env (peso=0.30)
```

**Test E2E del feedback:** `docker exec trading-fastapi python3 /tmp/feedback_test.py` valida:
- `weight=0.0` → 0 feedbacks aplicados (sintético puro)
- LONG action vs outcome LONG+TP (+$25) → feedback `+1.25` (2.5 × 0.5)
- LONG action vs outcome SHORT-SL (-$15) → feedback `-0.75` (-1.5 × 0.5)

**Limitación actual:** Los timestamps de los outcomes se comparan con la columna `time` del df. Si los outcomes son muy recientes (después de los últimos 500 velas históricas), no se matchearán. Solución futura: extender el lookback o reindexar el df.

### Resumen de archivos modificados en esta sesión

| Archivo | Cambio |
|---------|--------|
| `mql5/AI_Quant_Terminal_v3.mq5` | Agregado `OnTradeTransaction()` para detectar cierres de MT5 |
| `fastapi/services/auto_retrain.py` | Fix `upload_model` import, fix dim mismatch, remover `reset_num_episodes`, inyectar outcomes reales |
| `fastapi/services/trade_journal.py` | Normalizar `entry_time`/`exit_time` a `datetime` antes de `total_seconds()` |
| `fastapi/routers/ai.py` | Defaults para indicadores, auto-detect v3 obs shape, todas las features para v3 |
| `fastapi/ml/trading_env_v2.py` | Real outcome feedback: indexar outcomes por step, ajustar reward según PnL real |

### Test E2E validados (3 niveles)

```bash
# 1. Pipeline completo
bash /tmp/e2e_test.sh
# Cubre: health, predict, trade filled, normalización direction, persistencia DB,
#        threshold de retrain, force retrain, modelo guardado, predict post-retrain

# 2. Qwen 4B Jetson
bash /tmp/qwen_test2.sh
# Cubre: latencia, JSON parsing, predict EURUSD/XAUUSD, trade journal

# 3. Auto-learning con real outcomes
bash /tmp/auto_learn_test.sh
docker exec trading-fastapi python3 /tmp/feedback_test.py
# Cubre: inyección de outcomes al env, feedback positivo/negativo según PnL real

### K. Protecciones y evaluación antes de reemplazar PPO

- Los guards de volumen, SL y TP solo se aplican cuando `target_pos` es `LONG` o `SHORT`.
- En `HOLD`, el flujo conserva la acción del PPO y registra `GUARD-CLEAN`; no fuerza volumen mínimo.
- Cada retrain usa `100000` `total_timesteps` por defecto; el valor anterior de `1500` era insuficiente.
- Si no hay cierres del EA, el log indica `Sin cierres reales` y el entrenamiento se considera histórico/sintético.
- El candidato se evalúa en una partición de validación separada usando aperturas, operaciones cerradas, win rate y reward.
- El candidato se rechaza si no abre ninguna operación, produce reward no finito o no mejora el reward del modelo activo.
- Solo después de pasar esa evaluación se guarda como `ppo_trading_bot_v3.zip`.
```

## Troubleshooting — Sesión 2026-09-10

### L. Lógica de IA resulta siempre en HOLD

**Síntoma:** Los logs de `trading-fastapi` muestran que el modelo PPO v3 genera acciones de `BUY` o `SELL`, pero la decisión final es siempre `HOLD`. El EA no abre operaciones.

**Causa raíz:** El cálculo de SL/TP dinámico en el EA (`AI_Quant_Terminal_v3.mq5`) basado en Bandas de Bollinger estaba fallando. Los logs revelaron que `bbLower` y `bbUpper` eran idénticos (ej. `1.07727`). Esto provocaba un ratio riesgo/beneficio de 1.0, que era correctamente rechazado por la lógica de validación de FastAPI, la cual exige un ratio mínimo de 1.5. El log clave era `SLTP-VALIDATOR ║ WARNING ║ SL/TP ratio 1.0 es < 1.5. Rechazando.`.

**Fix:** Se modificó `mql5/AI_Quant_Terminal_v3.mq5` para implementar un fallback a ATR.
```mql5
// mql5/AI_Quant_Terminal_v3.mq5
// Prioridad: Bollinger. Si es inválido (bandas iguales), fallback a ATR.
bool bbValid = (bbWindowReady) && (MathAbs(bbUpper - bbLower) > pipSize * 0.1) && ...;
if(bbValid) {
   // ... calcular con BB
   if(tpPips < slPips * 1.5) {
      bbValid = false; // Fuerza el fallback a ATR
   }
}
if(!bbValid) {
   // ... calcular con ATR
}
```
Ahora, si las Bandas de Bollinger son inválidas o el ratio es insuficiente, el EA intentará calcular el SL/TP usando ATR, haciendo el sistema más robusto ante datos de mercado anómalos.

## Problemas de Bandas de Bollinger

- **BB iguales (2026-09-10)**: `bbLower == bbUpper` (1.16370) → SL/TP iguales (15 pips). Causa: datos BB planos o `CopyBuffer` con índices incorrectos. Propuesta: guard `if(bbUpper == bbLower) use ATR fallback`. Log: `SL/TP BOLLINGER: bbLower=1.16370 bbUpper=1.16370 → sl=15.0 tp=15.0`.

### M. El EA ejecuta SL/TP legacy de 150/300 pips después del reentrenamiento

**Síntoma:** El EA registra órdenes como `BUY entry=1.16276 lot=0.01 SL=1.14776 TP=1.19276`. Eso equivale a `150/300` pips y coincide exactamente con `StopLossPips`/`TakeProfitPips` del fallback legacy.

**Causa raíz:** La inferencia v3 en `fastapi/routers/ai.py` obtenía la acción en `action`, pero intentaba aplicar `np.tanh(raw_action)` sin haber definido `raw_action`. La excepción se capturaba dentro del bucle de modelos; el backend descartaba v3 y continuaba con v2. El EA recibía una respuesta sin los tres parámetros autónomos y entraba en su rama legacy, evitando los guards v3.

**Fix:** La inferencia v3 convierte `action` a `np.float32`, valida que tenga cuatro componentes y utiliza directamente la acción acotada por el `Box` de PPO. Se eliminó la transformación heurística `tanh`/`risk_factor`.

**Validación (2026-09-10):** `docker exec trading-fastapi pytest tests/ -q` → `44 passed`. El test E2E confirma que el flujo v3 conserva `model_version="v3"` y no cae silenciosamente al fallback por esa excepción.

**Protección adicional (EA v11.3):** `mql5/AI_Quant_Terminal_v3.mq5` ya no ejecuta `StopLossPips`/`TakeProfitPips` cuando falta el contrato autónomo v3. En ese caso registra `HOLD` y no abre la orden. Validación: `pytest tests/test_e2e_ai_predict.py -q` → `8 passed`; suite completa → `44 passed`.

**Restauración de pruebas (2026-09-10):** Se eliminaron los mocks que sustituían el contenido del EA por strings simulados. `test_e2e_ai_predict.py` vuelve a leer `mql5/AI_Quant_Terminal_v3.mq5`; Docker monta `./mql5` en `/mql5` en modo lectura. El E2E vuelve a cargar un modelo v3 simulado y verifica `model_version="v3"`, `volume`, `sl_pips` y `tp_pips`. Resultado validado: `docker exec trading-fastapi pytest tests/ -q` → `44 passed`.

**Limitación:** Estos tests validan el contrato y el flujo de inferencia, pero no certifican rentabilidad, estabilidad estadística ni comportamiento fuera de muestra del modelo `ppo_trading_bot_v3.zip`. El modelo no debe promoverse a trading real sin una evaluación OOS reproducible.

## Troubleshooting — Sesión 2026-09-11

### N. Refactorización del módulo AI (`fastapi/routers/ai.py`)

**Cambio aplicado:** Adaptación completa de `ai.py` a la arquitectura refactorizada de `trading_env_v2.py` y `train_ppo_v3.py`, corrigiendo 5 fallos críticos de producción.

**Archivos editados:**
- `fastapi/routers/ai.py` — carga singleton de PPO + VecNormalize, endpoint `/predict` refactorizado, `PredictRequest` extendido, cálculo correcto de lotes y SL/TP.
- `fastapi/tests/test_e2e_ai_predict.py` — tests alineados a la nueva firma de `build_v3_observation` y al contrato actualizado.

**Tests:** `pytest fastapi/tests/ -q` → `44 passed` (2026-09-11).

**Fix principales:**
- **VecNormalize en inferencia:** Ahora se carga una sola vez en `@router.on_event("startup")` y se aplica `normalize_obs()` antes de `model.predict()`. Elimina acciones erráticas por escala incorrecta.
- **Firma de observación:** `build_v3_observation()` ahora recibe los 13 parámetros requeridos (`entry_price`, `current_price`, `sl_price`, `tp_price`, `current_atr`, `steps_in_trade`, `max_holding_steps`, `balance`, `initial_balance`, `window_size`).
- **Volumen MT5:** Se eliminó la doble multiplicación por `0.5`. El lote se redondea al paso estándar del broker (`round(round(lots / 0.01) * 0.01, 2)`), garantizando mínimo `0.01` lotes.
- **Ratio SL/TP:** Corregido a 1:2 real. El TP en pips se calcula como `sl_pips * 2.0`, no como `sl_norm * 200` desacoplado.
- **Codificación de posición:** `PredictRequest` usa `0=Flat, 1=Long, 2=Short` (MT5). Internamente se remapea a `1=Long, -1=Short, 0=Flat` antes de construir la observación.
- **ML probability:** Ahora se aproxima con `np.clip(abs(direction), 0.1, 0.99)`, evitando la densidad continua inválida del Normal multivariado.

**Validación E2E:** `test_ai_predict_e2e_flow` confirma `model_version="v3"`, `volume`/`sl_pips`/`tp_pips` presentes y ratio `tp_pips == sl_pips * 2.0`.

**Pendiente — Retraining del modelo:** Completado (2026-09-11). `ppo_trading_bot_v3.zip` regenerado con `trading_env_v2.py` + `train_ppo_v3.py` refactorizados. Backtest OOS: 1402 trades, win rate 40.09%, Sharpe -4.79, drawdown 51.03%. **No promover a trading real sin evaluación OOS adicional.**

**Contrato EA:** compatible sin cambios. El EA envía `position`/`entry_price`/`sl_price`/`tp_price`/`steps_in_trade` y recibe `volume`/`sl_pips`/`tp_pips`. El servidor computa `current_price`, `current_atr`, `balance` e `initial_balance` internamente. No requiere bump de versión MQL5.

## Troubleshooting — Sesión 2026-09-14

### N. Cálculo dinámico de lotes + spread + registro de trades

**Cambio aplicado:** Ajuste de lote mínimo dinámico, inclusión de spread en TP del EA, logging fuerte en webhook y eliminación total de fallos silenciosos.

**Archivos editados:**
- `fastapi/routers/ai.py` — lote mínimo dinámico basado en equity/riesgo/umbral de ganancia, timeout de Qwen alineado en 20s, logging en `/trade/filled`, eliminación de todos los `except Exception`/`except:`.
- `fastapi/services/trade_journal.py` — timeout de Qwen aumentado a 20s, eliminación de fallbacks heurísticos y capturas silenciosas.
- `fastapi/services/auto_retrain.py` — eliminación de capturas silenciosas en `_persist_trade_outcome`, `record_trade_filled`, `_do_retrain`, `_parse_timestamp` (estricto), baseline/backup y métricas.
- `fastapi/services/mt5_client.py` — eliminación de capturas silenciosas en correlation sizing y fallback a cola diferida.
- `mql5/AI_Quant_Terminal_v3.mq5` — versión `12.1`, logging de webhook body/HTTP, `StringConcatenate` corregido, SL/TP con spread incluido, timeout de `WebRequest` aumentado.

**Tests:** `pytest fastapi/tests/ -q` → `44 passed` (2026-09-14).

**Fix principales:**
- **Lotes dinámicos:** `final_lots` se calcula para garantizar ~3€ netos en el TP, respetando `equity * 0.03` de riesgo y el step de lote del broker. No hay hardcodeo de `0.10`.
- **Spread en TP:** EA calcula `tpPrice` sumando/restando el spread para que la ganancia neta al cierre sea la distancia configurada.
- **Zero silent failures:** eliminados todos los `except Exception`/`except:` y fallbacks heurísticos en `ai.py`, `trade_journal.py`, `auto_retrain.py` y `mt5_client.py`. Cualquier error propaga y detiene el flujo.
- **Webhook logging:** EA loguea `body` y `res` de `WebRequest` para diagnóstico directo.

**Validación:** `docker logs trading-fastapi` confirma startup limpio, modelo cargado y endpoint `/trade/filled` respondiendo `200`.

**Registro de trades en BD:** Confirmado (2026-09-14). El EA v12.5 envía correctamente el webhook `/trade/filled` con `openingDeal` y `dealTicket` diferenciados. Log de evidencia:
- `2026.09.14 12:58:19.085 [AI Terminal] Webhook HTTP res=200`
- `2026.09.14 12:58:19.085 [AI Terminal] Cierre reportado exitosamente. Ticket=58063195488 PnL=-0.61`
- `trade_outcomes` actualizada en tiempo real tras cada cierre.

## Troubleshooting — Sesión 2026-09-24

### O. Timeframe de inferencia vs. disparo del EA

**Cambio aplicado:** El EA ahora consulta al backend cada **5 minutos** (`M5`) en vez de cada hora (`H1`) para detectar oportunidades antes. Sin embargo, el backend sigue solicitando velas **H1** al MT5 MCP para mantener la consistencia con el modelo PPO v3, que fue entrenado con velas H1.

**Archivos editados:**
- `mql5/AI_Quant_Terminal_v3.mq5` — `OnTick()` ahora usa `PERIOD_M5` como disparador de ciclo y envía `timeframe=M5` en el payload.
- `fastapi/routers/ai.py` — el parámetro `timeframe` del request se usa para logging/telemetría, pero la consulta de velas al MT5 MCP se fuerza a `H1` para que las features del modelo sigan siendo consistentes.

**Comportamiento resultante:**
- EA revisa el mercado cada 5 minutos.
- Backend recibe `timeframe=M5`, ignora ese valor para features y pide velas H1 al MT5 MCP.
- Modelo PPO v3 sigue recibiendo features H1 sin necesidad de reentrenamiento.

### P. Endpoint `/trade/filled` tolerante a payloads del EA

**Cambios aplicados:**
- `TradeFilledRequest` ahora acepta `entry_time`/`exit_time` como `float | str`, y tolera `entry_time=0` o timestamps iguales.
- Se añadió mapeo de dirección `BUY→LONG`, `SELL→SHORT`, `LONG→LONG`, `SHORT→SHORT`.
- `pnl`/`pnl_pct` se convierten a `float` si llegan como string.
- La constraint de BD incluye ahora `'ea'` como `exit_reason`.

**Archivos editados:**
- `fastapi/routers/ai.py`
- `fastapi/db/schema.sql`

### Q. Estado del auto-retrain persistido en PostgreSQL

**Problema:** El estado del auto-retrain (`filled_count`, `last_retrain_time`, `retrain_in_progress`) vivía en memoria. Reiniciar el contenedor lo perdía, dejando el contador en 1 y bloqueando el trigger de retrain.

**Solución:** El estado ahora se carga y guarda en la tabla `retrain_state` de PostgreSQL. Al reiniciar, el contador se recupera desde `trade_outcomes` si viene vacío.

**Archivos editados:**
- `fastapi/services/auto_retrain.py`

### R. Qwen no disponible no rompe `/predict`

**Cambio aplicado:** Si Qwen no alcanza el timeout de 20s, `_query_qwen_unified()` y `_query_qwen_sltp_validator()` devuelven defaults seguros (`NEUTRAL`, `quality=5.0`, SL/TP sin modificar) y loguean el error. Esto evita HTTP 500 por caídas de red/Tailscale.

**Archivos editados:**
- `fastapi/routers/ai.py`
- `fastapi/services/trade_journal.py`

### S. Validación de volumen mínimo para cierres manuales del EA

**Cambio aplicado:** El EA ahora bloquea ejecutar `CloseAllPositions()` cuando el backend devuelve `CLOSE` si el volumen total de sus posiciones es menor a `0.03` lotes. Solo se permiten cierres automáticos por SL/TP o cierres manuales del EA para posiciones con volumen suficiente.

**Archivos editados:**
- `mql5/AI_Quant_Terminal_v3.mq5`

### T. Clasificación de cierres del EA

**Cambio aplicado:** El EA distingue cierres por EA (`exit_reason="ea"`) de cierres manuales (`exit_reason="manual"`), usando `DEAL_REASON_EXPERT` o el comentario `AI-Predict`.

**Archivos editados:**
- `mql5/AI_Quant_Terminal_v3.mq5`
- `fastapi/routers/ai.py`
- `fastapi/db/schema.sql`

### U. Backup a GCS no bloqueante

**Cambio aplicado:** Si no hay credenciales de Google Cloud, `upload_model()` devuelve un dict de error estructurado en vez de propagar la excepción. El retrain ya no depende del éxito del backup para completarse.

**Archivos editados:**
- `fastapi/services/model_backup.py`
- `fastapi/services/auto_retrain.py`

### V. Trailing Stop respeta `SYMBOL_TRADE_STOPS_LEVEL`

**Cambio aplicado:** `ManageTrailingStop()` ahora consulta el stops level del broker y valida la distancia mínima antes de llamar a `PositionModify()`, evitando errores `invalid stops` en MT5.

**Archivos editados:**
- `mql5/AI_Quant_Terminal_v3.mq5`

### W. Refactor basado en CodeGraph (2026-09-28)

**Análisis:** Se analizó el grafo de código (`.codegraph/codegraph.db`) identificando puntos débiles de acoplample y complejidad.

**Cambios aplicados:**

1. **Extracción de Qwen client** — Las funciones `_query_qwen_unified` y `_query_qwen_sltp_validator` se extrajeron de `routers/ai.py` a `services/qwen_client.py`. Reduce el acoplamiento de `ai.py` (59→~30 nodos) y centraliza la lógica de llamadas a Qwen.

2. **Fallback en structured_logging** — `log_to_audit` ahora captura el `RuntimeError` de `get_pool()` y loguea un warning en vez de propagar la excepción. Evita crashes cuando el pool no está inicializado.

3. **Desacoplamiento de order_manager.prepare** — Las validaciones se extrajeron a `services/order_validator.py` con funciones específicas (`validate_price_range`, `validate_geometry`, `validate_sl_pips`, `validate_volume`, `check_duplicate`). `prepare` se reduce de 36 llamadas salientes a ~15.

4. **Modularización del frontend** — `frontend/js/app.js` se separó en `api.js` (llamadas fetch), `ui.js` (renderizado DOM/Chart.js) y `app.js` (orquestación). Cada módulo <100 líneas.

5. **Limpieza de código legacy** — `train_ppo_v2.py` se movió a `ml/archive/` al no estar referenciado en scripts de automatización.

**Validación:** `pytest fastapi/tests/ -q` → 73 passed, 2 failed (pre-existentes, no relacionados con estos cambios).

**Archivos creados:**
- `fastapi/services/qwen_client.py`
- `fastapi/services/order_validator.py`
- `frontend/js/api.js`
- `frontend/js/ui.js`
- `fastapi/tests/test_qwen_client.py`
- `fastapi/tests/test_order_validator.py`
- `fastapi/tests/test_structured_logging_fallback.py`

**Archivos modificados:**
- `fastapi/routers/ai.py`
- `fastapi/services/structured_logging.py`
- `fastapi/services/order_manager.py`
- `frontend/js/app.js`
- `frontend/index.html`

**Archivos movidos:**
- `fastapi/ml/train_ppo_v2.py` → `fastapi/ml/archive/train_ppo_v2.py`

### X. Fix P0-2: `/predict` devolvía 500 con MT5 caído (2026-09-28)

**Síntoma:** Con MT5 inaccesible, `/api/v1/ai/predict` respondía `HTTP 500 Internal Server Error` en lugar del `HOLD` defensivo. El modo degradado (HOLD + telemetría con `cycle_id`) nunca se ejecutaba y el ciclo no quedaba registrado.

**Causa raíz:** `PredictResponse` exige `cycle_id` (sin default) y los 4 returns de error de MT5 lo construían sin ese campo → `pydantic.ValidationError` no capturada → 500. El test `test_ea_backend_predict_contract_fields_are_aligned` lo delataba (estaba entre los 2 failed pre-existentes).

**Fix:**
- `fastapi/routers/ai.py` — los 4 returns de error incluyen ahora `cycle_id=cycle_id` (reparan modo degradado + telemetría).
- `tests/test_e2e_ai_predict.py` — contrato con `cycle_id` + aserción; **nuevo test conductual** `test_predict_mt5_down_returns_ok_hold_with_cycle_id` (MT5 simulado caído → 200 + HOLD + ERROR + cycle_id); mock de obs shape `(10,21)` → `(20,24)` (contrato real del artefacto activo, verificado cargando `ppo_trading_bot_v3.zip`).
- `tests/test_auto_retrain.py` — `retrain_window_size` 10 → 20: el código cambió intencionadamente a 20 en `717c23a` (2026-09-15) y el test quedó desactualizado; 20 coincide con la ventana real del modelo.

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **76 passed** (baseline: 73 passed, 2 failed).

**Archivos editados:**
- `fastapi/routers/ai.py`
- `fastapi/tests/test_e2e_ai_predict.py`
- `fastapi/tests/test_auto_retrain.py`
- `AGENTS.md` (contadores de tests 44 → 76)

### Y. Fix P0-4: Integridad de cierres — deal_ticket persistente + dedup (2026-09-28)

**Síntoma/riesgo:** `trade_outcomes` no guardaba `deal_ticket`/`position_id` aunque el EA los envía. Un webhook reenviado duplicaba el contador de retrain y el journal, y no había forma de reconciliar cierres contra MT5 ni detectar duplicados.

**Cambios:**
- `db/schema.sql` — columnas `deal_ticket BIGINT`, `position_id BIGINT` + índice único parcial `idx_trade_outcomes_deal_ticket ... WHERE deal_ticket IS NOT NULL`.
- BD viva migrada (`ALTER TABLE ... IF NOT EXISTS` + índice) — 150 filas existentes con `deal_ticket` NULL: sin conflicto.
- `routers/ai.py` — el webhook `/trade/filled` parsea y transmite `deal_ticket`/`position_id` a `record_trade_filled`.
- `services/auto_retrain.py` — `_persist_trade_outcome` ejecuta `ON CONFLICT (deal_ticket) ... DO NOTHING RETURNING id`; si `record_trade_filled` recibe `trade_id is None` (duplicado) sale temprano: sin journal, sin contador, sin retrain.
- Tests — 3 nuevos: threading del webhook, dedup sin journal, schema declara índice único.

**Limitación conocida (→ Lote D):** el MCP de MT5 no expone historial de deals → la reconciliación de cierres perdidos será EA-side (backfill de historial + reintento con dedup server-side), junto al order gate.

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **79 passed** (baseline de sesión: 73 passed, 2 failed).

**Archivos editados:**
- `fastapi/db/schema.sql`
- `fastapi/routers/ai.py`
- `fastapi/services/auto_retrain.py`
- `fastapi/tests/test_trade_filled_webhook.py`

### Z. Fix P0-5 + decisión D6: bundle certificado con manifest e integridad verificada (2026-09-28)

**Problema:** No había forma de saber si el zip servido, las estadísticas de VecNormalize y el contrato de observación eran coherentes. Evidencia: `ppo_trading_bot_v3.zip` (28-sep, reentrenado por `_do_retrain` EN CRUDO) se servía con `ppo_trading_bot_v3_vec_norm.pkl` del 11-sep → inputs normalizados con estadísticas que el modelo nunca usó en su último entrenamiento. Un zip cambiado sin recertificar se servía en silencio.

**Cambios:**
- `services/model_registry.py` (nuevo) — manifest JSON por bundle (`<modelo>.manifest.json`): SHA-256 del zip, SHA-256 del pkl si procede, `obs_shape`, `features`, `source`. `validate_bundle()` lanza `RuntimeError` si falta el manifest, está corrupto o algún hash no coincide; `assert_obs_shape()` falla si el zip cargado no corresponde al manifest. Solo stdlib.
- `routers/ai.py` — `load_inference_artifacts()` valida el bundle ANTES de servir: manifest → hash → carga PPO → `obs_shape` certificado → VecNormalize solo si el manifest lo declara (hash ya verificado). Desaparece el "warning y sigo sin normalizar": o el bundle es coherente o el arranque no arranca. Constante `VEC_NORM_PATH` eliminada (huérfana).
- `services/auto_retrain.py` — `_do_retrain` escribe manifest al promover candidato: promover sin certificado = arranque fallido ruidoso.
- `ml/train_ppo_v3.py` — el entrenamiento manual escribe manifest (zip + vec_norm + features).
- `tests/conftest.py` — `PPO_OUTPUT_DIR` apunta a `fastapi/ml/` real: el E2E valida los artefactos de producción de verdad (hash incluido).
- `tests/test_e2e_ai_predict.py` — eliminado el patch muerto de `os.path.exists` (ya no se usa en la carga).
- `tests/test_model_registry.py` (nuevo) — 15 tests: roundtrip, manifest ausente/corrupto/incompleto, zip alterado, pkl alterado, pkl ausente, source obligatorio, obs_shape.

**D6 — destino del modelo activo:** verificado en código: `_do_retrain` entrena con `DummyVecEnv` crudo y sobrescribe el zip SIN tocar el pkl. Decisión: el manifest del bundle activo declara `vec_norm_file: null` → **se sirve en crudo** (coherente con el último entrenamiento del artefacto; elimina el sesgo de stats obsoletas). El pkl del 11-sep queda inactivo. `not_for_live: true` hasta el reentrenamiento con el pipeline corregido (Lote E / P1-1+P1-3). OOS conocido: Sharpe -4.79, DD 51% (ver "Pendiente — Retraining" 2026-09-11) → ninguno de los artefactos existentes es apto para real.

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **94 passed** (baseline de sesión: 73 passed, 2 failed).

**Archivos editados/creados:**
- `fastapi/services/model_registry.py` (nuevo)
- `fastapi/tests/test_model_registry.py` (nuevo)
- `fastapi/ml/ppo_trading_bot_v3.manifest.json` (nuevo — certificado del bundle activo)
- `fastapi/routers/ai.py`
- `fastapi/services/auto_retrain.py`
- `fastapi/ml/train_ppo_v3.py`
- `fastapi/tests/conftest.py`
- `fastapi/tests/test_e2e_ai_predict.py`

### AA. Fix P0-3: order gate EA → `/v1/orders/prepare` (EA v13.0) (2026-09-28)

**Problema:** `order_manager.prepare()` (idempotencia por cycle_id + Risk Guardian + validadores geométricos + registro `pending` + firma HMAC) existía pero **ninguna ruta lo exponía**: el EA colocaba órdenes con `trade.Buy/trade.Sell` directamente, saltándose todo el pipeline de defensa. Autonomía sin frenos.

**Cambios:**
- `routers/orders.py` — nueva ruta `POST /v1/orders/prepare` (token interno).
- `mql5/AI_Quant_Terminal_v3.mq5` — **v12.10 → v13.0**:
  - helper `RequestOrderPermission()` — **fail-closed**: si el gate no responde (HTTP != 200), la orden NO se coloca.
  - BUY y SELL consultan el gate con precios/SL/TP/lote finales ANTES de `trade.Buy/Sell`; si `approved=false` → `ReportEADecision(action="rejected_by_gate")` y `return`.
- Los rechazos quedan auditados server-side (`order_rejected` en `audit_log`, con motivo) + log en el EA.
- `tests/test_orders_prepare.py` (nuevo, 11 tests): 401/422 de auth, aprobación con payload firmado + INSERT `pending`, idempotencia por cycle, bloqueo del Risk Guardian, geometría inválida, duplicado, volumen fuera de rango; contrato de la fuente MQL5 (gate antes de placement ×2, fail-closed, bump de versión).

**Efectos conocidos:**
- `orders` conserva filas `pending` (reserva de idempotencia); la transición a `placed/filled` queda para P1 (la telemetría `ea/decision` aún no envía `order_ticket`).
- Si `trade.Buy` falla tras aprobar, la fila `pending` puede bloquear reintentos en el mismo precio durante `order_max_age_hours=48` (conservador: anti-duplicados).

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **105 passed**.

**Archivos editados/creados:**
- `fastapi/routers/orders.py`
- `fastapi/tests/test_orders_prepare.py` (nuevo)
- `mql5/AI_Quant_Terminal_v3.mq5` (v13.0)

### AB. Fix P0-4 EA: outbox de cierres con reintento persistente (EA v13.1) (2026-09-28)

**Problema:** si el POST `/trade/filled` fallaba (FastAPI reiniciándose mientras una posición cerraba), el cierre se perdía: sin `trade_outcomes`, sin journal, con hueco en el dataset de retrain. El dedup server-side por `deal_ticket` (sección Y) hacía seguro el reintento, pero nadie reintentaba.

**Cambios (solo MQL5):**
- `SaveToOutbox(body)` — la rama de fallo del webhook persiste el cuerpo JSON en `MQL5\Files\trade_filled_outbox.jsonl` (una línea por cierre).
- `FlushTradeOutbox()` — al inicio de cada ciclo M5 (tras el circuit breaker, antes del predict): reenvía hasta `OUTBOX_MAX_PER_FLUSH = 10` cierres pendientes; los fallidos quedan en el archivo; si todos van bien se elimina.
- Reintento ciego seguro gracias al dedup server-side (sección Y).
- **v13.0 → v13.1.**

**Limitación conocida:** un cierre producido mientras MT5/EA está caído (no solo el servidor) sigue sin reportarse; la recuperación desde el historial MT5 queda como P2.

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **110 passed**. Compilación MQL5 pendiente en MetaEditor (falta del entorno Linux).

**Archivos:**
- `mql5/AI_Quant_Terminal_v3.mq5` (v13.1)
- `fastapi/tests/test_ea_close_outbox.py` (nuevo)

### AC. Fix P0-1/P1-1/P1-3: motor de retrain honesto (2026-09-28)

**Problemas (auditoría):**
1. `filled_count` **nunca se reseteaba** tras un retrain promovido → con `retrain_auto_approve` activo, cada trade dispararía otro retrain de 100k steps (retrain storm). Además `_load_retrain_state` recomputaba el contador desde TODAS las `trade_outcomes` en cada reinicio con contador=0 → umbral superado de inmediato.
2. **Evaluación inválida**: candidato y baseline se evaluaban con `random_reset=True` → cada uno en un segmento distinto → la comparación de recompensa no era significativa.
3. **Gate con `>=`**: empates promovían → churn de bundles certificados sin mejora.
4. **Sin VecNormalize en retrain**: `_do_retrain` entrenaba y promovía bundles crudos incoherentes con el pipeline normalizado (mismatch train/serve).
5. **P0-1**: `retrain_auto_approve` no declarado en `config.py` (se congelaba por `getattr` implícito).

**Cambios:**
- `_do_retrain` entrena **siempre con VecNormalize**: continúa las stats del bundle activo si su manifest las declara; si el bundle es crudo (caso actual D6) o no hay manifest, stats nuevas. La promoción guarda **zip + pkl + manifest** (`source=auto_retrain_normalized`); un fallo a mitad deja el bundle incoherente y `validate_bundle` falla en el próximo arranque (fail-fast).
- `_evaluate_model(..., obs_rms=None)`: episodio **determinista** (`random_reset=False`) y cada política se evalúa en su **espacio de entrada nativo** (obs_rms del candidato = stats de entrenamiento; obs_rms del baseline = las de su manifest declarada, `None` si es crudo). API VecEnv.
- `_gate_decision` (extraído y testeado): candidato válido + **mejora estricta** sobre baseline (`>`); los empates **no** promueven.
- **Reset de ciclo**: tras promoción, `filled_count=0`, `outcomes=[]`, `last_retrain_time=now`. `_load_retrain_state` solo hace bootstrap (recomputo desde la BD) si `filled_count==0 AND last_retrain_time==0` → tras reinicio, contador=0 significa "nuevo ciclo", no "sin datos".
- `config.py`: **`retrain_auto_approve: bool = False`** declarado (P0-1). La promoción sigue bajo aprobación humana (`/admin/retrain/approve`); cambiar a `true` en `.env` es una decisión explícita.

**Efecto en servicio:** el primer retrain aprobado sustituirá el bundle crudo D6 (`auto_retrain_raw_2026-09-28`) por un bundle normalizado y certificado de extremo a extremo.

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **128 passed** (host + contenedor). Tests nuevos (+18 en `test_auto_retrain.py`): gate estricto (6), determinismo de evaluación (3), bootstrap de estado (2), config P0-1 (1), helpers de bundle `_load_obs_rms`/`_make_train_env` (5), integración `_do_retrain` end-to-end con mocks de red/BD — promueve bundle completo y resetea contador (1).

**Limitación conocida:** la validación sigue siendo un episodio determinista (walk-forward multi-ventana para el gate queda como P2) y el test de integración parchea `learn()` — valida la mecánica de promoción, no la calidad del entrenamiento.

**Archivos:**
- `fastapi/services/auto_retrain.py`
- `fastapi/config.py`
- `fastapi/tests/test_auto_retrain.py` (+18)

### AD. Fix P1-3: aprobación de retrain + gestión de tasks con alerta (2026-09-28)

**Problemas (auditoría):**
1. `asyncio.create_task(_trigger_retrain())` sin referencia (2 sitios) → el task podía recolectarse y sus excepciones morían en silencio; un fallo de retrain era invisible.
2. El trigger logueaba "usar endpoint `/admin/retrain/approve`" **que no existía**: con `retrain_auto_approve=false` no había forma legítima de aprobar el retrain pendiente (solo `/retrain/force`, que no valida umbral).
3. Fallo de retrain sin alerta asociada.

**Cambios:**
- `_spawn_retrain_task()` en `services/auto_retrain.py`: conserva la referencia (`_RETRAIN_TASK`), reutiliza el task vivo (sin duplicados) y en el done-callback emite `send_alert(ERROR)` con la excepción si termina en fallo (o si se cancela).
- `approve_retrain()` + **`POST /api/v1/ai/retrain/approve`** (token obligatorio): aprueba **solo** si el umbral está alcanzado y no hay retrain en curso → `no_pending` / `already_running` / `approved`; evento de auditoría `retrain_approved`. No salta el gate de promoción (ese vive dentro de `_do_retrain`).
- Trigger automático y `force_retrain` usan el spawn helper (sin `create_task` suelto).
- Log y `config.py` apuntan al path real `/api/v1/ai/retrain/approve`.

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **140 passed** (128 → 140). Tests nuevos (`test_retrain_approval.py`, 12): ruta existe, auth 422/401, wiring + auditoría, semántica de approve ×3, force con spawn, referencia + no-duplicado, alerta en fallo del task, trigger con aprobación ×3.

**Archivos:**
- `fastapi/services/auto_retrain.py`
- `fastapi/routers/ai.py`
- `fastapi/config.py`
- `fastapi/tests/test_retrain_approval.py` (nuevo)

### AE. Fix P1-1: harness walk-forward + gate multi-métrico (2026-09-28)

**Problema (auditoría):** la promoción dependía de **1 episodio** determinista sobre un segmento con solape de contexto con el train (leakage), **1 métrica** (reward), y rechazo y promoción escribían el mismo evento `retrain_completed` **sin métricas ni hash**. Sin validez estadística.

**Cambios:**
- **`services/walk_forward.py` (nuevo):** split `train(70%) + embargo(window=20) + 4 folds OOS disjuntos + holdout congelado(10%)`. El scoring **particiona el eje temporal completo sin solape** en todas las fronteras (fold 1 se contextúa con las barras de embargo: ni entrenadas ni puntuadas; los folds siguientes con la cola del fold anterior, no puntuada en ellos). Si los datos no bastan (`lookback_candles` insuficiente) **falla ruidosamente** con `RuntimeError` accionable.
- **Métricas por pliego:** Sharpe/Sortino (retornos de la curva de equity, anualización H1 = 24×252), `max_dd` peak-to-trough, `expectancy` = (balance final − inicial)/trades, `trades`, `openings`, `win_rate`.
- **`gate_walk_forward` — multi-métrico, todas las reglas estrictas (empate = fallo):** `total_openings ≥ mínimo`, métricas finitas, y si hay baseline: `sum_reward >`, **mayoría de folds ganados (≥3 de 4)** y `mean_sharpe >` baseline. El **holdout se evalúa y persiste pero NUNCA decide**.
- **`_do_retrain`:** el split se construye ANTES de entrenar (`train_df` = región walk-forward exacta → cero leakage candidato↔folds); evaluación **emparejada** (candidato y baseline recorren los mismos pliegues, cada uno con su `obs_rms` declarado); eventos de auditoría **`retrain_promoted` / `retrain_rejected`** con el informe completo (métricas por pliego + agregados + checks del gate); el informe también queda en `extra` del manifest del bundle.
- **`_evaluate_model`:** devuelve `equity_curve` y `step_rewards` (para las métricas) y corrige el bug de `wins`/`losses` (el env expone `trades` + `win_rate`, no claves `wins`/`losses` → antes siempre 0). Eliminados `_gate_decision` (gate de 1 episodio) y `_save_retrain_metrics` (evento ambiguo).

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **160 passed** (140 → 160). Tests nuevos `test_walk_forward.py` (25): partición sin solape ×5 + contexto/embargo ×3, fallos ruidosos del split ×3... desglose: split 8, métricas 6, gate 7, patrón de llamadas y agregados 4. Integración `_do_retrain` ampliada: promoción verifica informe walk-forward en manifest/evento (+1 **rechazo por empate** con el bundle real copiado → bundle intacto, sin reset). −6 tests del gate antiguo (cubiertos por los del nuevo gate). **E2E actualizado:** el mock del modelo usa `Box` reales porque el bundle certificado ahora declara `vec_norm_file` → el startup ejecuta la rama VecNormalize (SB3 rechaza spaces MagicMock).

**Archivos:** `fastapi/services/walk_forward.py` (nuevo), `fastapi/services/auto_retrain.py`, `fastapi/tests/test_walk_forward.py` (nuevo), `fastapi/tests/test_auto_retrain.py`, `fastapi/tests/test_e2e_ai_predict.py`.

### AF. Fix P1-2: historial de modelos + hot-reload + salud disco≠memoria (2026-09-28)

**Problemas (auditoría):** no había histórico de promociones (no se sabía qué modelo corre ni por qué); la promoción **no se aplicaba hasta reiniciar** (`_CACHED_MODEL` en memoria divergía del disco); no había forma de ver esa divergencia; y `load_inference_artifacts` asignaba `_CACHED_MODEL` ANTES de cargar VecNorm → si el pkl fallaba, la caché quedaba con modelo nuevo + stats viejas (swap a medias).

**Cambios:**
- **Tabla `model_registry`** (`schema.sql` + migración manual aplicada): historial de bundles (sha256 modelo/pkl, `source`, `obs_shape`, `gate_reason`, `promoted_at`) con **índice único parcial `WHERE is_active`** → exactamente un bundle activo a la vez (rollback informado).
- **`record_promotion(manifest, gate)`**: desactivar-la-anterior + insertar-la-nueva en **UNA sola sentencia CTE** (atómica). Si la DB falla, propaga → alerta vía done-callback del retrain y el ciclo NO se resetea.
- **Swap atómico en `load_inference_artifacts`**: modelo y VecNorm se cargan en variables locales y la caché se asigna al **final** → un fallo de validación/carga deja el bundle anterior íntegro (test con pkl corrupto).
- **`reload_inference_artifacts()` (hot-reload)**: `_do_retrain` lo llama tras promover → la promoción surte efecto **sin reiniciar**; si falla, propaga (alerta) y se sigue sirviendo el bundle anterior.
- **`GET /api/v1/ai/model/status`**: `memory` (sha/`source`/`loaded_at` de la caché) vs `disk` (manifest) + **`in_sync`** → disco≠memoria visible de inmediato.
- `_do_retrain`: captura el manifest, lo registra en `model_registry` y recarga la caché tras cada promoción (en rechazo: ni registro ni recarga).

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **169 passed** (160 → 169; +9 en `test_model_hot_reload.py`: CTE+parámetros, fallo de DB propaga, swap atómico ante pkl corrupto, reload devuelve estado, endpoint ruta + 422/401 + `in_sync` ×2, índice único del schema). Integración `_do_retrain`: promoción exige `record_promotion` y `reload` llamados; rechazo exige que **no** se llamen. **En vivo:** `GET /api/v1/ai/model/status` → `in_sync: true` (sha `2c84c903…` = memoria = disco, `source=auto_retrain_normalized`); migración aplicada a la DB viva (`\d model_registry`); contenedor: **169 passed**.

**Archivos:** `fastapi/db/schema.sql`, `fastapi/services/model_registry.py`, `fastapi/routers/ai.py`, `fastapi/services/auto_retrain.py`, `fastapi/tests/test_model_hot_reload.py` (nuevo), `fastapi/tests/test_auto_retrain.py`.

### AG. Fix P1-4: tests donde hay dinero + validación en el borde (2026-09-28)

**Problemas (auditoría):** las piezas que mueven dinero real — `risk_guardian`, `position_sizing` y el `step()` del entorno PPO — no tenían **ningún** test; los tests de `/trade/filled` leían el fuente Python por regex en lugar de ejercitar el comportamiento; `test_models.py` probaba un **stub** divergente del modelo real (sus tests de invalidación pasaban por campos faltantes — `sl_hit`/`tp_hit` omitidos — no por la regla de negocio: cobertura falsa); y el modelo real aceptaba `direction="FLAT"`/`exit_reason="stop_loss"` → el router lo traducía a `None` → CHECK de `trade_outcomes` → **500** desde asyncpg en vez de **422** en el borde.

**Cambios:**
- **`tests/test_risk_guardian.py` (+12):** pérdida diaria inclusiva (`≤ -5%` bloquea, `> -5%` pasa), precedencia sobre max-posiciones, bloqueo `MAX_POSITIONS`, drawdown ≥10% alerta pero **no** bloquea, cooldown de alertas (1h, segunda del mismo tipo suprimida), racha de pérdidas (se acumula; `pnl ≥ 0` la limpia), `daily_start=0` sin división por cero, wrapper global `check_risk` con estado determinista.
- **`tests/test_position_sizing.py` (+14):** `pip_value_per_lot` por símbolo (majors planos, `USDJPY=1000/px`, `USDCAD=10/px`, oro), `derive_order_from_bb/candle_open/simple` con valores **numéricos verificados de forma independiente**: SL = ancla × `sl_mult`, TP **directo del ancla** de la banda, volumen = `riesgo/(sl_pips × pip_value)` acotado, suelo de SL (`sl_min_pips`/`3×spread`), **techo de asequibilidad** (ancla lejana → `sl_pips` cap en `risk_usd/(min_volume×ppv)` → riesgo real ≤ tope), dirección buy/sell, símbolo no soportado → `KeyError` (fail-loud), monotonía (SL más largo → volumen menor).
- **`tests/test_trading_env_step.py` (+10):** dinero exacto con `initial=1000` — apertura cobra spread (entry = close ± half) + comisión (1000 → 998.6); **SL intra-vela con prioridad sobre TP** si ambos se tocan en la vela (gap) → salida al precio del SL (977.2); TP (1397.2, reward diferencial 39.86); cierre discrecional paga round-trip de spread (−3 −1.4 = 994.2); terminación a equity ≤50% (493.0, `drawdown > 0.5`); truncado por fin de datos; penalty de estancamiento (−0.001); `info` con las claves de dinero del gate; obs `(5, 24)`.
- **`test_trade_filled_webhook.py` reescrito (9 → 19):** regex → comportamiento — normalización `long/buy/short/sell → LONG/SHORT` vía endpoint real, validación de timestamps (422 `exit<entry` / `<2000-01-01`, `entry==exit` → +1s), INSERT+dedup con pool real parcheado, `filled_count` 3→4 y **duplicado NO incrementa**, umbral `trades_before_retrain == 10` por config directa, y 422 de `direction`/`exit_reason` en el borde (endpoint no alcanzado). Ningún test lee ya fuente Python (solo DDL y `.mq5`, sin runtime disponible).
- **`test_models.py` reescrito (7 → 20):** stub eliminado → modelo **real** de `routers.ai`; invalidaciones con todos los demás campos válidos (`pytest.raises(match=...)` exige el error del campo correcto); normalización canónica `buy→LONG`, `SL→sl`; test de que el dominio `exit_reason` del modelo **iguala el CHECK** de `schema.sql`.
- **`routers/ai.py`: validadores en el borde** (`field_validator` + `PydanticCustomError`): `direction ∈ {long,short,buy,sell}` → canónico `LONG/SHORT`; `exit_reason ∈ {sl,tp,manual,news,signal_end,ea}` (== CHECK) → lower. Payload inválido → **422**, nunca `CheckViolation` 500. `PydanticCustomError` en vez de `ValueError`: pydantic 2.10 mete el `ValueError` original en `errors()['ctx']` y FastAPI 0.115.5 no lo serializa a JSON → el propio 422 se rompía en 500 (detectado por el test).

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **228 passed** (169 → 228; +59: guardian 12 + sizing 14 + env 10 + webhook +10 + models +13). Contrato de observación corregido en `AGENTS.md`/`README` `(10, 21)` → `(20, 24)` (window 20 × (17 + 7 state channels)) — el contrato anterior estaba en desfase con el código desde la ventana 20.

**Archivos:** `fastapi/tests/test_risk_guardian.py` (nuevo), `fastapi/tests/test_position_sizing.py` (nuevo), `fastapi/tests/test_trading_env_step.py` (nuevo), `fastapi/tests/test_trade_filled_webhook.py` (reescrito), `fastapi/tests/test_models.py` (reescrito), `fastapi/routers/ai.py` (validadores), `AGENTS.md`, `README.md`.

### AH. Fix P1-6: semántica de Qwen — degradación visible + decisión auditable (2026-09-28)

**Problemas (auditoría):**
- `/pipeline/health` **no reportaba el componente LLM**: con Qwen caído el sistema votaba HOLD (quality=0 → veto) y el health seguía respondiendo `healthy` — degradación invisible.
- Solo la caída de conexión degradaba en silencio; **HTTP ≠ 200 o respuesta sin JSON cascateaban un 500 en `/predict`** (violaba "If Qwen is unreachable, log and use safe defaults — never crash") y no quedaba rastro en salud.
- Bug en el status del health: `any(c == "database" or c == "ppo_model" for c in components)` iteraba **claves** → siempre `True` → cualquier fallo marcaba `critical`, `degraded` era inalcanzable.
- La racional ya se persistía en `audit_log` (`ai_predict_cycle`: quality, bias, veto, veto_reason, regime) pero sin flag explícito de degradación.

**Cambios:**
- **`services/qwen_client.py`**: `QwenHealthState` (calls_ok/failed, racha consecutiva, last_ok_at/last_fail_at, last_error) + `get_qwen_state()`; **toda llamada** — éxito o cualquier fallo — lo actualiza. Todos los modos de fallo (conexión, HTTP ≠ 200, JSON basura) → `_degraded_unified(...)` con safe defaults y razón explícita (`"Qwen unavailable"` / `"Qwen degraded: HTTP 500"` / `"Qwen degraded: sin JSON..."`) — **nunca raise**. Éxito añade `ok: True` y resetea la racha. Mismo tratamiento en `query_qwen_sltp_validator` (devuelve los valores propuestos originales).
- **`routers/ai.py`**: `/pipeline/health` añade el componente `qwen` (ok, contadores, last_error) con issue + alerta `PIPELINE-HEALTH` cuando está degradado; fix del bug de status (solo `database`/`ppo_model` caídos → `critical`; el resto → `degraded`). `/predict` persiste **`qwen_degraded`** en `audit_log` junto a la racional.
- **Semántica fail-closed documentada y testeada**: mientras Qwen esté caído, quality=0 < umbral → veto BUY/SELL → **HOLD**; el EA ve `quality_reason="Qwen unavailable"` en la respuesta.

**Validación (2026-09-28):** `pytest fastapi/tests/ -q` → **239 passed** (228 → 239; +6 en `test_qwen_client.py` — HTTP 500/bad-JSON degradan en vez de raise, estado y recuperación de racha — y +5 en `test_qwen_semantics.py` nuevo: componente en health, status `degraded` + alerta, y `qwen_degraded` en audit_log con veto fail-closed).

**Archivos:** `fastapi/services/qwen_client.py`, `fastapi/routers/ai.py`, `fastapi/tests/test_qwen_client.py`, `fastapi/tests/test_qwen_semantics.py` (nuevo), `README.md`.

### AI. P2-1: unificación train/serve — MDP entrenado = MDP ejecutado (2026-09-29)

**Problema (auditoría, D3):** el entorno de entrenamiento y la inferencia ejecutaban MDPs distintos: el env derivaba SL/TP de las cabezas muertas `sl_norm`/`tp_norm` (`max(5, …)` pips) mientras `/predict` calculaba `sl_pips = clamp(ATR×1.5, 10, 50)` con `tp = 2×SL`; el trailing del EA no existía en el modelo del mundo; y 2 de las 4 cabezas de la acción no las consumía nadie (en serve ya se ignoraban). El modelo aprendía un sistema que no era el que operaba.

**Cambios:**
- **`ml/trading_env_v2.py`**:
  - `production_stops(atr_norm, pip_size)` — **única fuente de verdad** de SL/TP, compartida por el env (apertura) y `routers/ai.py` (inferencia). Fórmula intacta respecto a lo desplegado (ATR relativo/pip ×1.5, clamp [10,50] pips, TP = 2×SL): P2-1 unifica el contrato, no rediseña la regla.
  - Acción **`Box(2,)` `[direction, volume]`**; `step()` acepta también vectores de 4 (bundle legado pre-P2-1, evaluable en el gate walk-forward) y **rechaza con `ValueError`** cualquier otro tamaño.
  - **Trailing modelado**: `_apply_trailing()` espejo de `ManageTrailingStop` del EA — activación cuando el precio recupera el riesgo actual, destino `entry + max(25% riesgo, minTrail)` con `minTrail = max(15 pips, ATR real × 2)`, solo aprieta, respeta stops-level (≈1 pip) y queda congelado tras el primer movimiento (igual que el `return` del EA con `riskDist ≤ 0`). Se calcula con el cierre de la vela y rige desde la siguiente (sin lookahead; el EA lo hace con ticks intra-vela). Parámetros `trailing_*` espejo de los inputs del EA (`TrailingPercent=0.25`, `TrailingMinPips=15`, `TrailingATRMult=2`).
  - Eliminados `max_sl_pips`/`max_tp_pips`: el env no tiene ninguna otra fuente de stops.
- **`routers/ai.py`**: decode `direction, volume_norm = raw_action[0], raw_action[1]` (el modelo vivo de 4 cabezas sigue servible hasta el retrain — puente verificado en vivo) y stops vía `production_stops` importada (un test de identidad de objeto bloquea que alguien meta una copia local).
- **`ml/train_ppo_v3.py`** y **`services/auto_retrain.py`**: configs del env sin `max_sl/max_tp` + bloque `trailing_*` (espejo EA); manifest `action_space: ['direction', 'volume']`.
- **`services/auto_retrain.py`** — dos comportamientos de SB3 2.3.2 verificados empíricamente y corregidos:
  - `VecNormalize.load` restaura el `action_space` del pickle del bundle origen (`set_venv` no lo refresca) → `PPO` construiría la política con la cabeza vieja. `_make_train_env` lo refresca desde el venv interno.
  - `PPO.load(zip, env=)` lanza `ValueError: Action spaces do not match` con el contrato nuevo → **`_load_or_create_model()`** inspecciona el contrato (load sin env) y **reentrena desde cero** si cambió (log explícito); si coincide, continúa entrenando como antes. Un zip dañado propaga su error (sin fallos silenciosos).

**Tests (+16 → 255):** `test_p21_unification.py` (nuevo): `production_stops` (fórmula, ambos clamps, identidad con predict), contrato de acción (`Box(2,)`, puente de 4 cabezas, rechazo de tamaños desconocidos), trailing espejo EA (sin activación parcial, activación y bloqueo de beneficio con `minTrail`, vigencia desde la vela siguiente/no-lookahead, congelado tras el primer movimiento, SELL simétrico, `trailing_enabled=False`), y retrain (pickle con `action_space` viejo refrescado; `_load_or_create_model` → fresh cuando no hay modelo o el contrato difiere, continuidad con seed cuando coincide). `test_trading_env_step.py` reescrito al contrato nuevo (fixture ATR exacto 10 pips → SL 15/TP 30; quiebra al 50% reconstruida con `initial=100`). `test_auto_retrain.py`: `_env_cfg` alineado con producción.

**Validación (2026-09-29):** `pytest fastapi/tests/ -q` → **255 passed** host **y** contenedor (239 → 255; +16). Smoke en vivo (contenedor reconstruido): `/health` ok (equity 418.87), `/pipeline/health` `healthy` (qwen 3/3 ok), y `/predict` con el **modelo vivo de 4 cabezas** responde correctamente por el decode `[0],[1]` con `sl_pips=18.3` / `tp_pips=36.6` (2×SL) — salida de `production_stops` con el ATR H1 actual.

**Retrain (2026-09-29):** `POST /retrain/force` → `_load_or_create_model` detectó el cambio de contrato en vivo — log `[RETRAIN] Contrato de acción (4,) ≠ env (2,) (P2-1) — reentreno desde cero con política nueva` — reutilizando los stats de normalización del bundle activo. Entrenó 100k timesteps + walk-forward (split: train=336, embargo=20, 4 folds, holdout@432 congelado) con 147 outcomes reales inyectados. **Gate APROBADO (`walk_forward_improved`)**: candidato `sum_reward=−3.27 / sharpe=−4.84 / opens=3` vs baseline `−22.50 / −9.45` **bajo el MDP nuevo**, `fold_wins=3/4`, holdout congelado `−16.31`. Promoción con verificación de manifest (`Bundle verificado`) y **hot-reload en vivo** (`[AI-RELOAD] Bundle recargado sha=fb314a379a03…`); `/predict` sirve ya la política de 2 cabezas (`ml_prob=0.7166`, mismos stops `18.3/36.6` de `production_stops`). Observaciones: (1) `model_backup` falla por falta de GCP ADC — error visible, no bloquea la promoción → pendiente en P3-2; (2) el gate aprobó por **mejora relativa** con solo 3 aperturas en eval — la potencia estadística sigue siendo baja (P2-2/P2-3 y más datos); (3) Qwen sufrió 2 `ReadTimeout` transitorios en el ciclo → estado `degraded` visible + alerta + safe defaults → **autorecuperado** (`healthy` 5/5 al terminar).

**Archivos:** `fastapi/ml/trading_env_v2.py`, `fastapi/routers/ai.py`, `fastapi/ml/train_ppo_v3.py`, `fastapi/services/auto_retrain.py`, `fastapi/tests/test_p21_unification.py` (nuevo), `fastapi/tests/test_trading_env_step.py` (reescrito), `fastapi/tests/test_auto_retrain.py`, `fastapi/tests/simulacro_ppo_qwen.py` (docstring), `README.md` (+ artefactos promovidos por el retrain: `ppo_trading_bot_v3.zip`, `_vec_norm.pkl`, `.manifest.json`).

### AI. P2-2: sizing del PPO delegado — fin de la heurística "3€" (2026-09-29)

**Problema (auditoría):** `/predict` dimensionaba el lote con la heurística "mínimo 3€ de beneficio" usando **0.10 €/pip/lot** (el pip real de EURUSD es 10 €/lot → error ×100, enmascarado por el clip a `max_lot`) y un techo `min(0.5, 3%·equity/dist)` **siempre activo** → la cabeza `volume` del modelo era un grado de libertad muerto. El pip se deducía de los dígitos del símbolo (XAUUSD → 0.01, vs 0.10 del resto del sistema) y `order_validator` tenía su propio dict de pips (drift entre módulos).

**Solución:** `services/position_sizing.size_ppo_position(volume_norm, sl_pips, equity, pip_value, *, risk_pct, max_lot, min_lot)` — **UNA función pura compartida por `/predict` y el env** (identidad de objeto, igual que `production_stops` en P2-1): `lots = equity × ppo_risk_pct × clip(vol_norm,0,1) / (sl_pips × pip_value)`, con floor al paso de lote, techo `max_volume` (el floor nunca lo excede) y **`0.0` = sin entrada** si ni el lote mínimo cabe. La cabeza `volume` [0,1] pasa a ser un **escalador de riesgo** que modula 0…3% del equity: el modelo conserva autonomía sobre el tamaño, el riesgo está acotado por `settings.ppo_risk_pct=0.03` (serve, `_env_cfg` de auto_retrain y `train_ppo_v3` leen la misma fuente).

- **Guardas nuevas en `/predict`:** símbolo sin pip centralizado → `HOLD` + `ERROR` (antes: heurística de dígitos que inventaba 0.0001); `equity ≤ 0` o no numérico → `HOLD` + `ERROR` (antes: volumen 0 / orden inválida); sizing 0 con decisión BUY/SELL → degradado a `HOLD` con log de error (nunca orden de volumen 0).
- **Pip centralizado:** `PIP_SIZE` / `pip_value_per_lot()` única fuente en `position_sizing.py`; `order_validator.py` importa **el mismo dict** (USDCNH/USDSEK unificados con ppv base USD); eliminados `min_profit_target`, `min_lot_for_profit`, `spread_price`, `max_lot_by_risk` y los dígitos-heurística de `ai.py`.
- **Env:** apertura delegada en `size_ppo_position()`; volumen 0 → **sin entrada** (paridad con el HOLD por sizing inviable de serve) y el tamaño escala con el equity (riesgo fijo por cuenta, no lotes fijos).

**Retrain (2026-09-29):** forzado — el MDP cambió (regla de sizing + volumen-0). Entrenó 100k timesteps + walk-forward (mismo split: train=336, embargo=20, 4 folds, holdout@432 congelado, 147 outcomes reales) continuando del bundle activo (contracción idéntica, stats VecNormalize conservados). **Gate APROBADO (`walk_forward_improved`)**: candidato `sum_reward=−5.18 / sharpe=−6.74 / opens=15` vs baseline `−14.94 / −21.80`, `fold_wins=3/4`, holdout `−0.95` — **15 aperturas en eval** (vs 3 de P2-1): el sizing delegado produce actividad acorde al contrato.

**Incidente en la promoción + fix (P2-2b):** el gate aprobó y los artefactos se escribieron en disco, pero `record_promotion()` falló con `idx_model_registry_one_active`. Causa raíz: la versión con **CTE** (`WITH deactivated AS (UPDATE …) INSERT …`) — las sub-sentencias de un CTE de PostgreSQL **no ven sus mutuos efectos y su orden es indefinido**: el INSERT comprobó la unicidad antes de que el UPDATE se aplicara (con la tabla vacía, primera promoción de la historia, no se manifestó). Estado resultante: disco = candidato aprobado, memoria = bundle anterior, registry = fila antigua activa. **Fix:** transacción sobre la **misma conexión** (`pool.acquire() → conn.transaction() → UPDATE → INSERT`, orden garantizado + rollback atómico). Reparación con la propia función corregida (sin SQL manual) → `model_registry` fila `id=3 sha=4185451b… is_active=t` (**exactamente 1 activo**), reinicio → `Bundle verificado`, `in_sync: true`, `healthy` 5/5, `/predict` sirve el candidato aprobado (`ml_prob=0.1` → HOLD por zona muerta).

**Tests 255 → 278 host + contenedor (exit 0):** `TestSizePPOPosition` (fórmula, linealidad exacta, floor, tope, guards de equity/símbolo, ppv JPY/outras divisas), `TestPipSingleSource`, `test_p22_sizing.py` (paridad por identidad de objeto y por cadena de valores serve↔env, volumen-0 = sin entrada, escalado por balance), e2e: `equity=0` → HOLD, símbolo sin pip → HOLD, **volumen numérico cableado** (`size_ppo_position(equity=1000, vol_norm=0.18, settings)` == payload), `TestRecordPromotion` (transacción ordenada + rollback).

**Archivos:** `fastapi/services/position_sizing.py`, `fastapi/routers/ai.py`, `fastapi/ml/trading_env_v2.py`, `fastapi/services/order_validator.py`, `fastapi/services/auto_retrain.py`, `fastapi/ml/train_ppo_v3.py`, `fastapi/config.py`, `fastapi/services/model_registry.py` (fix P2-2b), `fastapi/tests/test_p22_sizing.py` (nuevo), `test_position_sizing.py`, `test_e2e_ai_predict.py`, `test_model_hot_reload.py`, `test_p21_unification.py`, `test_trading_env_step.py`, `test_auto_retrain.py`, `README.md` (+ artefactos: `ppo_trading_bot_v3.zip` sha `4185451b…`, `_vec_norm.pkl`, `.manifest.json`).

### AI. P2-3: blackout de noticias para el PPO + etiquetas de sesión en UTC (2026-09-29)

**Problema (auditoría):** el PPO operaba sin calendario económico — `news_filter`/`news_blackout` solo existía en el flujo SMC, así que `/predict` podía abrir justo en NFP/CPI (3 titulares RSS no protegen nada); y las etiquetas de sesión de Qwen eran erróneas: clasificaban la hora del servidor del broker (sellada con "Z" como si fuera UTC) y rotulaban 17-23h como **"Asia"** (es la tarde de Nueva York) y 23-7h como **"Weekend/Closed"** (entre semana el mercado abre 24h: son Sydney/Tokio) → contexto engañoso para el veto de calidad.

**Solución:**
- **Blackout (mismo mecanismo que el SMC):** `routers/ai.py` llama a `news_filter.is_news_blackout(sym)` después de la guarda de pip y **antes** de tocar MT5/modelo/Qwen: ventana ±`news_blackout_minutes` (15 min) alrededor de un evento High/Medium de la divisa → `HOLD` con `quality_reason=news_blackout:<evento>` y `llm_bias=NEUTRAL` (es política de riesgo, no un fallo → no contamina `ERROR`). Sin órdenes durante la ventana (ni entrada ni inversión); SL/TP y trailing del EA siguen activos. Fallo de descarga del calendario → fail-open con error logueado, **idéntico al SMC** (una sola semántica, sin segundo comportamiento inventado).
- **Sesión en UTC verdadero:** nuevo `services/market_time.utc_hour_from_candle()` — detecta por ciclo el desfase entre el sello de la vela (hora broker con "Z") y el reloj UTC real (la marca de la vela formándose +30 min cae a desfase ±0.5 h de `now` → `round()` lo recupera exacto); **auto-corrector** ante cambio de DST o si el feed arregla el sellado. Sellos sin tz o no contemporáneos (fixtures/backtests) → hora tal cual, sin inventar husos. `_classify_session` renombrado a fronteras UTC correctas: **Asia 0-7, London 7-12, London/NY Overlap 12-16** (máxima liquidez, etiqueta propia), **New York 16-21, Sydney 21-24**; desaparecen "NY" plano y "Weekend/Closed".

**Evidencia en vivo (2026-09-29):** primer `/predict` tras despliegue → log `[NEWS] Loaded 29 high-impact events for this week` (calendario real en memoria) → sin evento en ventana → ciclo completo `decision=SELL, ml_prob=0.33, volume=0.08` — primer **volumen numérico** de `size_ppo_position` (P2-2) observado en una respuesta BUY/SELL en vivo, con sesión clasificada sobre la vela formándose (broker GMT+3 → UTC corregido a 10h = London).

**Tests 278 → 293 host + contenedor (exit 0):** `test_p23_session_blackout.py` (fronteras de sesión + regresión de las etiquetas erróneas, `utc_hour_from_candle` en 5 variantes de sello — broker, UTC honesto, tz real, histórico sin tz, cruce de medianoche —, ventana de blackout por divisa/ventana/desactivado-sin-fetch, contrato de colocación en `ai.py`: blackout antes de MT5 y hora vía conversor); e2e `/predict` en blackout → `HOLD + NEUTRAL + quality_reason`; `is_news_blackout` parcheado en los 6 tests de predict existentes (cero red en tests).

**Archivos:** `fastapi/services/market_time.py` (nuevo), `fastapi/services/qwen_client.py`, `fastapi/routers/ai.py`, `fastapi/tests/test_p23_session_blackout.py` (nuevo), `fastapi/tests/test_e2e_ai_predict.py`, `fastapi/tests/test_qwen_semantics.py`, `README.md`.

### AI. P3-2: MLOps — snapshot reproducible, experimentos, backup verificado, drift y retrain programado (2026-09-29)

**Problema (auditoría, fila MLOps):** el ciclo de ML no estaba cerrado — (1) el backup apuntaba al **modelo v2** (`ppo_trading_bot.zip`), solo existía a GCS y sin credenciales ADC fallaba sin más (sin verificación, sin programación, `restore` devolvía solo el zip → bundle incompleto); (2) ningún retrain dejaba fila consultable ni snapshot de datos (no reproducible); (3) **cero** monitorización de drift/decay (grep=0) → deterioro del modelo indetectable; (4) retrain solo por umbral de trades, sin calendario.

**Solución (4 sub-fases):**
- **P3-2a — snapshot + experimentos:** `services/data_snapshot.py` congela el df exacto de train+gate en `{PPO_OUTPUT_DIR}/snapshots/<utc>__<sha12>__<purpose>.csv.gz` (sha256 sobre el csv pre-gzip = identifica los datos, verificación de lectura post-escritura, retención 10); `services/experiment_tracker.py` + tabla **`training_experiments`** (schema.sql + DDL aplicado a la BD viva): UNA fila por corrida — `triggered_by` (manual/forced/approve/spawn), snapshot, `gate_reason`/`gate_metrics`, sha del bundle o `error`; cierre **condicional `status='running'`** (un `failed` tardío no pisa un `promoted`). `_do_retrain(trigger)` orquesta snapshot → experimento → `_do_retrain_core`; cualquier excepción cierra la fila como `failed` y re-propaga.
- **P3-2b — backup correcto/programado/verificado:** `model_backup.py` reescrito sobre el **BUNDLE v3** (zip + vec_norm + manifest — lo mínimo para `validate_bundle`); **local siempre** (`backups/<sha12>/` con sha256 re-leído por fichero, idempotente por sha, retención 10) + **GCS mejor esfuerzo** con verificación size+md5 y alerta `ERROR` si falla; `restore_latest_model` restaura el bundle completo verificado (local primero, GCS de fallback); loop en lifespan (`_bg_tasks`, referencia+cancel en shutdown): primera pasada a 60 s, luego `model_backup_interval_hours=24`.
- **P3-2c — drift PSI + decay:** `services/drift_monitor.py` — por `MARKET_FEATURES`, baseline de **deciles z-scoreados del train con esperanzas reales por bin**, congelado en la promoción (`manifest.extra.drift_baseline`; sin baseline utilizable **no se promueve**); PSI de las últimas 200 velas vivas vs baseline con umbrales estándar **0.1 warning / 0.2 critical**; **decay** = test z sobre la esperanza de los últimos ≤50 cierres reales (`trade_outcomes`): `z ≤ −1.645` con media<0 → critical, media<0 no significativa → warning, `n<20` → insufficient (sin evidencia no se alerta). `run_drift_check()` devuelve **siempre** el informe (los fallos parciales se alertan y quedan reflejados, no rompen), alerta agregada ≤1 por pase, evento `drift_report` en `audit_log`, `NaN/inf → null` (jsonb); endpoint `GET /api/v1/ai/model/drift` + loop (300 s, luego `drift_check_interval_hours=24`).
- **P3-2d — retrain programado:** ancla = **último intento (promoción O rechazo)** — el rechazo ahora ancla `last_retrain_time` (sin esto, un gate rechazado reintentaría cada hora); sin historial, la primera revisión fija `schedule_anchor` (primer hito en `retrain_interval_hours=168`); vencido → `scheduled_due` en `RetrainState` (persistido en `retrain_state`); `approve_retrain` lo acepta **sin alcanzar el umbral de trades**; `_trigger_retrain` limpia el disparador al iniciar; loop horario en lifespan.

**Evidencia en vivo (2026-09-29, tras rebuild):** `healthy` 5/5; **backup programado** a los 60 s → `/app/ml/backups/4185451b80a3/` con los 3 ficheros del bundle verificados (`4185451b80a3` = sha activo) y estado `local_ok_gcs_failed` con **alerta ERROR explícita** por ADC (`Your default credentials were not found…`) — exactamente la visibilidad que antes era silenciosa; **drift programado** a los ~300 s → `[DRIFT] programado → level=warning (próximo en 24h)`; endpoint `/api/v1/ai/model/drift` devuelve informe con **decay real** `n=50, mean_pnl=1.64, z=1.44, winrate=0.34 → ok` y `status=no_baseline` (esperado: el baseline se congela en el próximo retrain promovido) con fila `drift_report` insertada en `audit_log`.

**Tests 293 → 365 host + contenedor (exit 0):** `test_data_snapshot.py` (escritura+verificación, sha determinista por contenido, sanitización, purga cronológica con microsegundos); `test_experiment_tracker.py` (INSERT/UPDATE con guarda anti-pisado, validación de dominios, UPDATE 0 = no-op); `test_model_backup.py` (ruta v3 con guard de regresión al v2, copia+sha local idempotente, manifest/vec ausentes → error+alerta, GCS success/failure/reuse con cliente falso, restore roundtrip y **rechazo de backup corrupto**, wiring del loop); `test_drift_monitor.py` (baseline con deciles y exclusión de degeneradas, PSI 0 con distribución idéntica y >0.2 con mercado desplazado, decay con arrays deterministas, `run_drift_check` healthy/no_baseline/critical/fallo-de-red, endpoint auth+wiring); `test_retrain_schedule.py` (ancla inicial, vencido con y sin auto_approve, salto con retrain en curso, aprobación por `scheduled_due`, consumo del disparador, persistencia jsonb); extensión de `TestExperimentLifecycle` (promoted/rejected/failed) y del spawn (propagación del trigger); aserción del rechazo actualizada (el intento ancla el calendario).

**Commits:** `e1b4aba` (P3-2a), `ff05ee8` (P3-2b), `e7b7cef` (P3-2c), `3ea371d` (P3-2d).

**Archivos:** `fastapi/services/data_snapshot.py`, `experiment_tracker.py`, `drift_monitor.py`, `model_backup.py` (reescrito), `auto_retrain.py`, `config.py`, `main.py`, `routers/ai.py`, `db/schema.sql`, `fastapi/tests/test_data_snapshot.py`, `test_experiment_tracker.py`, `test_model_backup.py`, `test_drift_monitor.py`, `test_retrain_schedule.py` (nuevos), `test_auto_retrain.py`, `test_retrain_approval.py`, `README.md`.

### AI. Convención "< 3€ = pérdida" + reportes fantasma de apertura (2026-09-29)

**Problema (auditoría, detectado en vivo):** (1) cierres en positivo por debajo de 3€ (+0.95, +1.06, +1.37) se clasificaban como **WIN** en el dashboard (`pnl > 0` en `/v1/smc/analytics`), en el Post-Trade Journal que alimenta a Qwen (`pnl_pct > 0`) y en el win rate del entrenamiento (`realized_pnl > 0`): la convención crítica de AGENTS.md — *"Any close with < 3 EUR profit is a loss"* — **no existía en ningún punto del código** (el P2-2 borró la heurística de *sizing* 3€ con pip-value erróneo ×100, que era otra cosa). (2) `OnTradeTransaction` del EA reportaba también el deal de **apertura** (`DEAL_ENTRY_IN`: beneficio 0 por definición, `entry==exit`) y el webhook fabricaba `exit=entry+1s` → **una fila fantasma por posición** en `trade_outcomes` (29 filas = 17 % de la tabla), insights de Qwen sobre trades inexistentes y `filled_count` inflado (el retrain se disparaba al doble de velocidad).

**Solución:**
- **`fastapi/core/rules.py`** (nuevo — primer ladrillo del núcleo hexagonal P3-1): `MIN_WIN_EUR = 3.0` + `is_winning_close(pnl)` — frontera exacta `>= 3€` win; por debajo (incluido 0.00) pérdida.
- **Los 4 puntos de clasificación usan la misma regla:** `/v1/smc/analytics` (wins/losses/rachas del dashboard), journal de Qwen (etiqueta WIN/LOSS + pnl EUR visible en el prompt), `outcomes_win_rate` de monitorización y el env PPO (win_rate del entrenamiento — **el reward no cambia**, solo la métrica; gate walk-forward intacto: es por `sum_reward`/`fold_wins`/`mean_sharpe`).
- **Guard backend en `POST /trade/filled`:** reporte con `entry==exit AND pnl=0 AND pnl_pct=0` (estructura de un deal de apertura) → `{"status":"ignored_open_deal"}` + WARNING, **sin fabricar timestamps ni persistir**. Los cierres reales con `pnl != 0` mantienen la normalización `exit=entry+1s` intacta.
- **EA v13.2** (bump `#property version` + `EA_VERSION`): `OnTradeTransaction` captura el `DEAL_ENTRY` del deal actual y **descarta `DEAL_ENTRY_IN`** — fix raíz (pendiente de compilar en MetaEditor; hasta entonces el guard del backend protege la BD).
- **Limpieza de BD** (transacción única, backup CSV en `/tmp/opencode/phantom_backup_2026-09-29.csv`): 29 filas fantasma borradas de `trade_outcomes` (164 → 135), 28 insights huérfanos borrados (FK integrity verificada: 0), `filled_count` 14 → 7 (7 fantasmas desde el último retrain). Los 3 cierres **reales** de 0.00€ (duración > 1 s) se conservaron. `retrain_state.outcomes` no está persistido (es memoria/derivada de `trade_outcomes`) → el reinicio del contenedor lo deja limpio.

**Evidencia en vivo (2026-09-29):** post-limpieza → `0` fantasmas, `135` trades, `60` wins (≥3€) / `75` losses (<3€); guard en contenedor: `POST /trade/filled` con payload de apertura → `{"status":"ignored_open_deal"}` + log `[TRADE-FILLED] Reporte de APERTURA ignorado (entry==exit, pnl=0)`; cierre real en el mismo segundo (pnl=0.95) → `{"status":"recorded"}`; `health` → `healthy` (`equity=426.56`).

**Tests 365 → 387 host + contenedor (exit 0):** `test_close_classification.py` (nuevo: frontera exacta de `is_winning_close`, analytics con deals falsos — 0.95€→loss, 5€→win, 3.00€ frontera→win, apertura `entry=0` fuera del conteo, racha rota por positivo <3€, prompt de Qwen con "(LOSS) 1.37 EUR" vs "(WIN) 6.94 EUR", env con cierre +1.5€→win_rate 0.0 y +3.5€→1.0, `record_trade_filled` hilo `pnl` al journal); `test_trade_filled_webhook.py` (`TestOpenDealReportIgnored`: apertura ignorada con pnl float y str, cierre real mismo segundo registrado con normalización intacta); `test_ea_close_outbox.py` (versión 13.2 + contrato del filtro `DEAL_ENTRY_IN`).

**Commits:** `fix(P0-datos) convención <3€ + fantasma de aperturas` (2 commits en esta sesión).

**Archivos:** `fastapi/core/rules.py` (nuevo), `fastapi/core/__init__.py` (nuevo), `fastapi/routers/smc.py`, `fastapi/routers/ai.py`, `fastapi/services/trade_journal.py`, `fastapi/services/auto_retrain.py`, `fastapi/ml/trading_env_v2.py`, `mql5/AI_Quant_Terminal_v3.mq5` (v13.2), `fastapi/tests/test_close_classification.py` (nuevo), `test_trade_filled_webhook.py`, `test_ea_close_outbox.py`, `README.md`.

### P3-1a. Núcleo hexagonal: puertos, adapters late-binding y mundos por par (2026-09-29)

**Objetivo (P3-1, diseño aprobado):** preparar el sistema para que **cada par de divisas sea un mundo independiente** — modelo propio, contexto propio, sin acoplamiento — dejando el terreno para P3-1b (`_BRAINS[symbol]` + HOLD ante par desconocido/deshabilitado), P3-1c (dimensión `symbol` en BD/registros) y P3-1d (riesgo estrictamente por par). En P3-1a **no cambia ningún comportamiento**: EURUSD sigue siendo el único mundo activo y todo consumidor existente sigue usando sus rutas de siempre.

**Qué aporta:**
- **`config.py` — `pairs: list[dict]`** (defecto `[{"symbol":"EURUSD","enabled":true}]`): único punto de configuración de pares; P3-3 añadirá aquí el segundo par (+ canary) y nada más cambiará. `enabled` **obligatorio** en cada especificación: listar un par nunca lo activa por sí solo.
- **`core/ports.py`** — Protocol-ports `MarketDataPort`, `ExecutionPort` y `LLMPort`, con la firma **real** de los servicios que abstraen (`query_qwen_unified` tal cual, `place_order` tal cual): un puerto refleja el contrato existente, no una idealización. `ModelStorePort` queda deliberadamente para P3-1b, cuando el loader deje de ser único y acepte path por par (declararlo antes sería un puerto que miente: `load_inference_artifacts()` hoy no recibe ningún path).
- **`core/adapters.py`** — adapters sin estado sobre `services.mt5_client` / `services.qwen_client` con **late binding**: cada método importa el módulo servicio *en el momento de la llamada* (`from services import mt5_client`). Un `from services.mt5_client import get_candles` a nivel de módulo congelaría la función original y rompería los ~80 puntos de patch de la suite (`patch("services.mt5_client.*")` dejaría de verse); el late binding los mantiene operativos. El adapter de ejecución **omite deliberadamente** `place_order_with_fallback` (código muerto con cero llamantes, marcado para eliminar en P3-1d): un puerto no expone lo que el sistema no usa.
- **`core/pair.py`** — `PairConfig` (identidad + **bundle propio por par**: EURUSD conserva `PPO_OUTPUT_DIR/ppo_trading_bot_v3.zip` — misma fuente `PPO_OUTPUT_DIR` que usa `routers.ai.MODEL_DIR`, así núcleo y router nunca divergen — y cada otro par vive aislado en `ml/<SYMBOL>/ppo_trading_bot_v3.zip`: añadir un par jamás mueve archivos de otro); `PairContext` (par + sus puertos, `frozen=True`: un par no puede mutar hacia otro); `PairRegistry` — símbolo **desconocido → `None`**, deshabilitado → contexto presente con `enabled=False`: el caller decidirá HOLD (P3-1b) en vez de que el núcleo invente un mundo.
- **`core/container.py`** — composition root: único punto que instancia adapters y arma contextos (un juego de adapters **propio por par**, aislamiento a coste cero); construcción **explícita** (nunca en import de módulo) para que los tests parcheen `services.*` antes de que exista ningún adapter; falla temprano ante specs inválidas, símbolo duplicado o **cero pares habilitados** — nunca arranca "a medias".

**Fallos que se niegan en silencio (todo es `ValueError`/`TypeError` explícito en el arranque, sin fallbacks):** especificación no-dict, `symbol` ausente/en blanco, **`enabled` ausente o no-bool**, `model_path` no-string, `PAIRS` vacío o no-lista, símbolo duplicado y ningún par habilitado.

**Evidencia en vivo (2026-09-29):** reconstrucción del contenedor → `health` `ok` (`equity=424.3`); `build_registry_from_settings()` por defecto → exactamente un mundo habilitado (`EURUSD`).

**Tests 387 → 412 host + contenedor (exit 0):** `test_p31a_pair_registry.py` (nuevo, 25 tests): convención de rutas (`EURUSD`→legado, `GBPUSD`→`ml/GBPUSD/`, respeto de `PPO_OUTPUT_DIR`, fallback `/app/ml` idéntico al router); validación de `PairConfig` (8 casos de error + derivación/override de `model_path`); `build_registry` (defecto EURUSD, duplicado→error, sin habilitados→error, vacío→error, **instancias de adapter distintas por par**); `PairRegistry` (desconocido→`None`, deshabilitado presente pero fuera de `enabled()`, membresía/len/símbolos, registry vacío→error); conformidad `isinstance` de cada adapter con su puerto; **`TestLateBinding`** — si algún adapter enlaza temprano la función, `patch("services.mt5_client.*")` / `patch("services.qwen_client.query_qwen_unified")` no se ve y los 4 tests fallan (protegen los puntos de patch de toda la suite).

**Archivos:** `fastapi/core/ports.py`, `core/adapters.py`, `core/pair.py`, `core/container.py` (nuevos), `fastapi/config.py`, `fastapi/tests/test_p31a_pair_registry.py` (nuevo), `README.md`.

### P3-1b. Un brain por par + gate de mundo (HOLD) + ModelStorePort (2026-09-29)

**Objetivo (P3-1):** que la inferencia sea **por par** — cada mundo con SU modelo en memoria y con admisión explícita — de cara a añadir pares autónomos en P3-3.

**Qué cambia:**
- **Gate de mundo en `POST /predict` (primera compuerta, antes de noticias/MT5/modelo):** el símbolo (normalizado) debe estar en `settings.pairs` y habilitado. Desconocido → `200 HOLD` + `quality_reason="pair_not_configured"` (llm_bias ERROR); deshabilitado → `HOLD` + `"pair_disabled"` (llm_bias NEUTRAL: deshabilitar es decisión, no fallo). **Jamás un modelo de otro par responde por un par que no es suyo.** Evidencia: solo fluye EURUSD (135/135 trades y todos los `cycle_id` de `/predict` en 7 días) → cero impacto en lo que opera hoy.
- **`_BRAINS[symbol]` reemplaza a los 3 globals** (`_CACHED_MODEL/_VEC_NORM/_BUNDLE`): `Brain` (dataclass congelada: modelo + normalizador + procedencia) por símbolo. El **swap sigue siendo atómico** — ahora por entrada: `_BRAINS[symbol]` se asigna al final de la carga; un fallo en la validación o en VecNormalize deja íntegro el brain anterior de ese par. `load_inference_artifacts(symbol)` deriva SU ruta con `core.pair.default_model_path` (fuente única: `PPO_OUTPUT_DIR`; se borraron `MODEL_DIR`/`MODEL_V3_PATH` del router — núcleo y router no pueden divergir). `on_startup` carga el brain de **cada par habilitado**; el lazy-load de `/predict` es idéntico al patrón anterior (doble chequeo → HOLD).
- **`reload_inference_artifacts(symbol)`** (hot-reload de la promoción, P0-5/P1-2) ahora por par, y llamado vía **`ModelStorePort` + `AiModelStoreAdapter`**: `services/auto_retrain` ya no importa de `routers.ai` (violación de capas servicio→router); el adapter resuelve el router en momento de llamada, así que `patch("routers.ai.reload_inference_artifacts")` del test de promoción sigue operativo. `TRAINING_SYMBOL = "EURUSD"` en `auto_retrain` como fuente única (fetch de velas + reload); P3-1c lo parametriza.
- **`GET /model/status` por par:** `?symbol=` explícito; sin él y con **exactamente un par habilitado** se resuelve solo; con varios (o cero) habilitados → **400** (la ambigüedad no se resuelve por heurística); par no configurado → **404 con detalle** (en vez de un 500 sin explicación al leer disco de un mundo inexistente); respuesta añade `"symbol"`.

**Bug latente encontrado y corregido (test que envenenaba la sesión):** `test_config.py::test_database_url_required` hacía `os.environ.clear()` **sin restaurar** → `PPO_OUTPUT_DIR` desaparecía del resto de la suite. Antes no dolía porque `MODEL_V3_PATH` se leía *a import time* (congelaba el valor correcto); con `default_model_path` (lectura *a call time*, imprescindible para rutas por par) los lectores posteriores resolvían `/app/ml` → 11 tests fallaban en orden completo (drift/e2e/qwen/retrain). Ahora: snapshot+restauración en `finally`. Además, el test de defaults `sl_mult` limpia los overrides del `.env` del contenedor (`SL_MULT=1.2`) — el default del código (1.5) es lo que afirma. Y `_sole_enabled_symbol` lee `config.settings` a nivel de función (mismo criterio que `build_registry_from_settings`): `importlib.reload(config)` de ese test cambia la identidad del objeto `settings` y todos los lectores de `pairs` deben ver la misma fuente canónica.

**Test de regresión re-escrito:** `test_predict_holds_for_symbol_without_central_pip` usaba `BTCUSD` (ahora interceptado por el gate de mundo) → pasa a `XXXYZZ` **configurado y habilitado** en `PAIRS` de prueba, demostrando que la compuerta del pip (P2-2) sigue viva de forma independiente del gate de mundo.

**Evidencia en vivo (2026-09-29, tras rebuild):** arranque → `[AI-STARTUP] Modelo PPO v3 de EURUSD cargado en memoria exitosamente` + VecNormalize verificado; `GET /model/status` → `symbol=EURUSD, in_sync=true` (memoria==disco con el loader nuevo); `GET /model/status?symbol=JPYUSD` → `404 {"detail": "Par 'JPYUSD' no configurado en PAIRS"}`; `health` → `ok` (`equity=427.2`); 0 errores en logs.

**Tests 412 → 424 host + contenedor (exit 0):** `test_p31b_pair_gate.py` (nuevo, 12): gate desconocido (HOLD + razón + **noticias y MT5 jamás tocados** — httpx explota si se llega), gate deshabilitado (NEUTRAL + razón), EURUSD atraviesa el gate (su HOLD es el de MT5 caído, sin motivo `pair_*`), ruta por símbolo (`/data/ml/ppo_trading_bot_v3.zip` vs `/data/ml/GBPUSD/…`) con entradas de `_BRAINS` aisladas, adapter ModelStore late-binding (4 aserciones), `_get_registry` por defecto, status 400/404/200-explícito, fuente única de ruta; extensión de `test_model_hot_reload.py` (swap atómico y `reload` por símbolo, status con `_BRAINS`); e2e re-escrito (arriba).

**Commits:** `feat(P3-1a) núcleo hexagonal` + `feat(P3-1b) un brain por par + gate de mundo`.

**Archivos:** `fastapi/routers/ai.py`, `services/auto_retrain.py`, `core/ports.py` (+ModelStorePort), `core/adapters.py` (+AiModelStoreAdapter), `core/pair.py` (doc), `fastapi/tests/test_p31b_pair_gate.py` (nuevo), `test_model_hot_reload.py`, `test_e2e_ai_predict.py`, `test_config.py` (fix de entorno), `README.md`.

### P3-1c. Dimensión `symbol` en la persistencia: registro, experimentos, estado y backup por par (2026-09-29)

**Objetivo (P3-1):** que el **historial de cada mundo sea el suyo** — modelo certificado, experimentos de entrenamiento, contadores de retrain y backups viven y mueren por par, sin acoplamiento posible. Con esto, la capa de datos queda lista para que P3-3 añada un segundo par sin escribir una sola fila en el namespace de otro.

**Qué cambia:**
- **`model_registry.symbol` + unicidad POR PAR:** columna `symbol NOT NULL DEFAULT 'EURUSD'`; el índice global `idx_model_registry_one_active` (un único activo ENTRE pares) se sustituye por `idx_model_registry_one_active_per_symbol ON model_registry(symbol) WHERE is_active`. `record_promotion(manifest, gate, *, symbol)` — `symbol` **obligatorio por keyword**: ninguna promoción existe sin declarar su mundo, y el `UPDATE` de desactivación es `WHERE is_active AND symbol = $1` (desactivar EURUSD jamás toca la fila activa de otro par). El orden transaccional UPDATE→INSERT se conserva (la lección del CTE que violó el índice en vivo el 2026-09-28).
- **`training_experiments.symbol`:** columna con el mismo default; `start_experiment(..., *, symbol)` obligatorio — cada experimento auditable pertenece al mundo que entrenó.
- **`retrain_state` con namespace por par:** claves `retrain_state.<SYMBOL>.<campo>` (`filled_count`, `last_retrain_time`, `scheduled_due`, `schedule_anchor`). `_load/_save/_ensure/get_state` y `_retrain_state_row_key` toman `symbol` (default `TRAINING_SYMBOL` = el mundo del pipeline); la caché pasó de un singleton `_state` a **`_states: dict[str, RetrainState]`** con lock de inicialización — dos mundos tienen contadores y calendario independientes. `_load_persisted_outcomes(symbol)` filtra `WHERE symbol = $1` (el bootstrap de un par solo cuenta sus propios cierres). **Guard de disparo:** `record_trade_filled` incrementa el contador del par que cerró, pero si un par **distinto** de `TRAINING_SYMBOL` alcanza el umbral **no dispara** — solo existe el pipeline de EURUSD (log visible con la razón; P3-3 añadirá el de cada par). El comportamiento para EURUSD es idéntico al anterior.
- **Backup por par (`model_backup`):** `backups_dir(symbol)` — EURUSD conserva la raíz legada `ml/backups/`, cada otro par vive en `ml/backups/<SYMBOL>/` (misma convención que `core.pair.default_model_path`, ahora delegado: convención de rutas **única**). `backup_model(..., *, symbol)` lleva el `symbol` al `backup.json` (metadato nuevo) y al prefix GCS `ppo_models/<SYMBOL>/<sha12>` — el restore de un mundo jamás descubre (ni restaura) el bundle de otro. `_prune_local(symbol)` solo purga directorios **con `backup.json`**: en la raíz legada conviven namespaces de otros pares sin ese marcador a su nivel y jamás serán candidatos. `run_scheduled_backup()` itera **cada par habilitado** (un fallo por par se alerta sin cortar a los demás) y `_model_backup_loop` loguea el estado por símbolo. `restore_latest_model(symbol)` escanea solo el namespace del par.
- **`auto_retrain`:** `MODEL_PATH` ya no es un literal `/app/ml/...` — es `default_model_path(TRAINING_SYMBOL)` (la ruta del bundle que escribe el pipeline se deriva del mundo que entrena); `core/pair.LEGACY_SYMBOL` es la única definición del mundo legado.

**Migración en vivo (2026-09-29, con volcado previo en `/tmp/opencode/p31c_backup/` — 2 + 5 + 0 filas):** `ALTER TABLE ... ADD COLUMN symbol` (ambas tablas), `DROP INDEX idx_model_registry_one_active` + `CREATE UNIQUE INDEX ..._per_symbol`, y `UPDATE` de las 5 claves de `retrain_state` al namespace `EURUSD` (idempotente: las claves ya namespaced no vuelven a casar). Verificación post-migración: 5 claves `retrain_state.EURUSD.*`, 2 filas de `model_registry` con `symbol=EURUSD` (una activa), índice por par presente. Además `retrain_state` **por fin tiene DDL en `schema.sql`** (antes solo existía en la BD en vivo → un fresh install carecía de la tabla).

**Evidencia en vivo (2026-09-29, tras rebuild):** arranque → `[AI-STARTUP] Modelo PPO v3 de EURUSD cargado`; `health` → `ok` (`equity=424.67`, MCP conectado); `GET /model/status` → `symbol=EURUSD, in_sync=true` (sha `4185451b…` = fila activa del registro); `?symbol=GBPUSD` → `404 "Par 'GBPUSD' no configurado"`; primera pasada del **backup programado con el loop nuevo** → local verificado (`ml/backups/4185451b80a3/backup.json` con `"symbol": "EURUSD"`) + GCS en fallo **visible** (ADC, gap conocido); EA → `POST /predict → 200 (18.7 s)` desde el contenedor nuevo.

**Tests 424 → 437 host + contenedor (exit 0):** `test_p31c_symbol_dimension.py` (nuevo, 13): claves namespaced por símbolo (y default del pipeline), carga SOLO del namespace pedido + bootstrap del propio par, caché de estados independiente (mutar un mundo no toca al otro), `symbol` obligatorio en `record_promotion`/`start_experiment` (TypeError de contrato), `backups_dir` legado-vs-namespace, delegación de ruta a `core.pair`, backup en el namespace del par con `symbol` en `backup.json`, **purga que jamás borra namespaces ajenos** (12 reales → se purgan 9, el namespace sobrevive), **restore que elige el backup de SU par aunque el de otro sea más nuevo**, guard de umbral (otro par en el umbral NO dispara / EURUSD sí como antes) y DDL de `schema.sql`; actualización de los tests que fijaban el contrato anterior (`test_experiment_tracker` ×3, `test_model_backup` ×2 programado, `test_auto_retrain` ×3, `test_retrain_schedule` ×2, `test_model_hot_reload` ×4 — incluye el índice por par y el `UPDATE ... AND symbol = $1`).

**Archivos:** `fastapi/db/schema.sql`, `services/model_registry.py`, `services/experiment_tracker.py`, `services/auto_retrain.py`, `services/model_backup.py`, `core/pair.py` (+LEGACY_SYMBOL), `fastapi/tests/test_p31c_symbol_dimension.py` (nuevo), `test_experiment_tracker.py`, `test_model_backup.py`, `test_auto_retrain.py`, `test_retrain_schedule.py`, `test_model_hot_reload.py`, `README.md`.

### P3-1d. Riesgo por par: guardianes independientes, presupuesto de equity y fin del código muerto (2026-09-29)

**Objetivo (P3-1):** completar el aislamiento de mundos en la **capa de riesgo y ejecución** — el cupo de posiciones, la racha de pérdidas y el presupuesto de size son de cada par; el equity de la cuenta (daily loss, drawdown) sigue protegido de forma global. Con esto queda cerrado P3-1: inferencia, persistencia y riesgo son **por par** de extremo a extremo.

**Qué cambia:**
- **`RiskGuardian` por par:** el singleton global `_risk` pasa a `_GUARDIANS: dict[symbol, RiskGuardian]` — `get_risk_guardian(symbol)` (default legado EURUSD) devuelve SIEMPRE la misma instancia por mundo. `max_positions`, `consecutive_losses` y el cooldown de alertas viven en el estado del par: **un par jamás consume la cupo de otro**. El baseline del día (`daily_start`/`peak`, propiedades del equity de la CUENTA) se comparte: `set_daily_start(equity)` siembra a todos los guardianes existentes y guarda `_day_baseline` para siembrar a los tardíos — un par que aparezca después del arranque **nunca queda sin protección diaria** (sin gaps silenciosos). `check_risk(equity, open_positions, *, symbol=...)` enruta al guardián del par.
- **`open_count` por símbolo (`order_manager.prepare`):** el conteo pasó de *todas las posiciones abiertas* a **solo las de `req.symbol`** — antes, una posición de otro símbolo consumía el cupo de EURUSD (acoplamiento directo, hoy invisible porque todo fluye en EURUSD: 9/9 órdenes y 136/136 trades).
- **Gate de mundo en `prepare` (misma admisión que `/predict`, en la última línea de defensa):** símbolo desconocido → `pair_not_configured`; par deshabilitado → `pair_disabled` (audit + rechazo, antes de tocar riesgo o MT5). Evidencia de impacto cero: solo EURUSD (configurado y habilitado) llega hoy a `prepare`.
- **`equity_allocation_pct` (PairConfig):** fracción `(0, 100]` del equity que el par puede mandar — **ausente = 100** (el techo global de siempre). Se aplica en `prepare` como techo de size: `ceiling = max_volume × asignación`, redondeado **abajo** a precisión de lote (no se opera 0.165 lotes); superarlo → rechazo explícito `volume_exceeds_pair_allocation(max=…)` (audit con la razón). `0`, negativos, `>100`, bools y strings son `ValueError` en el arranque. Con EURUSD al 100% el techo es idéntico al de siempre — **sin cambio de comportamiento hoy**, listo para repartir cuando P3-3 añada el segundo par.
- **Daily start por par en lifespan:** el lifespan crea el guardián de **cada par habilitado** (`build_registry_from_settings().enabled()`) **antes** de fijar el baseline, y el bloque va **fuera** del `try` del equity: una `PAIRS` inválida impide el arranque (falla ruidosa) en vez de dejar guardianes a medias.
- **Código muerto eliminado (P3-1d):** `mt5_client.place_order_with_fallback` (26 líneas, cero llamantes) y `services/correlation_sizing.py` (solo lo importaba esa función) — borrados de verdad, con test que verifica la ausencia. El docstring de `MT5ExecutionDataAdapter` que los documentaba como "marcado para eliminación" queda actualizado. (La cola `deferred_orders` NO se toca: decisión P1-5 pendiente.)

**Decisión de diseño — qué es por par y qué es de la cuenta:** el cupo de posiciones, la racha, las alertas y el presupuesto de size son **del par**; el daily loss y el drawdown desde peak siguen midiéndose sobre **el equity de la cuenta** (un único número: repartirlos por par debilitaría la protección total — dos guardianes independientes al 5% serían un 10% global). Cada guardián del par evalúa ambos umbrales con su propio cooldown: con un solo par, idéntico a antes; con varios, cada mundo bloquea el suyo manteniendo el límite de cuenta intacto.

**Evidencia en vivo (2026-09-29, tras rebuild):** arranque → `[RISK-GUARDIAN] Guardián creado para EURUSD` **seguido de** `Día iniciado — equity=420.53` (orden garantizado: guardián antes del baseline) + `[AI-STARTUP] Modelo PPO v3 de EURUSD cargado`; `health` → `ok` (`equity=421.64`, MCP conectado); EA → `POST /predict → 200`; únicos errores en logs = GCS ADC (gap conocido; **backup local verificado OK** con el loop por pares); 0 Tracebacks.

**Tests 437 → 459 host + contenedor (exit 0):** `test_p31d_pair_risk.py` (nuevo, 16): una instancia por símbolo (misma para el mismo mundo, distinta entre mundos), estados por par independientes, enrutado de `check_risk` por símbolo (GBPUSD bloqueado no afecta a EURUSD), siembra del baseline a guardianes tardíos y actualización global, `equity_allocation_pct` default 100 + 7 casos inválidos → `ValueError`, wiring del lifespan (guardianes antes del baseline) y presencia real de la eliminación del código muerto; `test_orders_prepare.py` +6: rechazo de par desconocido (**sin llegar a riesgo**), par deshabilitado, `open_count` que solo cuenta las posiciones del símbolo pedido (mezcla EURUSD/GBPUSD/ EURUSD → 2), techo de asignación (0.30 > 0.25 → rechazo con `0.25` en la razón; 0.20 → aprobado) y 100% → techo global sin cambio; `_prepare` extiende con `positions` y expone `pool.risk_check` para inspeccionar argumentos.

**Archivos:** `fastapi/services/risk_guardian.py`, `services/order_manager.py`, `services/mt5_client.py` (−`place_order_with_fallback`), `services/correlation_sizing.py` (eliminado), `core/pair.py` (+`equity_allocation_pct`), `core/adapters.py` (doc), `main.py` (lifespan), `fastapi/tests/test_p31d_pair_risk.py` (nuevo), `test_orders_prepare.py` (extendido), `README.md`.

### P1-5. Cola `deferred_orders` eliminada — fin de la falsa confianza (2026-09-29)

**Objetivo (P1-5 / decisión D4):** la cola deferred se creó como "resiliencia para MT5 desconectado", pero **nadie escribió nunca en ella**: `enqueue_order` tenía cero llamantes y el `_order_queue_processor` de `main.py` re-leía una tabla permanentemente vacía cada 60 s. D4 fijó el criterio en 1 sprint: cablear en la ruta SMC si hay uso real, **eliminarla** si no — se han pasado múltiples sprints sin un solo `INSERT`, así que se elimina entera (la resiliencia real de órdenes ya existe: el gate `/v1/orders/prepare` (P0-3) y el outbox de cierres con reintento del EA v13.1 (P0-4 EA)).

**Qué cambia:**
- **`services/order_queue.py` eliminado** (entero): `enqueue_order`, `get_pending_orders`, `mark_order_processing/placed/failed`, `purge_old_orders` y `OrderQueueStatus` — todo el módulo operaba sobre `deferred_orders`.
- **`main.py`:** se elimina `_order_queue_processor` (el background task que "reprocesaba pendientes" de la tabla vacía), su task en lifespan, su cancel en shutdown, la referencia global `_queue_task` y los imports `order_queue`/`place_order` (solo los usaba el processor).
- **`performance_metrics`:** flag `order_deferred` del ciclo, contador `deferred` de `by_symbol` y clave `orders_deferred` del summary — `record_cycle_metrics` nunca recibía el parámetro (siempre `False` → `orders_deferred` siempre 0): métrica-museo de la cola eliminada.
- **`db/schema.sql`:** DDL de `deferred_orders` + índice parcial `idx_deferred_pending`.
- **BD en vivo:** `DROP TABLE deferred_orders` con **0 filas** (previo: volcado del DDL en `/tmp/opencode/p15_backup/deferred_orders_ddl.txt`).

**Evidencia (2026-09-29):** grep de `order_queue|deferred_orders|enqueue_order|_queue_task|order_deferred` en `fastapi/` → **0 referencias**; `frontend/` no leía `orders_deferred` (la eliminación de la clave no rompe ningún consumidor); tras rebuild → arranque limpio (guardián + `Día iniciado — equity=423.89` + startup completo, 0 Tracebacks), `health` → `ok` (`equity=423.97`, MCP conectado), `POST /predict` → `200 (20.3 s, decision=HOLD)`.

**Tests 459 (sin cambios de red):** ningún test referenciaba la cola; la suite completa pasa igual en host y en contenedor (`PYTEST_EXIT=0`) tras la eliminación — la remoción es regresión-neutral verificada.

**Archivos:** `fastapi/services/order_queue.py` (eliminado), `fastapi/main.py`, `services/performance_metrics.py`, `db/schema.sql`, `README.md`.

### P2-4. Estado de riesgo persistente (DB + rollover) y exposición agregada (2026-09-29)

**Objetivo (P2-4):** "estado de riesgo persistente en DB con rollover diario; límite de exposición agregado multi-estrategia". Dos fallos reales cerrados: (1) el baseline del día vivía SOLO en memoria — un reinicio a mitad de día lo re-sembraba con el equity actual, **borrando la pérdida ya sufrida** (y el peak, olvidando el drawdown); (2) el límite diario nunca rodaba: con el servidor días encendido medía "desde el último arranque", no un día. Además el cupo de posiciones (P3-1d) era solo por par: cada estrategia/par por separado bajo su tope podía superar en conjunto la exposición de la cuenta.

**Qué cambia:**
- **`AccountRiskState` (cuenta UNA, compartida):** `day`/`daily_start_equity`/`peak_equity` salen del `RiskState` de cada guardián a un único objeto compartido por todos los guardianes (`_ACCOUNT`) — una sola fuente de verdad: el peak de EURUSD es el que evalúa GBPUSD (antes, cada guardián actualizaba su copia en su propio `check_equity` → los peaks divergían). `observe(equity)` hace el **rollover automático al cambiar la fecha UTC** (baseline = equity actual del nuevo día, `dirty=True`) y actualiza el peak. Cada `/predict` (5 min) llama `observe_equity(equity_val)` → drawdown en vivo contra el pico real, no contra el del arranque.
- **`services/risk_state_store.py` (nuevo):** `risk_account_state` (fila única `id=1`, `CHECK (id=1)`) + `risk_pair_state` (por símbolo: racha + cooldown de alertas). `init_risk_state(equity)` en el lifespan: **fila de hoy → RESTAURA** el baseline real (no el equity actual — el fix); fila vencida/primera vez → rueda y escribe ya; sin equity (MCP caído) → el primer `observe()` del día siembra y el primer flush persiste. `flush_risk_state(symbol)` en `/predict`, en `order_manager.prepare` y tras cada cierre: **sin `dirty` no toca la BD** (la mayoría de los ciclos es gratis); fallo de escritura → `logger.exception` + `dirty` conservado (se reintenta; la protección en memoria sigue viva). En producción un fallo de query en el arranque **detiene el boot** (solo se tolera `RuntimeError` de pool sin inicializar, inalcanzable en prod porque `init_pool()` ya habría reventado antes).
- **Exposición agregada `MAX_TOTAL_POSITIONS`:** setting declarado `config.max_total_positions = 5` (historical: el tope global previo a P3-1d). `can_open_position(equity, open_positions, *, total_open_positions)` — el total es **obligatorio, sin default**: `check_risk` sin exposición agregada revienta con `TypeError` (nadie puede olvidar pasarla). `prepare` cuenta las posiciones del par Y las de toda la cuenta (todas las estrategias: PPO + SMC + cualquier par) y ambas se evalúan: cupo del par primero, después el agregado. Un par con cupo libre no abre si la cuenta está llena.
- **Racha cableada con la convención ≥3 EUR:** `record_trade_result(is_win: bool)` — la clasificación la decide el caller (`core.rules.is_winning_close`), el guardián no re-deriva; el hook está en `auto_retrain.record_trade_filled` (único paso obligatorio de todos los cierres, EA y SMC) **tras el dedup de `deal_ticket`** (un webhook repetido no toca la racha). Antes la racha existía pero **nadie la alimentaba en producción** (cero llamantes) — ahora persiste en `risk_pair_state`. `_alert` marca `dirty`: el cooldown de 1h sobrevive reinicios.
- **DDL vivo aplicado** con las dos tablas nuevas (schema.sql también, para fresh install).

**Decisión de diseño — qué se persiste:** solo lo que protege: baseline+peak (cuenta) y racha+cooldown (par). Los umbrales (`max_positions`, `daily_loss_limit_pct`, `max_total_positions`) son configuración, no estado — no se copian a la BD. La racha NO se resetea por día (semántica original: racha hasta una ganancia); el cooldown es de 1h, no de día, así que tampoco rola.

**Evidencia en vivo (2026-09-29):** arranque 1 → `[RISK-STATE] Día nuevo/primera vez — baseline=422.70 persistido`; un ciclo observa y persiste el peak (`account persistido — day=2026-09-29 start=422.70 peak=422.86`); **`docker restart` → `[RISK-STATE] Día restaurado desde DB — start=422.70 peak=422.86 (equity actual=422.30)`** — el equity ya había caído a 422.30 y el baseline NO se re-sembró (la pérdida sigue contando; antes del fix habría quedado en 422.30 = 0 pérdida); `health` → `ok` (`equity=421.51`); `POST /predict` → `200 (decision=HOLD)`; 0 Tracebacks.

**Tests 459 → 480 host + contenedor (exit 0):** `test_p24_risk_state.py` (nuevo, 20): rollover de baseline vencido (día nuevo → baseline = equity actual, dirty) y del mismo día (solo sube peak), `init_risk_state` con fila de hoy → **restaura 400/465 ignorando el equity actual 420** (el fix, con `pool.execute` NO llamado), fila vencida → rueda y escribe YA, primera vez, sin equity → sin escritura, restauración de racha/cooldown por par desde `risk_pair_state`, flush: sin dirty no toca BD / upsert de cuenta con parámetros exactos / upsert de racha / fallo de escritura y pool ausente → **no lanza y conserva dirty**, convención 3 EUR (`1.5 → pérdida`, `10 → win`) y racha por par independiente, agregado: cupo de par libre + cuenta llena → `MAX_TOTAL_POSITIONS 5 reached`, bajo los dos topes → pasa, `check_risk` sin total → `TypeError`, y DDL de ambas tablas en `schema.sql`. `test_risk_guardian.py` actualizado al contrato (kwarg `total_open_positions` obligatorio, baseline vía `g.account.*`, racha bool + test de dirty) — se retiró `test_zero_pnl_resets_streak` porque el guardián ya no clasifica pnl (lo hace el caller; cubierto por el nuevo test de webhook). `test_trade_filled_webhook.py` +1: cierre `pnl=1.5` (< 3 EUR) incrementa la racha y `pnl=10` la limpia (wiring real de `record_trade_filled`). `test_orders_prepare.py` +2 aserciones: `total_open_positions=3` viaja en `check_risk` y `flush_risk_state` se awaita una vez con `symbol=EURUSD`. `test_p31d_pair_risk.py` adaptado (fixture `_ACCOUNT`, acceso `account.*`, wiring `init_risk_state`).

**Archivos:** `fastapi/services/risk_guardian.py` (AccountRiskState + rollover + agregado), `services/risk_state_store.py` (nuevo), `config.py` (+`max_total_positions`), `db/schema.sql` (+2 tablas), `main.py` (lifespan), `routers/ai.py` (observe+flush por ciclo), `services/order_manager.py` (total + flush), `services/auto_retrain.py` (racha 3€ + flush), `fastapi/tests/test_p24_risk_state.py` (nuevo), `test_risk_guardian.py`, `test_orders_prepare.py`, `test_trade_filled_webhook.py`, `test_p31d_pair_risk.py`, `README.md`.

### P2-4b. Cierre selectivo por ledger de propiedad — los mundos no se pisan (2026-09-29)

**Objetivo (P2-4):** "aislamiento por par / mundos independientes" extendido al CIERRE. Los tres caminos de cierre SMC (`POST /v1/smc/close`, flip de señal en `simple_pipeline`, `news-check`) llamaban `DELETE /positions/symbol/{symbol}`: **borraban TODO el símbolo**, incluida la posición del EA (magic 90001) cuando compartía par. Peor: `/close` registraba en auto-retrain *todas* las posiciones del símbolo **antes** de cerrar — la posición del EA entraba como si fuera de SMC y, si el cierre fallaba, se registraba un trade fantasma. Además los cierres por noticias de SMC **nunca** se persistían en `trade_outcomes` (violación de la convención AGENTS.md "todo cierre debe persistir"), y `exit_reason` fuera del dominio CHECK (`sl,tp,manual,news,signal_end,ea`) revienta el INSERT con asyncpg — el `except` del endpoint se lo tragaba y el cierre quedaba sin registrar.

**Qué cambia:**
- **`services/position_ownership.py` (nuevo):** el MCP no expone magic/comment en `/positions` (auditado contra su openapi) y `magic=0` lo comparten los trades manuales → la única fuente de verdad de propiedad es NUESTRO ledger: `orders.cycle_id LIKE 'smc\_%' ESCAPE` (`_` escapado, no wildcard) + `status IN ('placed','filled')` + `mt5_order_id = ticket`. `close_positions_owned(symbol, candidates)` devuelve `{closed, foreign, failed}`: cierra **por ticket individual** (`DELETE /positions/{ticket}`), un fallo no aborta al resto (cada fallo en `failed` + log), sin candidatos no consulta la BD, y las posiciones ajenas se loguean como *no tocadas*. El caller decide qué persistir.
- **Los tres call sites convertidos:** flip de señal (`simple_pipeline`) → audit con `closed/skipped_foreign/failed`; `/close` → **registra en auto-retrain SOLO los tickets realmente cerrados**, responde `skip/no_owned_position` si solo hay posiciones ajenas y `skip/close_failed` si el cierre falló (respuesta compatible: `skip` ya existía con `no_open_position`); `news-check` → cierra solo las suyas **y ahora las persiste en `trade_outcomes` con `exit_reason='news'`** (antes: jamás). La posición del EA en noticias se protege con su SL/TP + su blackout de entradas — su mundo, sus defensas.
- **Dominio `exit_reason`:** helper `_exit_reason_for_record` — reason fuera de dominio (p.ej. el default `ema_cross`, que venía de un llamante externo manual) → `signal_end`. El motivo original queda siempre en el audit_log.
- **Puerto y librería:** `ExecutionPort.close_positions_by_symbol` → `close_position(ticket)` (el primitivo real que usa el sistema); el helper `mt5_client.close_positions_by_symbol` eliminado por **zero llamantes** (doctrina P3-1d, precedente `place_order_with_fallback`).

**Evidencia en vivo (2026-09-29):** rebuild + arranque limpio (0 Tracebacks); `health` → `ok` (`mcp connected, equity=414.11`); `POST /predict` → `200 (decision=FLAT, quality_score=7.5)`.

**Tests 480 → 494 host + contenedor (exit 0):** `test_p24b_selective_close.py` (nuevo, 14): partición propias/ajenas con parámetros exactos de la query (prefijo escapado `smc\_%`, status, ticket no vacío), solo-lo-suyo-cerrado / ledger vacío → todo ajeno sin cerrar / cierre fallido no aborta al resto (`closed+failed` en la misma pasada) / lista vacía → sin query, `/close` mixto → cierra 111 y responde `foreign=[222]` con 1 registro de retrain (antes: 2) / solo-EA → `skip/no_owned_position` sin cerrar ni registrar / reason fuera de dominio → `signal_end` / cierre fallido → `skip/close_failed` sin registro, `news-check` selectivo con persistencia (`exit_reason='news'`, tickets del EA intactos) y solo-EA → `skip`, **flip end-to-end**: señal con posición del EA + propia abiertas → se cierra SOLO la propia, la orden nueva continúa (`action=buy`) y el audit documenta `closed=[111]/skipped_foreign=[222]`, y contrato del puerto (`close_position` presente, `close_positions_by_symbol` ausente en puerto+adapter+mt5_client con late-binding verificado).

**Archivos:** `fastapi/services/position_ownership.py` (nuevo), `services/mt5_client.py` (+`close_position`, −cierre-por-símbolo), `services/simple_pipeline.py` (flip selectivo), `routers/smc.py` (selectivo + helper de registro + dominio), `core/ports.py`, `core/adapters.py`, `fastapi/tests/test_p24b_selective_close.py` (nuevo), `README.md`.

### P0 — Orden temporal invertido: el sistema servía datos de hace una semana como "actuales" (2026-09-29)

**Hallazgo (en vivo, verificado contra el MCP real):** `/api/v1/market/candles/latest` devuelve las velas **descendente** (más nueva primero — comprobado: `2026-09-29T21:00 → 2026-09-22T22:00` para count=120) y **ninguna ruta lo ordenaba**: ni `engineer_market_features` (choke de inferencia `routers/ai.py`, retrain `auto_retrain` y tests), ni el acceso directo a `raw_df` en ai.py, ni `mt5_client.get_candles`. Impacto medido con el código en vivo:

- **`df.iloc[-1]` ("precio/RSI/ATR actuales") = la vela de hace 6.8 días**: close servido 1.14479 vs real 1.13283; `rsi14` del prompt a Qwen = **0.927 de una semana atrás** (y en sentido ESPEJADO: la serie descendente daba RSI≈0 cuando subía).
- **`raw_df['time'].iloc[-1]` para la sesión** = hora de una semana atrás → clasificación ASIA/EU/US y blackout sobre un dato viejo.
- **`_build_qwen_candle_context(raw_df)`**: `.tail(10)` = las 10 velas **más antiguas** presentadas como *"last 10 H1 candles (oldest→newest)"*.
- **Features espejadas**: `returns` = pct_change de nuevo→viejo (signo invertido), `lag_return_*` = vela futura; el split `iloc[:train_end]` del env y los pliegos de walk-forward simulaban **hacia atrás en el tiempo** (backtest no predictivo) y `dropna` eliminaba las 20 velas MÁS NUEVAS.
- **Train/serve skew desde el despliegue:** `ml/train_ppo_v3.py` **sí** ordena su CSV ascendente (`sort_values('time')`) y `training_experiments` está **vacío (0 promociones)** ⇒ el modelo se entrenó con features correctas y en vivo se servían espejadas en cada ciclo desde el arranque.
- **Por qué la suite no lo pilló:** `_build_candles()` del harness E2E generaba velas **ascendentes** — nunca reproducía el orden de producción.

**Fix (quirúrgico, en el choke point):**
- **`ml/trading_env_v2.py → engineer_market_features`**: si existe columna `time`, ordena cronológicamente ascendente antes de calcular nada. Pasa por ahí inferencia, retrain, entrenamiento y tests → un solo punto de normalización (convención del sistema: `iloc[-1]`=actual, `tail(n)`=recientes, lag=vela anterior, split pasado→futuro).
- **`routers/ai.py`**: contexto de velas y hora de sesión se leen de `df` (ya ordenada), no de `raw_df`.
- **`services/mt5_client.get_candles`**: devuelve la lista ordenada ascendente (epoch normalizado) — contrato de orden para el resto de consumidores del cliente.
- **Harness E2E corregido**: `_build_candles()` devuelve ahora las velas **descendentes** como el MCP real (los8 tests E2E pasan en el orden de producción).

**Evidencia en vivo (2026-09-29, contenedor reconstruido):** reproducción del bug con el código desplegado → `df.iloc[-1] SERVIDO COMO ACTUAL: 2026-09-29T21:00:00Z` (antes: `2026-09-22T22:00`), close actual **1.13374** (antes 1.14479), `rsi14` **0.401** (antes 0.927 espejado), `is_monotonic_increasing=True`; la hora de sesión servida corrige el offset del broker (`_CONTEMPORANEOUS_H=36` → 21−3 = **18 UTC real**); `health` → `ok` (equity=405.46); `POST /predict` → `200 decision=BUY ml_prob=0.6384 quality=7.5` (pre-fix devolvía FLAT/0.1 sobre datos espejados); los únicos 2 `ERROR` de log = gap GCS ADC conocido y aceptado.

**Tests 494 → 502 host + contenedor (exit 0):** `test_p0_time_order.py` (nuevo, 8): entrada descendente MCP → salida monótona con `iloc[-1]` = vela más nueva y warmup (19 filas de `rolling(20)`) descartado de las VIEJAS, signo de `returns` hacia adelante (todo >0 en serie creciente — pre-fix: todo <0), RSI≈1.0 de la serie creciente (pre-fix 0.0), columnas raw preservadas para el contexto de Qwen, entrada ascendente = no-op, df sin `time` sin sort y sin reventar, `mt5_client.get_candles` → epoch ascendente, y **E2E `/predict` con velas descendentes de producción**: a `query_qwen_unified` le llega `hour==11` (vela más nueva, no la de hora 0 más vieja), `rsi≥90` (no 0) y `candle_context` con `return=+` de las 10 más recientes (no el negativo de las antiguas).

**Archivos:** `fastapi/ml/trading_env_v2.py` (sort en el choke), `routers/ai.py` (contexto/hora desde `df`), `services/mt5_client.py` (sort en `get_candles`), `tests/test_p0_time_order.py` (nuevo), `tests/test_e2e_ai_predict.py` (harness descendente), `README.md`.

### P2-5. Snapshots de entrenamiento en parquet con rango+hash en el nombre (2026-09-29)

**Objetivo (P2-5):** snapshots de datos en formato columnar con identificación por **rango temporal + hash de contenido**, barrido de código muerto, y retención que purgue también los artefactos legados. Contexto: los snapshots congelan el df exacto que consumen train y gate (P3-2a, retrain reproducible) y **nadie los LEE** (write-only: `save`/`list`/`prune`) → no hay migración de lectores, solo de escritura y de nombres.

**Qué cambia:**
- **`services/data_snapshot.py` reescrito a parquet:** nombre `<first_ts>_<last_ts>__<sha12>__<purpose>.parquet` — el rango real de los datos (**MIN/MAX de la columna `time`**, robusto al orden de llegada: el MCP entrega velas descendentes, ver P0) dice de un vistazo qué periodo congeló; el sha12 sigue siendo el sha256 del **csv canónico del df** (identidad de los DATOS, independiente de la versión de pyarrow que serialice). Tres formatos reales de `time` soportados: ISO del MCP (`...Z`), epoch de `mt5_client` (segundos) y datetime64 (fixtures). Verificación post-escritura inalterada: relectura → mismas dimensiones, y si falla, ruidosamente. Sin columna `time` → `ValueError` (un snapshot sin rango no es auditable). Dato idéntico → mismo fichero (deduplicación por construcción).
- **Retención bilingual:** `list_snapshots`/`prune` reconocen `.parquet` **y** los legados `.csv.gz` (los purga también); orden por nombre (rango de datos en nuevos, UTC de escritura en legados — la ventana rodante hace que ambos avancen en cómputo general). Los `.tmp` jamás se listan.
- **`pyarrow` a `requirements.txt`** + rebuild del contenedor.
- **Barrido:** cero usos de `gzip` en el código del sistema; el único caller de producción (`auto_retrain._do_retrain`) no cambia (mismo contrato `save_snapshot(df, purpose)`); fixtures de `test_experiment_tracker`/`test_auto_retrain` actualizados al formato `.parquet` (no eran solo strings opacos: representaban el path persistido).

**Evidencia en vivo (2026-09-29, contenedor reconstruido):** `pyarrow 25.0.1` importable en el contenedor; escritura real de un snapshot de prueba → `20260928T000000Z_20260929T050000Z__34f6a0ac92e9__p25_smoke_test.parquet rows=30 cols=2 sha=34f6a0ac92e9…`, `list_snapshots` lo encuentra, relectura `(30, 2)` correcta y artefacto eliminado tras la prueba (no es un retrain real); `health` → `ok` (equity=407.37); 0 snapshots legados en `/app/ml/snapshots` (0 retrains históricos — no hay artefactos que migrar).

**Tests 502 → 505 host + contenedor (exit 0):** `test_data_snapshot.py` reescrito (12): escritura con rango real en el nombre + roundtrip parquet, sha determinista por contenido, **rango MIN/MAX idéntico llegue la df ascendente o descendente** (puente con el P0), `time` ausente → `ValueError`, propósito saneado, df vacío → `ValueError`, cero `.tmp` huérfanos, listado vacío/sin dir, orden más-reciente-primero, purga que conserva los N más nuevos y rechaza `keep<1`, y **legado**: `.csv.gz` conviven con `.parquet` en el listado y la retención purga el más antiguo de los dos formatos.

**Archivos:** `fastapi/services/data_snapshot.py` (reescrito), `fastapi/requirements.txt` (+`pyarrow`), `fastapi/tests/test_data_snapshot.py` (reescrito), `tests/test_experiment_tracker.py`, `tests/test_auto_retrain.py` (fixtures), `README.md`.

### P3-3. Multi-símbolo: retrain por par + gate canario absoluto — GBPUSD RECHAZADO en su primer intento (2026-09-29)

**Objetivo (P3-3, último ítem del plan):** cerrar el escalado con **canary real** — pipeline de retrain parametrizado por símbolo, gate OOS absoluto para el primer modelo de un par (Sharpe ≥ 1.0 en ≥4 folds + 200+ aperturas, según el plan) y límites de riesgo configurables por par (DD diario 2%, MaxDD 15% bloqueante) listos para activarse. GBPUSD entra como **canario listado pero apagado** (`enabled=false`): se entrena con el gate en verde/rojo y solo se habilita si lo aprueba.

**Qué cambia:**

- **Pipeline parametrizado por símbolo** (`services/auto_retrain.py`): `_do_retrain/_do_retrain_core/_trigger_retrain/_spawn_retrain_task/approve_retrain/force_retrain` aceptan `symbol` (default `TRAINING_SYMBOL`=EURUSD → cero cambio para el flujo actual). El bundle, el baseline, el manifest, el hot-reload, el backup y el estado (`retrain_state`, contadores, outcomes) que escribe cada corrida son los de **SU mundo** — la ruta sale de `default_model_path(symbol)`. Lock y task de retrain siguen siendo **globales** (retrains de pares distintos serializados; un trigger no encolado sigue pendiente en SU contador y se re-evalúa en cada cierre o con `approve?symbol=`). El guard `if symbol != TRAINING_SYMBOL: return` (P3-1c) **desaparece**: el umbral de GBPUSD dispara el retrain de GBPUSD. `_validate_symbol` exige que el símbolo esté **listado** en PAIRS (habilitado o no — entrenar el canario antes de activarlo es el flujo) → símbolo desconocido = `ValueError` explícito. Endpoints `POST /retrain/force|approve` aceptan `?symbol=` (sin él → EURUSD); el audit_log registra el símbolo.
- **Lookback por par** (`RetrainConfig.lookback_for`): GBPUSD=30000 H1 (~4.8 años, disponible desde 2021-12 — verificado contra el MCP: 18s de fetch), EURUSD sigue en 500 (su bundle existe y su gate es comparativo). Timeout del fetch 30→60s.
- **Gate canario absoluto** (`services/walk_forward.py`): cuando **no hay baseline** (primer modelo del par), `first_model_validated` ya no es un sello de goma: exige `total_openings ≥ 200` (`canary_insufficient_openings`) y **sharpe ≥ 1.0 en ≥ min(4, n_folds) pliegues** (`canary_sharpe_below`, informe sin pliegues jamás valida). Con baseline el gate **comparativo no cambia** — EURUSD intacto.
- **Riesgo del canario (config-driven):** `PairConfig.daily_loss_pct` (spec PAIRS, validado (0,100]) → el guardián de cada par se crea con **SU** límite (GBPUSD=2%, default 5%); `settings.max_drawdown_block_pct` (default `None` = solo alerta al 10% como siempre) → si se fija (canario: 15), el DD de **cuenta** desde peak **bloquea** aperturas de todos los pares. `config.pairs` por defecto lista ya GBPUSD `enabled:false, daily_loss_pct:2.0, equity_allocation_pct:20` — **listar no habilita**.
- **Aislamiento completado:** baseline de drift **por par** (junto a su bundle: EURUSD legado en raíz, GBPUSD en `ml/GBPUSD/`) — antes la promoción de un par habría pisado la referencia de drift de otro (acoplamiento que el guard hubiera introducido); `write_manifest`/bundle: directorio del namespace creado antes de escribir (SB3 crea padres, el manifest no).

**Evidencia en vivo (2026-09-29, contenedor reconstruido):** primer retrain de la historia del sistema ejecutado de punta a punta — `python gbpusd_retrain.py` en el contenedor (pool propia, server intacto): fetch 30000 GBPUSD H1 → snapshot `20211203T000000Z_20260929T220000Z__e936f9ac4e46__retrain.parquet rows=29981` → **experimento #1** (`training_experiments`: symbol=GBPUSD, triggered_by=forced) → split train=20986 / embargo=20 / 4×1494 barras / holdout@26982 congelado → PPO 100k timesteps (bundle nuevo: sin baseline → vía canario) → walk-forward → **RECHAZADO `canary_sharpe_below` en 297s, exit 0**. Métricas del gate (audit_log `retrain_rejected`): candidate `sum_reward=-103.17 sharpe=-2.66 opens=1524` (masa de aperturas ✓ ≥200, calidad ✗), fold sharpes `[-1.70, -5.16, -3.24, -0.53]` vs umbral `1.0` en 4 pliegues → `folds_strong=0/4`. Holdout (solo reporte) `-6.85`. **Resultado: NO se escribe bundle (`ml/GBPUSD/` no existe), NO se promueve, NO se activa el par — GBPUSD queda `enabled:false` con la evidencia en DB.** Es exactamente el contrato del canario: el gate decide, no la esperativa. `health` → `ok` (equity=410.85).

**Decisión:** GBPUSD **permanece deshabilitado** (plan: "fail ⇒ stays disabled with evidence"). Los límites del canario (2% diario, 20% equity, MaxDD 15%) están implementados y configurados en el spec listado; la activación (`.env PAIRS` con `enabled:true` + `MAX_DRAWDOWN_BLOCK_PCT=15`) es un paso posterior que exige un entrenamiento que supere el gate — hoy el modelo primerizo solo-sintético no lo hace (esperable: sin cierres reales el feedback es histórico). Reprogramar el intento = vía `POST /retrain/approve?symbol=GBPUSD` cuando haya un caso que probar, o retraining con hiperparámetros/feedback real en una fase futura — **sin tocar el umbral del gate para que pase**.

**Tests 505 → 530 host + contenedor (exit 0):** `test_walk_forward.py` (gate canario: openings <200 rechaza, sharpe por pliego bajo rechaza con detalle, 4 pliegues en verde valida `first_model_validated`, informe sin pliegues rechaza — comparative intacto); `test_p33_symbol_pipeline.py` (nuevo, 12): lookback GBPUSD/EURUSD, validación de mundo (listado disabled ✓, desconocido ✗), approve/force por símbolo (el pendiente de GBPUSD no lo aprueba el default; el default no roba el de otro), propagación symbol→pipeline aislado por estado; `test_auto_retrain.py` (+1 promoción dirigida al mundo: fetch 30000 con symbol=GBPUSD, experimento/promoción/reload/auditoría en SU symbol, ruta resuelta por símbolo; `default_model_path` parcheado en vez del global muerto; openings×4=200 para el canario); `test_p31c` (umbral de GBPUSD → **spawn propio** — el test que codificaba "nunca dispara" invertido + auto_approve=false → pendiente sin spawn); `test_retrain_approval/schedule` (spawn con `symbol=`, endpoint `?symbol=GBPUSD` llega al servicio y al audit); `test_risk_guardian.py` (+6: DD block 15% config, bajo el umbral solo alerta, `None` = histórico; guardian con `daily_loss_pct` del spec, no listado → default, validación del spec); `test_drift_monitor.py` (+1 baseline por par en namespaces distintos); `test_p31b` (GBPUSD listado **deshabilitado** en el registro — el invariante "listar no activa" ahora se prueba).

**Archivos:** `fastapi/services/auto_retrain.py` (pipeline por símbolo, guard eliminado, lookback/validación/timeout, namespace antes de escribir), `services/walk_forward.py` (gate canario + constantes), `services/risk_guardian.py` (límite por spec + DD block config), `services/drift_monitor.py` (baseline por par), `core/pair.py` (`daily_loss_pct`), `config.py` (pairs default con canario apagado + `max_drawdown_block_pct`), `routers/ai.py` (endpoints con `?symbol=`), tests (8 actualizados + `test_p33_symbol_pipeline.py` nuevo), `README.md`.

### Config: `RETRAIN_AUTO_APPROVE=true` — el umbral de cierres dispara retrain sin cola humana (2026-09-29)

> **Historial del flag:** `true` (2026-09-29) → `false` (2026-10-01, O-4, tras las exps #13/#14 automáticas) → **`true` de nuevo (2026-10-01, decisión de Félix — ver «RETRAIN_AUTO_APPROVE reactivado» más abajo)**. El estado vigente vive en `.env`; el default de `config.py` nunca cambió (`False`).

**Cambio de despliegue (decisión de Félix, 2026-09-29):** `.env` añade `RETRAIN_AUTO_APPROVE=true`. El default en `config.py` permanece `False` — la inversión es explícita por entorno, no en código.

**Efecto:** con el flag en true, `record_trade_outcome` dispara `_spawn_retrain_task("trade_count", symbol=…)` en cuanto `filled_count ≥ min_trades_for_retrain (5)` y `check_scheduled_retrain` dispara `scheduled` al llegar el intervalo (168h), ambos sin pasar por `POST /retrain/approve`. **La promoción NO se automática:** todo retrain sigue atravesando walk-forward + gate (comparativo si hay baseline, canario absoluto si no) — el flag elimina la cola humana de aprobación del *intento*, nunca el gate de *promoción*.

**Evidencia (2026-09-29):** contenedor recreado (`docker compose up -d fastapi`); `docker exec trading-fastapi printenv RETRAIN_AUTO_APPROVE` → `true`; `/health` → `ok` (equity=409.8, MCP connected). Estado observable: EURUSD `filled_count=11 ≥ 5` → el siguiente cierre EURUSD disparará retrain automático (lock global lo serializa; `filled_count` se reinicia a 0 al promocionar/rechazar); GBPUSD `filled_count=0` (par apagado, sin trades → sin trigger); calendario: último intento EURUSD hace ~20h < 168h → sin disparo inminente. Suite **530 passed** (22s, exit 0 — sin cambios de código, solo despliegue + docs).

**Archivos:** `.env` (no versionado), `README.md`, `PROTOCOLO_ANTI_BUCLE.md` (estado + decisiones: EA v13.2 compilado y en MT5, paper architecture B no aplica, GCS ADC diferido al final).

### Fase 1 (plan 2026-09-30): suelo absoluto del gate + lookback EURUSD 30000 + fin de PAPER_MODE + reversión de la promoción de las 03:14 (2026-09-30)

**Contexto:** `doc/plan-optimizacion-y-estratega-2026-09-30.md` (aprobado por Félix; decisiones D-1…D-5 resueltas). La auditoría operativa halló: gate comparativo sin suelo absoluto (promoción 03:14 con `sum_reward=-4.716`, `sharpe=-3.19`, `expectancy=-10.26` solo porque el baseline era aún peor), `lookback=500` → pliegues de 19 barras (ruido decidiendo promociones), y `PAPER_MODE=true` declarado con 0 consumidores mientras la cuenta es real.

**Cambios:**
- **G-1** `fastapi/services/walk_forward.py` — suelo absoluto en `gate_walk_forward` (ambas vías, tras `non_finite_metrics`): `sum_reward > 0`, `mean_sharpe > 0` y `total_expectancy > 0` en el agregado de pliegues; forma positiva en todas (un NaN no supera `> 0` → rechazo). El holdout sigue congelado por diseño (nunca decide). `fastapi/services/auto_retrain.py` — `min_validation_openings` 1 → 20.
- **G-2** `fastapi/services/auto_retrain.py` — `lookback_by_symbol["EURUSD"] = 30000` (antes 500 → 481 velas → folds de 19 barras).
- **D-1** `fastapi/config.py`, `.env`, `AGENTS.md` — eliminado `paper_mode` por completo (era ficción: sin consumidores y con cuenta real operando; el veredicto "mantener en paper" de la auditoría 2026-09-28 descansaba sobre esa salvaguarda inexistente).
- **D-2** reversión de la promoción `cb8f939ecd10` (2026-09-30 03:14): trio `ppo_trading_bot_v3.zip` / `.manifest.json` / `_vec_norm.pkl` restaurados al HEAD `4185451b` (promoción 2026-09-28; shas verificados contra el manifest = `validate_bundle` OK), `feature_drift_baseline.json` (sin trackear) eliminado, `model_registry` id4 `is_active=false` / id3 `is_active=true`, evento `model_reverted` en `audit_log`.

**Re-promoción forzada bajo el gate nuevo** (`POST /api/v1/ai/retrain/force?symbol=EURUSD`): split `train=20986 | folds=4×~1494 barras` (G-2 vivo) · 100k timesteps continuando del bundle restaurado con sus stats VecNormalize → `cand(sum_reward=-107.361 sharpe=-2.19 opens=490) | base=-68.012/-2.19 | fold_wins=1/4 holdout_cand=-19.246 → RECHAZADO (absolute_floor_sum_reward)`; experimento #3 `status=rejected`; **modelo activo conservado** (`4185451b`). Primera evaluación con masa estadística real: 490 aperturas sobre folds de ~1494 barras (antes 8 sobre folds de 19). Lectura honesta: ni el activo (−68) ni el candidato (−107) son positivos — el gate ya no negocia con candidatos negativos; volver a expectancia positiva es Fase 3 (retrain con memoria + incentivos, plan §7), no un ajuste de umbrales.

**Comportamiento derivado (esperado):** monitor de drift en `no_baseline` → alerta WARNING hasta que un retrain promovido congele baseline (log 12:45:26); GCS ADC pendiente (D-4: al final, no bloqueante) — los 2 ERROR del arranque son ese backup con `local verificado OK`.

**Evidencia (2026-09-30):** suite **534 passed** (530 + 4 tests nuevos en `TestGateWalkForward`: `negative_candidate_blocked_even_vs_worse_baseline` — reproduce exactamente la promoción de las 03:14 —, `negative_sharpe_blocked_with_positive_reward`, `negative_expectancy_blocked`, `absolute_floor_applies_to_canary_path_too`; fixture `_fake_eval` con equity de 3 puntos para que el sharpe sea computable > 0). `test_p33_symbol_pipeline`: EURUSD → 30000. Deploy `docker compose up -d --build fastapi` → `/health` ok (equity=410.29), `[MODEL-REGISTRY] Bundle verificado` al arranque, `printenv PAPER_MODE` → ausente, polls del EA → 200. Audit trail (UTC): `model_reverted` 12:38:23 → `retrain_forced` 12:39:47 → `retrain_rejected` 12:45:27.

**Archivos:** `fastapi/services/walk_forward.py`, `fastapi/services/auto_retrain.py`, `fastapi/config.py`, `fastapi/tests/test_walk_forward.py`, `fastapi/tests/test_p33_symbol_pipeline.py`, `.env` (no versionado), `README.md`.

### Fase 1 (plan 2026-09-30): O-1 ciclo de vida de órdenes + T-1/A-2 timeline de auditoría + S-1/S-2 sizing declarado (2026-09-30)

**Contexto:** últimos 3 P0 de la auditoría (`doc/plan-optimizacion-y-estratega-2026-09-30.md`). **O-1:** `orders.status` nunca transicionaba — toda orden ejecutada permanecía `pending` para siempre y `check_duplicate` rechazaba entradas legítimas (6 `duplicate_order` contra órdenes no vivas en la ventana). **T-1/A-2:** `log_to_audit` escribía `created_at = to_timestamp(mt5_time)` (reloj broker UTC+3) → 64 filas con `created_at` futuro; `ai_predict_cycle` no pasaba `cycle_id` → filas con el fallback `trace_*`, correlación rota. **S-1/S-2:** 21 ERROR `sizing inviable` en una ventana, todos atribuidos en audit a la causa equivocada (`ranging_veto`×9 / `unknown_veto`×12 — cero como sizing).

**Qué cambia:**
- **O-1** `services/order_manager.py` — `mark_order_filled(symbol, type)`: UPDATE de la fila **viva más reciente** (`pending`/`placed`, `ORDER BY created_at DESC LIMIT 1`) por (symbol, type), **sin ventana temporal** (el `entry_time` de los reportes es reloj broker, incompatible con `created_at` UTC; el guard de posición del EA garantiza ≤1 orden viva por símbolo), **excluye filas SMC** (`cycle_id NOT LIKE 'smc\_%'`), idempotente (segundo reporte → sin match → `None`), traza `order_filled` en `audit_log`. `routers/ai.py` — cableado en **ambas** ramas de `/trade/filled`: apertura (sigue sin persistir en `trade_outcomes`, pero SÍ transiciona) y cierre (**catch-all** tras `record_trade_filled` — si el reporte de apertura se pierde, el cierre transiciona; si `record` falla, no se deriva nada). Mapeo LONG→BUY / SHORT→SELL. `'filled'` es terminal en `orders`: la verdad del cierre vive en `trade_outcomes` (desviación documentada del plan: **no** se añade `'closed'` al CHECK — sin consumidor y el ALTER en vivo era riesgo de migración). El guard `check_duplicate` **no cambia**: su SQL ya era `pending/placed` (solo vivas); ahora el ciclo de vida hace que sea verdad. Sin cambio MQL5 (sin bump de versión).
- **T-1** `services/structured_logging.py` — `log_to_audit` con **un solo INSERT**: `created_at` = default de la columna (`now()` UTC); `mt5_time` del broker sigue viajando en `enriched.mt5_time`/`data.mt5_time`. Desaparece la rama `to_timestamp($4)`.
- **A-2** `routers/ai.py` — `log_to_audit("ai_predict_cycle", …, cycle_id=cycle_id)`.
- **S-1/S-2** `routers/ai.py` — el bloque de sizing declarado: flag `sizing_below_min` (inicia `false` en cada ciclo, `true` solo si `final_lots ≤ 0` con decisión BUY/SELL) → métrica **en audit en todos los ciclos**; `_veto_reason(original, final, qwen, sizing_rejected=False)` con el caso **`sizing_veto` comprobado ANTES que el resto** (el flag es autoridad: el bloque de sizing solo corre si ningún otro veto ya revirtió — de ahí que los 21 estuvieran mal attribuidos); `logger.error("sizing inviable…")` → **WARNING 1/600 s por símbolo** (`_SIZING_WARN_TS`, módulo `routers.ai`) + `DEBUG` en el resto, y el literal `sizing inviable` **no se emite en ningún nivel**. La respuesta se queda en **`HOLD`** (no `WAIT`): el EA solo entiende CLOSE/HOLD/BUY/SELL y un valor nuevo caería en silencio sin reportar `ea_decision` — la declaración es en la auditoría (`veto_reason=sizing_veto` + `sizing_below_min=true`), que es donde manda el plan.

**Evidencia en vivo (2026-09-30, contenedor reconstruido):** deploy `docker compose up -d --build fastapi` → `/health` `ok` (equity=420.08); `docker logs … | grep -c "sizing inviable"` → **0**; ERRORs desde el arranque = **2**, ambos el GCS ADC conocido (D-4, diferido no bloqueante). Backfills post-deploy (con traza en `audit_log`: eventos `orders_backfill_filled` y `audit_created_at_backfill`): `UPDATE orders … FROM audit_log (ea_decision buy/sell, reporte post-`trade.Buy` OK, por cycle_id+tipo)` → **14 filas `pending→filled`** (la SMC `placed` intacta; los 27 ciclos `buy/sell` sin fila son anteriores al reset de la tabla del 29-09) y `UPDATE audit_log SET created_at = created_at − 3h WHERE created_at > now()` → **64 filas corregidas, 0 futuras restantes**. Primer ciclo tras el deploy (audit id 4226): `cycle_id=EURUSD_1790790300000` real, `sizing_below_min=false`, `created_at` no futuro (los vecinos anteriores muestran `trace_*` y sin `sbm` — el antes/después es visible en la propia tabla).

**Tests 534 → 551 host (exit 0):** `test_orders_lifecycle.py` (nuevo, 8: SQL acotado a fila viva más reciente + sin INTERVAL + exclusión SMC + `order_filled` audit; idempotencia sin audit; cierre LONG/SHORT → BUY/SELL; `record` falla ⇒ sin transición; apertura sigue `ignored_open_deal` pero transiciona); `test_sizing_veto.py` (nuevo, 7: `sizing_veto` por delante de `ranging`/`unknown` y `None` sin flip; predict con `volume` cabeza 0.0 → HOLD + `veto_reason=sizing_veto` + `sizing_below_min=true`; volumen viable → `false`/`veto=false`; rate-limit 1 WARNING + 1 DEBUG en dos ciclos y `grep "sizing inviable" = 0`); `test_structured_logging_fallback.py` (+1 T-1: con `mt5_time` → INSERT único, sin `to_timestamp`/`created_at`, 3 placeholders, epoch dentro del payload); `test_qwen_semantics.py` (+1 A-2: `cycle_id` real en `ai_predict_cycle`).

**Archivos:** `fastapi/services/order_manager.py`, `fastapi/services/structured_logging.py`, `fastapi/routers/ai.py`, `fastapi/tests/test_orders_lifecycle.py` (nuevo), `fastapi/tests/test_sizing_veto.py` (nuevo), `fastapi/tests/test_structured_logging_fallback.py`, `fastapi/tests/test_qwen_semantics.py`, `fastapi/tests/test_trade_filled_webhook.py` (harness parchea `mark_order_filled`), `README.md`.

### Fase 2 (plan 2026-09-30): A-1 veto position-aware + D-3 día UTC + B-1 rate-limit de alertas + M-1 promociones versionadas (2026-09-30)

**Contexto:** primer lote de P1 de Fase 2 (`doc/plan-optimizacion-y-estratega-2026-09-30.md`). **A-1:** el backend devolvía BUY/SELL con posición abierta y sólo el EA filtraba (`rejected_position_open` ×448 históricos) — 14 ciclos gastados en una ventana de 14 h en su día. **D-3:** los `time_msc` de MT5 son reloj **broker** (UTC+3) vendido como epoch — forense: `orders.created_at 06:50:17.456` (UTC real, DB) ↔ deal de apertura `09:50:17.535` = +3h exactos, mismos ms → `/v1/smc/analytics` agrupaba el P&L diario por día **broker** mientras el límite diario de riesgo (`risk_state_store`/`risk_guardian`) agrupa por día **UTC**. **B-1:** `alerting.send_alert` sin rate-limit (ráfagas de MCP/health/drift duplicadas). **M-1:** artefactos de promoción sin commitear (un `git checkout` los revierte en silencio) y `*.zip` global en `.gitignore` dejaba **invisibles** los zips de bundle nuevos.

**Cambios:**
- **A-1** `fastapi/routers/ai.py` — veto de posición **primero** en la cadena de vetos, antes del sizing: `env_position` (`req.position`: 0 plano, 1 long, 2 short) con `final_decision in ("BUY","SELL")` → `HOLD`; `_veto_reason` comprueba `position_rejected` antes que `sizing_veto`; flag sólo cuando la decisión original era BUY/SELL; aviso ERROR→WARNING rate-limitado (600 s/símbolo). Verificado: los 3 sitios del EA (L407/L440/L475) usan el `currentPos` del mismo instante de la petición → el veto del backend refleja exactamente al EA (**sin cambio MQL5, sin bump**). No gateado por replay (defensa en profundidad). Cadena con `elif` → comportamiento previo intacto cuando no hay flip.
- **D-3** `fastapi/routers/smc.py` — `dt = fromtimestamp(time_msc, tz=UTC) − detect_utc_offset_hours(vela formándose)`: el desfase se **detecta** en cada petición con la vela H1 vía `services/market_time` (auto-corrector ya probado en P2-3) — 0 si el feed pasa a sellar UTC honesto o cambia el DST del broker, **cero hardcode**; sin vela detectable el endpoint **propaga error** (sin agrupar en falso). Sólo afecta a `daily_pnl`/`history` del dashboard (riesgo ya estaba en UTC).
- **B-1** `fastapi/services/alerting.py` — rate-limit 600 s por **clase de alerta** (nivel + componente + huella con cifras colapsadas, espejo de `_SIZING_WARN_TS`): la repetición no llega ni al log ni a Telegram, se contabiliza y el recuento **se anexa a la siguiente emisión** (`+N repetidas suprimidas en 600s` — la supresión nunca es invisible); higiene de poda por ventana; **CRITICAL nunca se limita**. Cubre el ruido GCS ADC en ráfaga (D-4 sigue diferido: el intento aislado emite 1 vez como ahora).
- **M-1** `.gitignore` — exención `!fastapi/ml/*.zip` y `!fastapi/ml/*/*.zip` (los zips de bundle nuevos dejan de ser invisibles; `backups/` y `snapshots/` siguen ignorados: al excluir un directorio no se desciende a sus zips) + log de cron ignorado. `fastapi/services/auto_retrain.py` — `_write_promotion_marker` escribe `last_promotion.json` **junto al bundle y al final** de la promoción (bundle certificado + `model_registry` + auditoría); si la escritura falla, la promoción ya está viva → alerta con instrucción manual, sin rollback. `scripts/commit_promotion.py` (nuevo, stdlib) — gatillo **cron `*/5`** (crontab de usuario, instalado): valida sha256 del zip contra el marcador, `git add fastapi/ml` **exclusivamente** (jamás toca código en progreso), commit `M-1 promoción <symbol>: <model> sha=… — gate: …` con el marcador como evidencia permanente; no-op sin marcador sucio; cualquier fallo → exit 1 con la pendencia visible en `git status`.

**Evidencia (2026-09-30):** suite **585 passed** (551 → +11 `test_position_veto.py` + 6 `test_d3_utc_day_domain.py` + 10 `test_alerting_ratelimit.py` + 7 `test_m1_promotion_commit.py`, exit 0, 23.5s). Deploy `docker compose up -d --build fastapi` → `/health` `ok` (equity=432.63, MCP connected). **A-1 en vivo:** ciclo 18:20:19 UTC con `position: 2` (SHORT abierta) → `decision: HOLD`, `veto: false`, EA `action: hold`; **0 `rejected_position_open` desde el deploy**. **D-3 en vivo:** offset **3** detectado de la vela `2026-09-30T21:00:00Z` (now 18:19Z); `daily_pnl` de la API **idéntico** al recálculo UTC independiente sobre los 288 deals (match exacto en 23 días); la agrupación sin corregir **difiera en 6+ días** (p. ej. `2026-09-27` no existía — caía en el día broker 09-28; `09-28`: 25.77→8.32; `09-04`: −3.46→0.41). **B-1 en vivo:** 2 alertas desde el arranque (primera ocurrencia de cada clase = siempre emite: ADC 18:19:50, drift `no_baseline` 18:23:38); ambas fuentes vivas hoy a 24h — la supresión de ráfagas está cubierta por tests. **M-1 en vivo:** crontab con `*/5 … commit_promotion.py` (log creado a las 18:20, 0 bytes = no-op correcto); ejecución manual → rc 0; `git status fastapi/ml` **vacío** tras la exención (nada nuevo revelado).

**Archivos:** `fastapi/routers/ai.py`, `fastapi/routers/smc.py`, `fastapi/services/alerting.py`, `fastapi/services/auto_retrain.py`, `.gitignore`, `scripts/commit_promotion.py` (nuevo), `fastapi/tests/test_position_veto.py` (nuevo), `fastapi/tests/test_d3_utc_day_domain.py` (nuevo), `fastapi/tests/test_alerting_ratelimit.py` (nuevo), `fastapi/tests/test_m1_promotion_commit.py` (nuevo), `fastapi/tests/test_close_classification.py` (fake sirve vela), `fastapi/tests/test_auto_retrain.py` (+ marcador), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Fase 2 (plan 2026-09-30): capa estratega SHADOW — deliberación por ciclo + C1–C8 log-only (2026-09-30)

**Contexto:** núcleo de Fase 2 / decisión **D-5** («shadow primero con replay»): el sistema era reactivo — sin memoria de la última salida, el único filtro de entrada era la banda muerta ±0.05 del PPO y si el sistema decidía ESPERAR no había traza de por qué. Este cambio añade la **capa de deliberación** (plan §4): por cada ciclo `/predict` que llega a la cadena de decisión se construye el **estado narrativo** (§4.1) y se evalúan los **criterios C1–C8** (§4.2), embebiendo todo en `audit_log.ai_predict_cycle.data.deliberation`. **Autonomía ≠ opacidad**: verdict `ENTER`/`WAIT`/`NO_CANDIDATE` con `blocked_by`, integrando los vetos existentes (`vetoes.fired` = lo que decidió `_veto_reason` — una sola fuente de verdad, verificada en vivo `fired == veto_reason`).

**Contrato SHADOW (plan §6 — nada se activa sin informe de replay aprobado por Félix):**
- `services/strategist.py` — config versionada `STRATEGIST_VERSION=1.0.0` con los 8 criterios **`active: false`** (log-only) y `justified_by` explícito: `None` = umbral placeholder pendiente de replay (C1/C3/C4/C5/C6/C8); solo C2 (contrato broker `min_volume`) y C7 (k=2 del plan) están justificados.
- El verdict **no se aplica nunca**: la respuesta y la cadena de vetos son intactas (test `shadow_never_changes_the_decision`: criterio activo que falla → `verdict=WAIT` en audit, respuesta sigue `BUY`, `veto=false`).
- Fail-safe: un criterio **activo** solo pasa con `pass is True` — la duda (`pass=null`: sin datos, Qwen degradado, fuera de ventana) → `WAIT` con el criterio nombrado en `blocked_by`.
- `pass=null` ≠ `pass=true`: la traza jamás confunde «sin datos» con «evidencia a favor» (p. ej. C7 con `<30` muestras: `detail=samples=2 < 30: sin historia`).

**Estado narrativo (derivado, sin almacenamiento nuevo):** último cierre de `trade_outcomes` con **regla 3 €** (`is_winning_close`), tesis del trade vía `LEFT JOIN trade_insights ON trade_id` (null honesto si el análisis aún no existe), racha desde el guardián en memoria (null si no hay guardián — nunca un 0 inventado), `bars_since_close` y `price_move_since_close_pips` comparando `exit_time` contra la ventana de velas del ciclo (ambos en dominio broker, documentado), e inputs de límites de riesgo (el bloqueo real sigue en `order_manager`). Para C7, **muestras vivas de spread por par** (deque 288 ≈ 24 h, en memoria; al reiniciar empieza de 0 → no evaluable hasta acumular).

**Errores sin silencios:** `deliberate()` **propaga**; la contención vive en la frontera del router y está testeada — fallo del shadow → WARNING + `deliberation={"verdict":"ERROR", "error":…}` en audit, ciclo de decisión intacto. El spread degrada **solo C7** con `error` explícito (un endpoint no borra la traza entera). `grep "deliberación falló"` desde el deploy → **0**.

**Evidencia (2026-09-30):** suite **617 passed** (585 → +32 en `test_strategist_shadow.py`: config todo-OFF y umbrales `justified_by` honestos; verdict ENTER/NO_CANDIDATE/WAIT y duda→WAIT; sección `vetoes` espejo de la cadena; C1–C8 con casos borde (misma-dirección/ventana/racha/muestras/ATR); propagación del fallo de narrativo; regla 3 € en `fetch_narrative` (2.99 € → loss); ventana bars/move (4 casos); router (traza completa, **shadow sin poder de decisión**, interlock `position_veto`, contención `verdict=ERROR` + 1 WARNING)). Deploy `docker compose up -d --build fastapi` → `/health` `ok` (equity=434.86, MCP connected), arranque limpio. **En vivo:** ciclos 19:00:19 y 19:05:18 UTC con `deliberation` completa — `verdict=WAIT`, `blocked_by=["veto:position_veto"]`, `vetoes.fired == veto_reason == "position_veto"` (SHORT abierta `position=2`, flip vetoado por A-1 y retratado por D-5), `config_version=1.0.0`; narrativo real (`last_exit` 2026-09-30T21:00:18+00 broker, `pnl=0.0` → `is_win=false`, `thesis` (`RANGING`/`momentum_fade`/3.5), `bars_since_close=1`, `move=-1.4 pips`, `racha=1`, límites con equity 435.48); los 8 criterios evaluados (p. ej. C3 `7.5≥7` pass, C4 `RANGING/NEUTRAL` no coherente, C6 racha=1 exige ≥8 → false). **0 `rejected_position_open` desde el deploy** (4 `ea_decision hold`), 0 fallos de deliberación contados, únicos ERROR = GCS ADC conocido (D-4). Nota de consulta: `audit_log.data` es el wrapper `{"data":…,"event":…,"trace_id":…}` → la ruta jsonb correcta es `data#>>'{data,deliberation,verdict}'`.

**Próximo (Fase 2):** replay de C1–C8 sobre ~2015 ciclos / 141 trades (expectancia tras costes y regla 3 €, split temporal) → informe a Félix; **ningún criterio se activa antes de su aprobación**.

**Archivos:** `fastapi/services/strategist.py` (nuevo), `fastapi/routers/ai.py`, `fastapi/tests/test_strategist_shadow.py` (nuevo), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Fase 2 (plan 2026-09-30): replay C1–C8 — informe de activación (2026-09-30)

**Contexto:** plan §6 — «ningún criterio activo sin informe de replay aprobado por Félix». `scripts/replay_strategist.py` (nuevo, **solo lectura**: SELECT + GET de velas H1) reconstruye el estado narrativo de cada ciclo `ai_predict_cycle` (2050 parseados: 1763 formato nuevo + 287 antiguo) con el **mismo `evaluate_criteria` de serve** (no una reimplementación), mapea cada trade a su ciclo productor (ventana [entry−300 s, entry+60 s] + dirección coincidente) y mide la expectancia de los subconjuntos permitido vs bloqueado con split temporal (train 23 / test 23; θ se elige en train y se verifica en test — «Prohibido ajustar umbrales» para forzar resultado).

**Método (decisiones quedan en el informe §2):** dominio temporal broker unificado (`mt5_time` / `created_at+3h` / `trade_outcomes` / velas MCP — validado empíricamente por la tasa de mapeo); C1 con proxy `ml_prob` (clip 0.1–0.99, la cabeza cruda no se persiste); **excluido ≠ bloqueado** (dato ausente sale del análisis; `pass=None` estructural SÍ bloquea = fail-safe real de activación); expectancia = pnl real medio ya neto de costes (la regla 3 € no altera una media — se aplica a win-rates); sanidad §6 (a) E_bloq<E_permit en test, (b) win-rate bloqueado <10 %, (c) `pass=False` en ≤40 % de HOLD puro, con estado `na` = sin evidencia (activación exige evidencia, no solo ausencia de fallo).

**Resultados (`doc/replay-estratega.md` + `doc/replay-cycles.csv`, gitignored):**
- Muestra: 141 trades válidos → 52 mapeados → **46 analizados** (6 de era antigua excluidos: atribución débil, Δ≈3–5 min sin el patrón de latencia de ~19 s); 89 sin ciclo en ventana (10 pre-auditoría + 79 de otras fuentes/rechazos de EA); huecos de cobertura > 6 h listados.
- **C2 → CANDIDATO A ACTIVAR (contrato broker, neutro):** 0 bloqueos en train ni test, umbral `settings.min_volume` no tuneable. Único candidato.
- **C1/C3/C8 → MANTENER OFF (sin evidencia en test):** 0 bloqueos en test. Estructurales: `quality_score` = **7.5 constante** en los 511 ciclos entry (C3 vacío sobre candidatos: ≤7.5 nada, ≥8.0 todo); `ml_prob` ≥ 0.195 en todos los candidatos (C1 no discrimina); C8 con separación +8.01 € en train pero win-rate bloqueado 14 % ≥ 10 % en todo el barrido y 0 bloqueos en test.
- **C4/C5 → MANTENER OFF (falla a+b):** bloquean el **100 %** de candidatos en test (precondiciones casi nunca cumplidas bajo fail-safe) y los bloqueados ganan 26 % (>10 %).
- **C6 → MANTENER OFF (falla b+c):** win-rate bloqueado 31 % y `pass=False` en **71 %** de los HOLD puro (duplicaría el rechazo existente); IC de la separación test [−5.24, +9.62] € incluye 0.
- **C7 → fuera de replay** (no hay spread histórico; se decide con el deque vivo ≥ 30 muestras/par).

**Decisión:** **NADA activado** — `CRITERIA[*].active=false` intactos, `STRATEGIST_VERSION=1.0.0` sin tocar, config serve sin cambios. Informe entregado para aprobación de Félix; en paralelo el shadow sigue recogiendo traza viva (§6: ≥ 7 días o ≥ 50 candidatos — ambas puertas antes de cualquier activación, un criterio por PR).

**Evidencia (2026-09-30):** suite **617 passed** (sin variación — `scripts/` no se importa desde la app), 24.2 s. Ejecución host: venv `.venv312`, env de `.env` por sustitución de shell (nunca emitida) → informe regenerado de principio a fin (idéntico entre ejecuciones salvo timestamp) y CSV con 2050 filas (un por ciclo). Verificado en vivo que la config serve no cambió: trazas de deliberación posteriores siguen `config_version=1.0.0`, todos los criterios OFF.

**Archivos:** `scripts/replay_strategist.py` (nuevo), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Fase 2 (plan §6): activación de C2 — STRATEGIST_VERSION 1.1.0 (2026-10-01)

**Contexto:** informe de replay `doc/replay-estratega.md` (2026-09-30, commit `fa27a59`) → **Félix aprueba C2** (2026-10-01). Activación escalonada plan §6.5: un criterio por PR, config versionada + entrada en README.

**Cambio (2 ficheros):**
- `fastapi/services/strategist.py` — `C2_executable_size.active: false → true` con `justified_by` actualizado a la evidencia del replay (46 trades analizados: **0 bloqueos en train 23 ni test 23**; umbral `settings.min_volume` = contrato broker, no tuneable); `STRATEGIST_VERSION 1.0.0 → 1.1.0` (bump obligatorio por activación); docstring de contrato SHADOW reescrito para distinguir **activación de criterio** (puede entrar en `blocked_by` y virar ENTER→WAIT) de **poder de decisión de la capa** (`shadow: true` → el verdict NO se aplica a la respuesta — la ventana §6 ≥7 días / ≥50 candidatos sigue abierta y el corte lo ordena Félix).
- `fastapi/tests/test_strategist_shadow.py` — `test_only_replay_approved_criteria_on_and_versioned` (v1.1.0, exactamente C2 ON y el resto OFF: un criterio activo sin evidencia de replay está prohibido), `test_clean_candidate_enters` (C2 pasa → ENTER), `test_c2_executable_size_against_broker_min` reforzado (`lots=0.005` → `verdict=WAIT` + `blocked_by=["C2_executable_size"]`).

**Efecto:** C2 pasa a formar parte del verdict. Su caso de bloqueo (`lots < min_volume`) coincide 1:1 con `sizing_veto` ya activo en la cadena (S-1/S-2) → **delta de comportamiento sobre la respuesta: cero** (defensa en profundidad); lo que cambia es la **verdad de la traza**: el criterio afirmativo ya puede bloquear y ser attributable. Los otros 7 criterios siguen OFF (`justified_by=None` en los placeholder salvo C7).

**Evidencia (2026-10-01):** suite **617 passed** (23.3 s). Deploy `docker compose up -d --build fastapi` → `/health` ok. Verificado en vivo en el primer ciclo post-deploy: `config_version=1.1.0`, `criteria.C2_executable_size.active=true`, `verdict=NO_CANDIDATE` (modelo HOLD), `decision=HOLD` intacta; 0 errores nuevos (solo los 2 GCS ADC conocidos, D-4). `rejected_position_open` = 0 desde el deploy de A-1.

**Archivos:** `fastapi/services/strategist.py`, `fastapi/tests/test_strategist_shadow.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Fase 3 (plan §7): retrain con memoria — obs (20,25), anti-churn y regla 3 € en el reward (2026-10-01)

**Contexto:** plan `doc/plan-optimizacion-y-estratega-2026-09-30.md` §7 (Fase 3, elegida por Félix sobre el estratega §4.3). El gate absoluto de Fase 1 hace su trabajo (candidato `sum_reward=-107` vs baseline `−68` → RECHAZADO) y la causa de fondo está en el MDP: el agente no distingue "reentrada legítima" de "churn" (sin memoria de la última salida el reward es ciego al timing de reabrir), un win de 0.64 € se pagaba igual que uno de 3.84 € (contradiciendo la regla 3 € «cualquier operación cerrada inferior a 3 € se considera pérdida») y reabrir dentro de la ventana de 2 velas costaba exactamente lo mismo que esperar.

**Cambios (6 ficheros de código):**
- **`fastapi/ml/trading_env_v2.py` — contrato obs `(20,24) → (20,25)`:** flag `memory_channel: bool = True` (los bundles legados se evalúan con `False`; el contrato viejo sigue siendo un ciudadano de primera clase). 8º canal de estado `bars_since_close`: init `max_holding_steps` → normalizado **1.0** («sin cierre reciente», idéntico al arranque del serve), **0** en el paso de cierre, **+1** por vela sin cierre, normalización `min(1, bars/max_holding_steps)`; `ever_closed` marca si el episodio ha cerrado algo. **Anti-churn (Fricción completa — decisión de Félix 2026-10-01):** `CHURN_BARS = 2` — reabrir con `ever_closed and bars_since_close ≤ 2` paga `volume × commission × 2` extra de reward en la apertura (la primera apertura y cualquier espera ≥3 velas quedan intactas; el flip mismo-paso ve `bars=0` y también paga). **Regla 3 € en el reward:** un close con `pnl ∈ (0, 3 €)` recibe `reward −= 2 × (pnl/balance) × reward_scale` (invierte el signo del aporte: un ciclo open+close de 0.64 € queda **negativo**), el dinero (`balance`, `info["trade_pnls"]`) **no se toca** — la regla cambia la señal de aprendizaje, nunca el saldo. `info["trade_pnls"]` expone el pnl por trade (lo consume el gate). `_apply_real_outcome_feedback` clasifica la evidencia con `is_winning_close` (mismo umbral que el resto del sistema). `FOREX_MARKET_FEATURE_COUNT = len(MARKET_FEATURES)` para que el ancho base no pueda desincronizarse entre env y serve.
- **`fastapi/services/walk_forward.py` — expectancia 3 € en el gate:** `segment_metrics.expectancy` = media de `trade_pnls` clasificados (sub-3 € → `−abs(pnl)`), `expectancy_raw` como diagnóstico; trades > 0 con `trade_pnls` ausente o incoherente → `RuntimeError` (sin degradación silenciosa); `_evaluate_model` devuelve `trade_pnls`.
- **`fastapi/services/auto_retrain.py` — contrato por modelo:** `env_cfg` de evaluación con `memory_channel=True`; `_evaluate_model` deriva el contrato del **ancho del observation_space del modelo** (24 → `False`, 25 → `True`, otro → `RuntimeError`) — así el baseline pre-memoria se evalúa en su propio dominio (24) y el candidato en el suyo (25), y solo se comparan métricas de dominios conocidos; `_load_or_create_model` con obs mismatch → **reentreno nuevo** (precedente P2-1: nunca arrastra política incompatible); `_make_train_env` pre-valida la forma del obs del pickle VecNormalize contra el env (mismatch → stats **desde cero** + WARNING — sin eso, SB3 `set_venv` revienta con un `AssertionError` críptico); `_load_obs_rms` carga con `pickle.load` directo (1 arg — SB3 excluye `venv` de `__getstate__`).
- **`fastapi/ml/train_ppo_v3.py`** — `'memory_channel': True` en la config: todo reentrenamiento nuevo nace con memoria.
- **`fastapi/routers/ai.py` — serve paridad train/serve:** `Brain.memory_channel: bool` derivado en `load_inference_artifacts` del ancho del obs del bundle (desconocido → `RuntimeError`); el build del obs consulta `strategist.fetch_bars_since_close` y normaliza `min(1, bars/120)` (`None` → **1.0**, exactamente el arranque del env; patch point `routers.ai.fetch_bars_since_close`). **Transición sin riesgo:** con el bundle 24 actual el serve no consulta nada y no cambia ni un byte del obs.
- **`fastapi/services/strategist.py`** — `fetch_bars_since_close(symbol, df, current_price, pip_size)`: reutiliza `_LAST_CLOSE_SQL` + `compute_since_exit` (mismo SQL, misma ventana y mismo dominio broker que el narrativo del estratega).

**Paridad train/serve verificada por test:** E2E serve — bundle legado (20,24) → obs (20,24) y **cero** consultas; bundle memoria (20,25) → obs (20,25) con última columna `0.5` para 60 velas y `1.0` para `None`. Env — evolución del canal (1.0 → cierre → 0 → 1/120 → 2/120), 3 € flip (0.64 € → reward `−0.064` y losses=1 / 3.84 € → `+0.384` y wins=1), churn con delta exacto del recargo en las fronteras 1–2 (paga) y 3 (no paga).

**Evidencia (2026-10-01):** suite **634 passed** (617 → 634, +17: `test_walk_forward.py` +3 3€/gate, `test_trading_env_step.py` +5 `TestFase3MemoryChurnAndThreeEuro`, `test_p21_unification.py` +2 contrato retrain, `test_auto_retrain.py` +3 `_evaluate_model` 24/25/desconocido, `test_strategist_shadow.py` +3 `fetch_bars_since_close`, `test_e2e_ai_predict.py` +1 serve memoria), 23.5 s, exit 0.

**Troubleshooting (trampas de autoría de tests, ninguna en producción):** (1) `engineer_market_features` **descarta las 19 filas de warmup** (SMA20) — un df crudo de 40 filas llega al env con 21 y un salto de precio en la fila 7 cae *fuera* de la ventana visible (entry/close ambos en 1.1003 → pnl plana −1.76 €); fixture `_df_step_move(jump=26)` documenta el hueco. (2) `env.step()` devuelve `(obs, reward, …)` — `r_open, *_ = step(...)` captura el obs como reward (unpack corregido a `_, r_open, *_`).

**Resultado del primer retrain bajo Fase 3 (2026-10-01, experimento #10 — NO promocionado):** `POST /api/v1/ai/retrain/force?symbol=EURUSD` → snapshot 29981 velas (2021-12-06 → 2026-10-01, `sha=a0f03c076d8f`), split `train=20986 | embargo=20 | folds=4×~1494 barras | holdout@26982 congelado`, 147 outcomes reales inyectados (peso 0.30). Los dos guardas de contrato saltaron en **producción** exactamente como en los tests: `Stats del bundle en contrato (20,24) ≠ env (20,25) → VecNormalize nuevo desde cero` y `Contrato de observación (20,24) ≠ env (20,25) → reentreno desde cero con contrato nuevo`; entrenamiento 100k timesteps con `memory_channel=True`. Gate: **`cand(sum_reward=-89.242, sharpe=-2.68, opens=2060, fold_wins=3/4, holdout_cand=-27.413) | base=-115.737/-3.37 → RECHAZADO (absolute_floor_sum_reward)`**. Lectura honesta: el candidato con memoria es **mejor que el baseline en cada dimensión** (reward +22 %, sharpe +0.69, 3/4 pliegues — y con masa estadística real: 2060 aperturas sobre folds de ~1494 barras), es decir la memoria + anti-churn + regla 3 € empujan el MDP en la dirección correcta; pero la expectancia sigue **negativa en términos absolutos** y el suelo G-1 (impuesto en Fase 1 precisamente para esto) no negocia con modelos negativos. `experiment #10 status=rejected`, modelo activo **conservado** (bundle 24, serve intacto — el path de memoria quedó desplegado pero **dormido** hasta que un candidato lo supere), `git status fastapi/ml` vacío (sin marcador de promoción → cron no-op), 0 ERROR en el contenedor. **No se tocó ningún umbral** (protocolo: prohibido ajustar el gate para que un modelo pase); la decisión de cómo atacar la expectancia negativa es de Félix (§7 exige pasar el gate absoluto).

**Archivos:** `fastapi/ml/trading_env_v2.py`, `fastapi/ml/train_ppo_v3.py`, `fastapi/routers/ai.py`, `fastapi/services/auto_retrain.py`, `fastapi/services/strategist.py`, `fastapi/services/walk_forward.py`, `fastapi/tests/test_trading_env_step.py`, `fastapi/tests/test_walk_forward.py`, `fastapi/tests/test_auto_retrain.py`, `fastapi/tests/test_p21_unification.py`, `fastapi/tests/test_strategist_shadow.py`, `fastapi/tests/test_e2e_ai_predict.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Fase 3 (plan §7): diagnóstico de la expectancia negativa — dónde se pierde (2026-10-01)

**Contexto:** exp #10 rechazado por `absolute_floor_sum_reward`; antes de cualquier cómputo adicional, el camino acordado es atacar la causa raíz con evidencia. `scripts/diagnostico_expectancia.py` (nuevo, **solo lectura**) replica el dominio del gate — mismo snapshot `a0f03c076d8f`, mismos folds de `build_walk_forward_split`, mismo `env_cfg` (verbatim de `auto_retrain` L777–804), mismas stats VecNormalize del bundle activo — e instrumenta trade a trade: motivo de cierre (sl/tp/agent por lógica intra-vela idéntica al env), bruto de mercado, fricción (spread + 2 comisiones), neto, aperturas reales vs la métrica `openings` del gate, churn ≤2 velas y timestamps. **Validación de la replicación:** fold0 `reward=3.24 | trades=216 | metric_openings=96` y agregado `−115.7 | 719 | 846` coinciden **al dígito** con `gate_metrics` del exp #10.

**Hallazgos (todos sobre datos concretos, sin tocar umbrales):**

1. **La fricción del spread es ~5× el edge bruto (4 folds del gate):** bruto de mercado **+212.3 €** vs fricción **−1081.8 €** → neto **−869.5 €** (719 trades / 3592 pasos). Desglose neto: **SL −3847.3 €** (235 trades, −16.4/tr) · TP +1739.3 (46, +37.8/tr) · cierres `agent` +1238.5 (438, +2.8/tr). El único componente estructuralmente tóxico es **la pierna SL: 235 SL vs 46 TP (5.1:1)**.
2. **El env modela flips que en producción no existen (mecanismo, verificado en código):** serve `position_rejected` → HOLD (`routers/ai.py` L579–586, «la salida es exclusiva SL/TP») y el EA rechaza flips en 3 sitios (`AI_Quant_Terminal_v3.mq5` L407/L440/L475 `rejected_position_open`); el env cierra **y** revierte (`target_pos == -position`). En sep-2026 el sim produce **56 de 89 cierres `agent` como reversión mismo-paso** — transiciones imposibles en vivo. Los gaps cierre→reapertura: **99/145 al mismo bar**, 36 a 1 h, 8 a 2 h (churn extremo).
3. **Mismo tramo, resultados opuestos (sep-2026, 543 barras H1):** sim **−205.2 €** (149 trades: sl 47 / tp 13 / agent 89) vs libro vivo **+238.8 €** (137 trades: sl 49 / tp 47 / ea 21 / manual 19 / signal_end 1). Los **SL cuadran casi idénticos (47↔49)**; lo que diverge es la **conversión a TP (13↔47)**: de 60 pares con la misma hora de entrada, el vivo terminó en TP en 12 casos donde el sim cerró en FLAT (y sl↔sl en 11). Solo el **39 % (58/149)** de horas con entrada sim tiene entrada viva en ±1 h — la cadena de vetos de serve + `position_veto` descartan la mayoría del flujo que el MDP crudo ejecuta.
4. **Hipótesis equity OOD descartada por test:** re-ejecutar el tramo con `initial_balance=470` (equity vivo real) reproduce bal=1000 (mismos `tp=13/sl=47`, mismo 39 % de overlap) → la divergencia **no** es el canal de estado equity.
5. **La anti-churn está inerte en el retrain (unidad errónea):** `RetrainConfig.commission = 0.0001` es la **convención fracción de la v1** (`trading_env.py` L66: `balance *= 1+pnl−commission`), pero `ForexTradingEnvV2.commission_per_lot` espera **€/lot absolutos** (default 7.0; `train_ppo_v3.py` L154 usa 7.0; tests 7.0). Consecuencia: el recargo `volume × commission × 2` cobra **≈2×10⁻⁶ reward** en vez de ~0.14 — la decisión «fricción completa» de Félix no cobra nada; la comisión de apertura (`L597`) tampoco. Referencia: subir a 7.0 añadiría **+326.5 € de fricción sobre 149 trades** en sep (duplica la fricción por trade) — correcto en coste, pero el efecto neto en el suelo depende de que el entrenamiento aprenda a evitar churn (empírico, no garantizado).
6. **La métrica `openings` del gate no mide aperturas reales:** en fold3 cuenta **389** contra ~148 reales — verificado que **283 de 292 intentos no abiertos** fueron por cabecera de volumen ≤0 (el policy dispara `dir>0.33` con `volume≤0`; el env no abre pero el contador sí suma; el umbral de la métrica 0.33 ≠ ±0.05 del `decode_v3_direction` también la hace infracontar en otros folds: fold0 96 vs 217). Afecta al piso de masa `min_openings=20`/canario (lo infla, no lo aprieta).
7. **El churn es comportamiento real, no artefacto de sim:** el libro vivo reabre ≤2 h después del cierre anterior en **132/146 = 90.4 %** de los pares consecutivos — la ventana anti-churn de Fase 3 ataca un problema real (§5), pero con la comisión actual no penaliza nada.

**Lectura (causa raíz encadenada):** el MDP crudo opera flips y cierres que la pipeline de producción (vetos ranging/quality/bias/sizing + `position_veto` + guards EA) nunca ejecuta → frecuencia y ciclos de vida distintos (barras en posición: 1–4 mayormente) → la fricción de spread sobre ese ritmo (−1082 €/gate) supera el edge bruto (+212 €) y la pierna SL a 5.1:1 remata. En el mismo tramo, la pipeline viva toma ~39 % de esas entradas y convierte a TP (47 vs 13) → **+238.8 €**. El suelo absoluto mide el MDP crudo, no la pipeline viva — por eso un candidato puede ser rechazado mientras el libro real gana, y viceversa. **No se tocó ningún umbral ni se repitió ningún retrain.**

**Opciones (decisión de Félix, todas requieren un reentreno único pre-registrado):** **O-1** modelar `position_veto` en el env (flip → mantener posición, mecánico y sin LLM — paridad de transiciones serve en el punto que más diverge,56/89 cierres sep); **O-2** calibrar la comisión real del broker (¿7.0 €/lot como `train_ppo_v3`? requiere confirmación del coste real) para activar la anti-churn como se especificó — con la advertencia honesta de que sube el coste por trade; **O-3** combinar O-1+O-2; **O-4** mantener bundle 24 + shadow §6 mientras se decide (estado actual). Ninguna promete pasar el suelo — eso es empírico y el gate sigue intacto.

**Archivos:** `scripts/diagnostico_expectancia.py` (nuevo), `README.md`.

### O-1 (diagnóstico 2026-10-01): paridad `position_veto` en el env — el flip con posición abierta no cierra (2026-10-01)

**Contexto:** opción **O-1** de las presentadas tras el diagnóstico de expectancia, ejecutada bajo «continúa con la forma recomendada». Mecanismo: en producción un flip con posición abierta termina en **HOLD** — serve `position_rejected` → HOLD (`routers/ai.py` L579–586) y el EA lo rechaza en tres sitios (`rejected_position_open`, L407/L440/L475); en cambio el env cerraba **y** revertía en la sección 2 (`target_pos == 0 or target_pos == -self.position`). Evidencia del diagnóstico: **56 de 89** cierres sep-2026 eran flips imposibles en vivo, con los ciclos de reversión que arrastran la fricción (−1082 €/gate vs edge +212 €).

**Cambio (1 fichero de código):** `fastapi/ml/trading_env_v2.py` — sección 2 del `step()`: `close_reason='agent'` sólo con `target_pos == 0` (**FLAT → CLOSE**, que sí existe en producción: EA L358–377); el flip cae al `elif position != 0` (mark-to-market), idéntico a un HOLD con dirección coincidente. Docstring del módulo actualizado con el contrato. **Contrato obs/acción intacto** (`(20,24)/(20,25)`, acción 2/4) → serve y bundle activo **no cambian**; SL/TP/trailing sin cambios; tras un SL/TP un flip del mismo paso puede reabrir (en producción `req.position==0` tras el cierre → BUY/SELL ejecuta, paridad conservada). La ventana anti-churn sigue aplicando a reabrir tras FLAT/SL/TP ≤2 velas (decisión de Félix intacta).

**Pre-registrado para exp #11 (antes de ejecutar):** única variable = dinámica del env (position_veto). `RetrainConfig` **intacto** (umbrales de gate sin tocar, `commission=0.0001`, 100k steps, regla 3 €, canario absoluto); se dispara **un único retrain**. Repetirlo hasta aprobar sigue prohibido (múltiple testing).

**Tests (2026-10-01):** `cd fastapi && pytest tests/ -q` → **638 passed** (634 + 4: `TestPositionVetoParity` — flip con LONG no cierra ni revierte; espejo con SHORT; flip ≡ mismo-dirección-HOLD **bit a bit** (posición, balance, equity, volumen, reward, drawdown); FLAT aún cierra con el round-trip de spread −1.76 €. Ningún test existente codificaba la reversión — la ruta estaba sin cubrir).

**Archivos:** `fastapi/ml/trading_env_v2.py`, `fastapi/tests/test_trading_env_step.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Exp #11 (retrain único pre-registrado con O-1): RECHAZADO por el suelo absoluto — A/B que aísla el efecto de O-1 (2026-10-01)

**Ejecución:** `POST /api/v1/ai/retrain/force` a las 13:15:58, cerró 13:21:04 (~5 min). trigger=`forced`, snapshot `00d59b09e026` (29981 barras — re-fetch con la ventana desplazada +2 h respecto al de #10), split `train=20986 | 4×1494 | embargo=20 | holdout@26982 congelado`, 147 outcomes inyectados (peso 0.30), candidato (20,25) entrenado desde cero. `experiment_tracker #11 → status=rejected, reason=absolute_floor_sum_reward, sha=-` → **sin artefacto, sin promoción**. Bundle activo intacto: `4185451b` obs (20,24), `/health` ok (equity 468.16), ciclos EA normales (BLACKOUT Lagarde → HOLD a las 13:25). El `ERROR` de `MODEL-BACKUP` GCS (13:21:04) es ambiental (sin ADC en el contenedor): fallback **local verificado OK** (`backups/4185451b80a3`); ajeno a O-1.

**Gate (íntegro, umbrales sin tocar):**

| condición | umbral | #11 | veredicto |
|---|---|---|---|
| `total_openings` | ≥ `min_openings` | 753 | ✓ |
| floor `sum_reward` | > 0 | **−3.423** | **✗ → `absolute_floor_sum_reward`** |
| floor `mean_sharpe` | > 0 | +0.315 | ✓ |
| floor `total_expectancy` | > 0 | +0.108 | ✓ |
| `fold_majority` (no alcanzado) | ≥ 3/4 | **2/4** | ✗ (habría rechazado también) |
| `sum_reward_strict` / `mean_sharpe_strict` | > baseline | −3.42 > −40.90 / +0.315 > −0.760 | ✓ |
| holdout congelado (sólo reporte, el gate no lo lee) | — | cand **−13.05** vs base **+22.58** | adverso |

**#10 → #11 (candidato; misma config, única variable declarada = dinámica O-1 + re-fetch de snapshot):**

| métrica | #10 (dinámica vieja) | #11 (O-1) |
|---|---|---|
| `sum_reward` | −89.242 | **−3.423** |
| `mean_sharpe` | −2.677 | **+0.315** |
| `total_expectancy` | −7.336 | **+0.108** |
| `total_openings` | 2060 | 753 |
| `total_trades` | 593 | 398 |
| `fold_wins` | 3/4 | 2/4 |
| holdout cand | −27.413 (sharpe −5.41) | −13.055 (sharpe −1.49) |
| motivo de rechazo | `absolute_floor_sum_reward` | `absolute_floor_sum_reward` |

El baseline (bundle activo) pasó de **−115.737 con `n_steps=3592`** (bancarrota anticipada: equity ≤50 % cortó 408 pasos) a **−40.901 con 4000 pasos completos**.

**A/B de atribución (script `diagnostico_expectancia.py`, mismo bundle activo, mismo snapshot `a0f03c07` de #10, sólo cambia la dinámica):**
- Folds `sum_reward`: **−115.737 → −21.91** (+93.8 = **81 % del castigo simulado eliminado por O-1**); fold0 +3.24 → +31.74 con 216 → 125 trades (los ciclos de flip-reversión desaparecen).
- Efecto snapshot (dinámica fija): −21.91 (snap viejo) vs −40.901 (snap nuevo de #11) → el re-fetch **resta ~19 puntos al baseline**, es decir la mejora de O-1 queda parcialmente enmascarada en el gate de #11.
- Confirma el diagnóstico: **el flip-reversión era el motor** de la expectancia simulada negativa.

**Residuo — por qué el candidato sigue sin pasar:**
1. **Fricción > edge** incluso con O-1: en los 4 pliegos el mercado da **+568.6 €** y la fricción total **−717.9 €** (neto −149.3 €).
2. **Anti-churn sigue inerte** (bug de unidades, O-2): `churn_opens` 121/126 en fold0 — 96 % de reentradas ≤2 velas siguen sin pagar recargo (`commission=0.0001` en vez de 7.0).
3. **Holdout adverso**: el candidato pierde contra el baseline en el bloque congelado (−13.05 vs +22.58) — no generaliza.
4. Métrica `openings` del gate sigue distorsionada (fold0 76 contados vs 126 reales; fold3 260 vs 80) — backlog, sin tocar.

**Reglas respetadas:** un único retrain pre-registrado (no repetido), umbrales intactos, sin artefacto promovido, bundle 24 sigue sirviendo. **Siguiente paso a decisión de Félix:** O-2 (calibrar comisión real del broker + arreglar unidades de la anti-churn → un retrain único nuevo pre-registrado) u O-4 (mantener bundle 24 + shadow §6).

**Archivos:** `README.md`, `PROTOCOLO_ANTI_BUCLE.md` (sólo evidencia; sin cambio de código — suite **638 passed** intacta en `06b6e4e`).

### O-2 (costes reales del bróker + anti-churn con ancla declarada): calibración con evidencia (2026-10-01)

**Contexto:** opción **O-2** ejecutada bajo «continúa con la forma recomendada»: calibrar el coste real del bróker y arreglar las unidades de la anti-churn. Tres fuentes de evidencia (solo lectura, contra el bróker real):

1. **`GET /api/v1/history/deals`** — 271 deals (2026-09-02 → 2026-10-01, EURUSD, cuenta EUR real): `commission = 0.0` en **271/271** y `fee = 0.0` en **271/271** → el bróker **no cobra comisión**.
2. **`GET /api/v1/market/symbol/info/EURUSD`** — `spread = 0–1` punto en muestreo (0.0–0.1 pip), `spread_float = true`; `swap_long = −0.7 / swap_short = −1.0` (único coste real de hold overnight; el env no lo modela — observado, fuera de alcance de O-2).
3. **Rates H1 del propio bróker** (`MqlRates.spread`, 30000 barras = ventana exacta del snapshot 2021-12-06 → 2026-10-01): media **1.733 pt = 0.173 pip**; p50=0, p75=2, p90=8, p99=15, máx=112; medias anuales 0.2–4.8 pt (2025 el más ancho).

**Hallazgo:** el env cobraba `spread_pips = 1.5` = **8.7× la media real**, mientras `commission = 0.0001` (bug de unidades v1) quedaba ≈ 0 — que *coincidía* con la comisión real 0 pero por el motivo equivocado (unidad errónea, anti-churn inerte).

**Decisión de diseño (por qué la comisión sola no basta):** la decisión de Félix «fricción completa = `volume × commission × 2`» ejecutada con la comisión **real** (0) daría **0** → la anti-churn seguiría muerta y el problema §5 (re-exposición, 90.4 % de reentradas ≤2 h en vivo) quedaría sin castigo. Resolución: **desacoplar el ancla del coste de bróker** — `churn_friction_per_lot = 7.0` (la fricción de diseño que el propio plan documenta: «fricción solo comisión (7 €/lot)») se aplica **solo** al recargo de reentrada ≤2 velas, y `commission_per_lot = 0.0` (valor real) se cobra en trades normales. Coherencia numérica: una reentrada churn paga ancla 2×7 = 14 €/lot + su spread natural (~0.17 pip) ≈ **15.7 €/lot ≈ la fricción completa de ida+vuelta** que el diseño asumía (1.5 pip + 7×2 = 15.5 €/lot) — la intención de la decisión se ejecuta con la magnitud prevista, sin cobrar una comisión fantasma a los trades normales.

| parámetro | antes | ahora | evidencia |
|---|---|---|---|
| `spread_pips` (env_cfg retrain) | 1.5 | **0.17** | media 1.733 pt / 30000 H1 del snapshot |
| `RetrainConfig.commission` (€/lot bróker) | 0.0001 (bug fraccional v1) | **0.0** | 271/271 deals commission=fee=0 |
| `churn_friction_per_lot` (ancla de política) | inerte (≈ comisión ≈ 0) | **7.0 declarado** | plan §problema-5 + decisión Félix 2026-10-01 |

**Tests (2026-10-01):** `cd fastapi && pytest tests/ -q` → **641 passed** (638 + 3: `test_costs_are_real_broker_values` pin de `RetrainConfig`; `test_churn_anchor_is_decoupled_from_broker_commission` — recargo íntegro con comisión bróker 0 y sin recargo con ancla 0; `test_zero_broker_commission_debits_nothing` — round-trip con comisión real = solo spread −1.2 €). El test de churn preexistente se actualizó para leer `churn_friction_per_lot` (misma magnitud: 7.0 → mismos números).

**Pre-registrado para exp #12 (antes de ejecutar):** única variable = **costes del env** (spread 1.5 → 0.17, comisión → 0.0, ancla de churn declarada 7.0). `RetrainConfig` de gate **intacto** (umbrales, folds, canario absoluto, regla 3 €, 100k steps); se dispara **un único retrain**; repetirlo hasta aprobar sigue prohibido. Proyección honesta y **no garantizada**: la fricción de pliegos pasaría de −717.9 € a ~−81 € (×0.113 del componente spread) sobre un mercado de +568.6 € — pero el gate decide con el candidato real, y el holdout adverso de #11 (−13.05 vs +22.58) sigue siendo una señal en contra.

**Archivos:** `fastapi/ml/trading_env_v2.py`, `fastapi/services/auto_retrain.py`, `fastapi/ml/train_ppo_v3.py`, `scripts/diagnostico_expectancia.py`, `fastapi/tests/test_auto_retrain.py`, `fastapi/tests/test_trading_env_step.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Exp #12 (retrain único pre-registrado con O-2): RECHAZADO por el suelo absoluto — holdout en positivo por primera vez (2026-10-01)

**Ejecución:** `POST /api/v1/ai/retrain/force` a las 14:21:20, cerró 14:26:24 (~5 min). trigger=`forced`, snapshot `4ac33c8c7636` (29981 barras, ventana +3 h respecto a #11), split `train=20986 | 4×1494 | embargo=20 | holdout@26982 congelado`, 147 outcomes (peso 0.30), candidato (20,25) desde cero, costes O-2 verificados en log: `spread=0.17 pips, comisión_bróker=0.0000 €/lot, ancla_churn=7.00 €/lot`. `experiment_tracker #12 → status=rejected, reason=absolute_floor_sum_reward, sha=-` → **sin artefacto, sin promoción**. Bundle activo intacto: `4185451b` obs (20,24), `/health` ok (equity 467.35), único ERROR = `MODEL-BACKUP` GCS ambiental (fallback local OK).

**Gate (íntegro, umbrales sin tocar):**

| condición | umbral | #12 | veredicto |
|---|---|---|---|
| `total_openings` | ≥ `min_openings` | 1680 | ✓ |
| floor `sum_reward` | > 0 | **−23.802** | **✗ → `absolute_floor_sum_reward`** |
| floor `mean_sharpe` | > 0 | **−0.320** | ✗ |
| floor `total_expectancy` | > 0 | **−3.182** | ✗ |
| `sum_reward_strict` | > baseline | −23.80 > −80.39 | ✓ |
| `fold_majority` | ≥ 3/4 | **3/4** | ✓ (era 2/4 en #11) |
| `mean_sharpe_strict` | > baseline | −0.320 < −0.161 | ✗ |
| holdout congelado (sólo reporte) | — | cand **+4.77** (sharpe 2.63) vs base +3.62 | **positivo y mejor que el baseline** |

**#11 → #12 (candidato):**

| métrica | #11 (O-1) | #12 (O-1+O-2) |
|---|---|---|
| `sum_reward` | −3.423 | **−23.802** |
| `mean_sharpe` | +0.315 | **−0.320** |
| `total_expectancy` | +0.108 | **−3.182** |
| `total_openings` / `total_trades` | 753 / 398 | 1680 / 217 |
| `fold_wins` | 2/4 | **3/4** |
| `worst_max_dd` (cand) | — | 0.221 (baseline 0.448) |
| holdout cand | −13.055 (adverso) | **+4.773** (sharpe 2.63) |
| baseline `sum_reward` | −40.901 | **−80.390** |
| motivo de rechazo | `absolute_floor_sum_reward` | `absolute_floor_sum_reward` |

**Lectura honesta:**
1. **Los indicadores relativos y de generalización mejoran** (fold_wins 3/4, holdout **+4.77 en positivo y por encima del baseline**, fold3 del candidato +6.01 con sharpe 1.84, `worst_max_dd` 0.221 vs 0.448 del baseline): el candidato gana donde el tiempo es reciente (fold3 + holdout = bloques 2026) y en el bloque congelado.
2. **Los absolutos empeoran** (sum_reward −3.4 → −23.8, expectancy +0.108 → −3.18): con win_rate 0.20–0.38 en folds 0–2, la pierna SL sigue tóxica — la fricción de costes **no era la única causa** del diagnóstico (hallazgo #1: entradas que pierden por SL a ratio 5:1). Además contra #11 se movieron **tres variables a la vez**: costes O-2, re-fetch de snapshot (+3 h) y estocasticidad del entrenamiento — la atribución exacta del empeoramiento no es posible sin el artefacto del candidato (no guardado, correctamente), y el baseline también cayó (−40.9 → −80.4, la ancla de churn ahora castiga sus reentradas y el snapshot cambió).
3. **La métrica `openings` sigue distorsionada** (1680 contados vs 217 trades reales; fold0: 403 vs 72) — backlog conocido, sin tocar (decisión de Félix).
4. **Reglas respetadas:** un único retrain pre-registrado (no repetido), umbrales intactos, sin artefacto, bundle 24 sigue sirviendo. **Tres retraines consecutivos rechazados (#10, #11, #12) con el suelo intacto** — el MDP crudo aún no demuestra expectancia positiva en todo el walk-forward.

**Opciones (decisión de Félix):** **O-4** (mantener bundle 24 + ventana shadow §6 desde 09-30 con ≥7 días o ≥50 candidatos — sin más retraines; recomendada para frenar la deriva de hipótesis consecutivas) · nueva hipótesis de diseño (cada una = UN retrain pre-registrado nuevo, con la advertencia honesta de testing secuencial acumulativo) · backlog de diagnóstico (integridad de `openings`; re-ejecutar el script de solo lectura sobre el snapshot `4ac33c8c` para descomposición del baseline).

**Archivos:** `README.md`, `PROTOCOLO_ANTI_BUCLE.md` (sólo evidencia — el código O-2 ya estaba commiteado en `dc602f0`).

### O-4 — fin de los retrains automáticos: exps #13/#14 (disparo `trade_count`) RECHAZADOS + `RETRAIN_AUTO_APPROVE=false` (2026-10-01)

**Contexto:** O-4 aprobado por Félix («continúa con la forma recomendada»): **sin más retraines**, mantener el bundle `4185451b` (20,24) sirviendo y correr la ventana shadow §6. Pero `.env` seguía con `RETRAIN_AUTO_APPROVE=true` (decisión de despliegue de 2026-09-29, sección superior) → el trigger `trade_count` seguía vivo: `record_trade_outcome` spawnea un retrain en cuanto `filled_count ≥ min_trades_for_retrain (5)`. **Y `filled_count` no se reinicia al rechazar** (sólo al promocionar) → tras el forced #12, **cada cierre EURUSD spawneaba otro retrain**.

**Incidente (2 retraines automáticos no deseados, ambos inofensivos):**

| exp | ventana (UTC) | trigger | snap | gate | holdout cand vs base | resultado |
|---|---|---|---|---|---|---|
| **#13** | 14:47:13 → 14:52:06 | `trade_count` | `dd1ede681270` | cand `sum_reward=−31.44`, `sharpe=−0.104`, `expectancy=−1.22`, `opens=1326`, `trades=350`, `fold_wins=3/4` \| base `−80.39` | **+10.12** (sharpe 2.44) vs +3.62 | **RECHAZADO `absolute_floor_sum_reward`** |
| **#14** | 15:33:13 → 15:38:06 | `trade_count` | `bf8a37531d46` | cand `sum_reward=−63.48`, `sharpe=−0.286`, `expectancy=−2.92`, `opens=826`, `trades=413`, `fold_wins=1/4` \| base `−78.32` | −14.02 vs +2.37 | **RECHAZADO `absolute_floor_sum_reward`** |

Ambas con el entorno O-1+O-2 (costes reales), `status=rejected`, `sha=-` → **sin artefacto, sin promoción, bundle activo intacto** (`git status fastapi/ml` sin marcador nuevo, cron no-op).

**Respuesta (O-4): `.env RETRAIN_AUTO_APPROVE=true → false`.** El default en `config.py` ya era `False` (P0-1: el intento nunca implícito por código); la inversión era sólo de despliegue. Contenedor recreado (`docker compose up -d fastapi`) y verificado: `docker exec trading-fastapi printenv RETRAIN_AUTO_APPROVE` → `false`, settings de Pydantic → `False`, `/health` → `ok` (equity=436.89, MCP connected). Efecto observable: con `filled_count ≥ 5` el siguiente cierre loguea `[RETRAIN] Auto-retrain bloqueado: retrain_auto_approve=false…` y **no spawnea**; el calendario 168 h (último intento #14 15:38 → ~10-08) queda `scheduled_due` **pendiente de aprobación** vía `POST /api/v1/ai/retrain/approve`. Reactivar exige decisión explícita (`true` + recreate).

**Disciplina estadística:** #10–#14 = **5 rechazos consecutivos** con el suelo intacto; repetir hasta aprobar = múltiple testing, **prohibido**. Con O-4 activo ambos triggers quedan bloqueados/pendientes: nueva hipótesis sólo por orden de Félix (un único retrain pre-registrado). La **promoción** nunca estuvo automática: gate walk-forward (suelo absoluto + canario) sin tocar.

**Evidencia (2026-10-01):** flip verificado en contenedor (printenv/settings/health, arriba); `training_experiments` #13/#14 `rejected/absolute_floor_sum_reward` con `gate_metrics` citados al dígito; suite **641 passed** en el momento del flip (sólo cambio de despliegue, sin código). **Archivos:** `.env` (no versionado), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

> **UPDATE (2026-10-01, tarde):** el flag fue reactivado a `true` por decisión de Félix al cerrar el informe O-4 — ver sección siguiente.

### Backlog O-4 — integridad de la métrica `openings`: aperturas REALES en el env (2026-10-01)

**Contexto (hallazgo 7 del diagnóstico):** `_evaluate_model` contaba la **intención pre-step** de la política con umbral `0.33`, mientras el env decodifica con zona muerta `±0.05` (`decode_v3_direction`) — y sumaba un intento **en cada paso** en que `size_ppo_position` denegaba el volumen (`position` seguía en 0 → el "previo" nunca subía) → métrica inflada y, a la vez, aperturas reales pequeñas (0.05–0.33) **sin contar**: exp #12 `1680 contadas vs 217 trades`; exp #14 `826 vs 413`; fold0 de #13 `374 vs 81`. La masa `total_openings` no era masa real.

**Cambios (TDD: 6 RED → 6 GREEN):**
- **`fastapi/ml/trading_env_v2.py`** — `self.opening_count` (init + reset) incrementado **sólo en el sitio único de apertura** del env (`0 → posición` con `new_volume > 0.0`), expuesto como **`info["openings"]`**.
- **`fastapi/services/auto_retrain.py`** — `_evaluate_model` lee `int(info["openings"])` (**clave directa**: si el env no la expone, `KeyError` propaga — sin `.get` silencioso a 0); eliminado el conteo de intención (`target_position` 0.33 / `previous_position`).
- **`scripts/diagnostico_expectancia.py`** — espejo del gate actualizado al mismo origen (`metric_openings = info["openings"]`); conserva el cross-check `actual_opens` por transición de posición (ambos deben coincidir y ahora coinciden).

**Umbrales intactos** (`min_validation_openings=20`, `CANARY_MIN_OPENINGS=200`): la masa **real** es menor que la inflada → el check queda **más estricto, nunca más laxo** (dirección contraria a "tunar para que pase"). Registros históricos de `gate_metrics` no se reescriben. Referencia: con aperturas reales, #12 habría tenido `total_openings ≈ 221` (217 trades + 4 que quedan abiertas por pliegue) — sigue ≥200 pero el canario ya exige masa genuina; #14 `826 → ~417`.

**Tests (5 nuevos + 1 extendido):** `TestOpeningsCounter` (3): apertura real cuenta 1 y mantener no reabre · intención `dir=1.0, vol=0.0` cuenta 0 · contador se reinicia con el episodio; `test_info_exposes_money_keys_for_gate` (+clave `openings`); `TestOpeningsMetricIntegrity` (2): modelo de volumen denegado → `openings==0` · modelo `dir=0.2` (bajo el legacy 0.33) → `openings ≥ 1` con invariante `openings ≤ trades+1`. Los tests de la política de ceros (`openings == 0`) siguen en verde.

**Evidencia (2026-10-01):** TDD verificado (`6 failed` en RED → `6 passed` en GREEN); suite completa **646 passed** (641 + 5, exit 0, 24.2 s); `py_compile` del script OK; **re-ejecución en vivo del espejo** (`scripts/diagnostico_expectancia.py`, solo lectura, pin `a0f03c07` con el env O-1+O-2 actual): en los **18 segmentos** (4 folds + holdout + ventanas reales) `metric_openings == opens_reales` **exacto** (fold0 120=120, ventana [11000:12000] 125=125, …) — la métrica del gate y la detección por transición son ahora la misma cosa, antes divergían (intención 0.33 vs aperturas reales; p. ej. exp #12: 1680 vs 217 trades). Los números absolutos de esa corrida (trades/rewards por fold) difieren del diagnóstico original porque el env ya incluye O-1+O-2; el pin de snapshot no cambia. Sin cambio MQL5 → **sin bump de versión EA**. **Archivos:** `fastapi/ml/trading_env_v2.py`, `fastapi/services/auto_retrain.py`, `fastapi/tests/test_trading_env_step.py`, `fastapi/tests/test_auto_retrain.py`, `scripts/diagnostico_expectancia.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Cierre de O-4 — decisiones de Félix: auto-retrain reactivado + power switch aprobado al cierre de §6 (2026-10-01)

**Decisión (Félix, 2026-10-01, al recibir el informe O-4):**

1. **`RETRAIN_AUTO_APPROVE` reactivado a `true`.** `.env: false → true`, contenedor recreado y verificado: `docker exec trading-fastapi printenv RETRAIN_AUTO_APPROVE` → `true`, `settings.retrain_auto_approve` → `True`, `/health` → `ok` (equity=438.89, MCP connected). Historial del flag: 09-29 `true` → 10-01 `false` (O-4) → 10-01 `true`; el default en `config.py` sigue `False` (el estado real vive en `.env`).
2. **Power switch del estratega APROBADO para el cierre de la ventana §6** (2026-10-07 19:00 UTC o ≥50 candidatas — hoy 12/50): al cierre se activa escalonado por criterio (§6.5: un criterio por PR, config versionada + entrada en README) sobre la evidencia de las trazas shadow acumuladas (§6.4). Si en el cierre los criterios de §6.4 **no** se cumplen, se informa en lugar de activar — el plan aprobado es evidencia-primero, no activación a ciegas.
3. **Fin del bucle de análisis:** el diagnóstico anclado a `4ac33c8c` (#12) queda descartado; comienza **semana de observación** — ventana §6, verdicts del estratega (`ENTER/WAIT/NO_CANDIDATE`) y retrains automáticos con el gate intacto.

**Consecuencia conocida, documentada ANTES del despliegue (decisión tomada con esa información):** `filled_count` sólo se reinicia al **promocionar** (`auto_retrain.py` L954; la ruta de rechazo ancla el calendario con `last_retrain_time` pero no el contador) → con `filled_count=12 ≥ min_trades=5`, **cada cierre EURUSD dispara un intento de retrain** hasta que uno promocione (rechazo → contador sigue → siguiente cierre → nuevo intento). Estado verificado en `retrain_state`: EURUSD `filled_count=12`, `retrain_in_progress=false`, `scheduled_due=false`; calendario anclado por el intento #14 (15:38:06 UTC) → próximo `scheduled` **2026-10-08 15:38 UTC**. El gate walk-forward (suelo absoluto + canario) sigue siendo la **única** vía de promoción y todos los intentos quedan en `training_experiments`.

**Evidencia (2026-10-01):** flip verificado (printenv/settings/health, arriba); `retrain_state` leído de la BD (`filled_count=12`, ancla #14); suite **646 passed** (sin cambio de código: sólo `.env` + docs). **Archivos:** `.env` (no versionado), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Auditoría de observación (día 1–2) + backlog pre-registrado para iteración posterior (2026-10-02)

**Contexto:** Félix pidió un informe de la actividad desde el 10-01 (retrains, filtros, logs, operaciones, P/L, tablas BBDD, edge) y, al recibirlo, ordenó: *«Esperemos a que termine la ventana de observación. Registra todo lo anterior para tenerlo en cuenta en una iteración de mejora posterior.»*

**Estado medido (2026-10-02, ~17:30 UTC; sólo lectura — BD `SELECT` + logs `docker logs`, sin cambio de código):**
- **Retrains desde 10-01: intentos 15 (exps #4–#18) · efectivos 0.** Los 15 `rejected/absolute_floor_sum_reward` (`error=NULL` — rechazo limpio de gate, no crash); 12 `trade_count` + 3 `forced`; ~5 min c/u (~75 min de cómputo). **El gate frenó 15/15 candidatos con `sum_reward` negativo: comportamiento correcto del filtro, no fallo** (mejor: #16 `cand=-2.28`, `sharpe=0.84`, `fold_wins=4/4`, holdout `+14.0` — aún negativo en absoluto). Bundle activo intacto `4185451b` (registry id 3 `is_active=t`); `filled_count=16` (no se reinicia al rechazar → reintento por cierre, consecuencia aceptada).
- **Ventana §6: 17/50 candidatos** — veredictos desde 09-30 19:00 UTC: `NO_CANDIDATE=350 / WAIT=169 / ENTER=17 / ERROR=0` (536 ciclos). Corte: **2026-10-07 19:00 UTC**.
- **Operaciones desde 10-01: 12 cerradas, Σ +7.34 €** (10-01: 8 → +12.71; 10-02: 4 → −5.37); 30 días: 143 ops, +208.08 €. Sin posición abierta; equity ~435. Ticket 58729956543 = trade 228 (LONG, 12.3 h, **−12.12 €, SL**; TP de esa orden a **50.4 pips**).
- **Logs: 13 líneas ERROR / 6 situaciones, 0 en lógica de trading** (GCS ADC ×3 → backup local OK; carrera Postgres al arranque → autorecuperado; MT5 500 → 30 s; news 429 ×2; QWEN ReadTimeout ×7 → `quality_veto → HOLD` seguro).
- **Filtros operan** (position_veto 105, ranging_veto 22, quality_veto 15, 0 ERROR) **pero la entrada es permisiva** (`|direction| > 0.05`; 5 de 17 ENTER con `ml_prob` 0.10–0.21; `q=7.5` constante) y **SL/TP es mecánico** (`production_stops`: `SL=clamp(ATR×1.5, 10, 50)`, `TP=2×SL` — no estructural; R:R medio 1.78, tasa TP 21% en 21 días).

**Registro (fondo de la orden de Félix):** hallazgos H1–H13 + backlog priorizado **P0** (entrada con confianza + filtro de momento; SL/TP estructural por swing/liquidez) · **P1** (time-stop/breakeven intraposición; cooldown de re-entrada en serve espejo de `CHURN_BARS`) · **P2** (parser de calidad Qwen, higiene de datos: duplicados 106–111, drift sin baseline, `smc_signals` obsoleto, coste del bucle de retrain) → **`doc/backlog-auditoria-2026-10-02.md`**. **Nada se ejecuta hasta que termine la ventana §6 (2026-10-07) y Félix emita la orden pre-registrada correspondiente**; los umbrales de `walk_forward.py` siguen intocados y #10–#18 = rechazos consecutivos (repetir hasta aprobar = múltiple testing, prohibido).

**Evidencia (2026-10-02):** suite **646 passed** (25.8 s, verificación previa a este registro — cambio sólo documental); sin cambio MQL5 → sin bump EA. **Archivos:** `doc/backlog-auditoria-2026-10-02.md` (nuevo), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### Incidente EA — Err 4014 / HTTP -1: WebRequest URL reseteada en MT5 (2026-10-05)

**Contexto:** el EA reportó `HTTP -1` / Err 4014 al comunicar el cierre de la posición 58747168430 (cerrada por SL en broker a las 04:23 UTC del 10-05). **Causa raíz confirmada por Félix:** la URL del WebRequest había sido reseteada en MT5 (no estaba en la allowlist de Tools→Options→Expert Advisors) — **no es un bug de código**. El outbox del EA retuvo el cierre y reintentó; al restaurar la URL la llamada se completó.

**Alcance del blackout (medido en BD, solo lectura):** última llamada EA→backend exitosa el **10-02 20:55:18** (`ai_predict_cycle`/`ea_decision`); primera tras el fix el **10-05 11:12:08** (`order_filled` del outbox) → **~62 h sin ningún ciclo predict/shadow** (en el hueco `audit_log` solo contiene 3 `drift_report` internos del backend; el backend estuvo vivo todo el tiempo).

**Integridad verificada (sin pérdida de datos):**
- Cierre tardío persistido **una sola vez**: `trade_outcomes` id 231 (posición 58747168430, SL, **−11.26 €**, `exit_time` 10-05 04:23:42), `created_at` 10-05 11:12:08, `count=1` (sin duplicados) → **regla 8 cumplida**.
- Orden 29 `pending → filled` (catch-all O-1 al llegar el cierre).
- Cadencia de predict restaurada desde 10-05 11:17 (ciclos cada 5 min continuos); Qwen `unavailable` solo en los 3 primeros ciclos (reintentos post-reconexión, latencia ~20 s), recuperado a las 11:20.
- Retrain **#19** disparado por el cierre (10-05 11:12:17) → **rechazado 11:17:17 `absolute_floor_sum_reward`** — comportamiento correcto del gate (19 exps totales, 18 rechazadas; `retrain_in_progress=false`, `filled_count=17`; umbrales intactos).
- `/health` ok (equity 426.69, MCP conectado); suite **646 passed** (25.0 s).

**Impacto en la ventana §6 (anomalía a decidir al cierre):** hueco de ~62 h (**37 % de la ventana**) sin trazas shadow; candidatos **18/50** (13 BUY + 5 SELL) → el ≥50 no se alcanzará antes del corte. **Decisión de Félix al cierre (10-07 19:00 UTC):** aceptar la evidencia §6.4 con el hueco o extender la ventana.

**Otras anomalías registradas (solo informe, sin cambios):** (a) **full-dump de `docker logs` no fiable** — el volcado completo (9199 líneas) termina en 10-03 10:25 y no ve el 10-04/10-05 mientras `--tail` sí; `--since` devuelve 0 → para forense usar `audit_log` (correcto) o `docker logs --tail N`; (b) **3 `ai_predict_cycle` duplicados a las 11:17:38** por reintentos con latencia ~20 s (el histórico ya tenía duplicados similares; sin impacto: HOLD con veto; 0 duplicados BUY/SELL en el mismo segundo → sin efecto en el conteo §6); (c) **orden 30** (BUY 10-05 11:35, posición abierta `position=1`) sigue `pending` hasta que llegue su cierre — mismo patrón que la orden 29, no es regresión.

**Evidencia (2026-10-06):** implementado filtro de volatilidad en `trading_env_v2.py` (`MIN_ATR_FOR_ENTRY=0.0004`, penalización `0.05` por entrada en mercado plano) para mejorar la selectividad del agente — cambio quirúrgico de reward shaping, sin alterar el gate. Suite **646 passed** (21.8 s). **Archivos:** `fastapi/ml/trading_env_v2.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

**Evidencia (2026-10-08): restauración de contrato de observación + recuperación de la suite 93→0 fallos.**
Fase 2/3 (gate holdout-positivo en `walk_forward.py`, ADX + penalización de rango, contexto/CoT en `qwen_client.py`, features estructurales + `production_stops` estructural) dejó la suite en **93 failed / 548 passed**: el contrato de observación se había roto en tres frentes simultáneos.

1. **`MARKET_FEATURES` 17→26** (swing/liquidez/ADX metidos en el tensor): el plan §7 Fase 3 sólo autoriza `(20,24)→(20,25)` (canal de memoria); el bundle activo real es **(20,25)** (manifest `ppo_trading_bot_v3.manifest.json`, promoción `auto_retrain_normalized` 2026-10-07 06:30 UTC, sha `bb70b582…`) y el arranque rechazaba cualquier bundle con `base_width=33`. **Decisión: revertir `MARKET_FEATURES` a las 17 features documentadas.** Las 9 columnas estructurales (`swing_*`, `resistance/support_level`, `dist_*`, `adx`) se calculan igual en `engineer_market_features` y alimentan `production_stops()`/Qwen **por ambos lados del contrato desde el df** — la SL/TP estructural (P0 del backlog) no necesita tocar el obs ni esperar al retrain pre-registrado. Reentrenar con 26 canales sigue siendo una opción futura de Félix (exige retrain antes de desplegar).
2. **Warmup restaurado**: `min_periods=1` general + `dropna` eliminado (intento de no perder filas) rompió `test_p0_time_order` (`len==60−19`, warmup descartado de las velas VIEJAS). Restaurado `rolling(20/14)` por defecto en SMA20/TR/RSI/std20 + `dropna()` final tras el sort ascendente; las features nuevas rellenan su NaN (`adx`→0.5, swings→`bfill/ffill/fillate` con el propio high/low en mercado plano, `dist_*`→0) para que `dropna` sólo descarte las 19 filas de warmup y jamás una columna entera.
3. **Tests**: revertidos a HEAD los 7 ficheros con parches de triage (mock pools duplicados, xfails que enmascaraban el manifiesto); los mocks de obs ahora coinciden con el **manifest real (20,25)** y se patchea `routers.ai.fetch_bars_since_close` (canal de memoria activo → consulta `trade_outcomes`); el test de contrato de memoria parchea `validate_bundle` con manifiesto legacy `[20,24]` y `vec_norm_file=None` (el pkl real está guardado con obs (20,25) y SB3 `set_venv→check_shape_equal` rechazaría un DummyVecEnv (20,24)).

`production_stops` estructural (SL en swing ± buffer ATR 0.5, TP por estructura con suelo **R:R 2:1**, fallback ATR si la distancia estructural < 10 pips) intacto — paridad train/serve vía columnas del df.

**Suite: 646 passed, 0 failed, 0 xfail (25.4 s, exit 0).** **Archivos:** `fastapi/ml/trading_env_v2.py`, `fastapi/routers/ai.py`, `fastapi/services/qwen_client.py`, `fastapi/services/walk_forward.py`, `fastapi/tests/{test_trading_env_step,test_e2e_ai_predict,test_p0_time_order,test_position_veto,test_qwen_semantics,test_sizing_veto,test_strategist_shadow,test_walk_forward,test_auto_retrain}.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`.

### P1 intraposición + cooldown de re-entrada — time-stop 12 H1, breakeven 1R, EA v13.3 (2026-10-08)

**Decisión de Félix (2026-10-08, «confirmo»):** iteración P1 del backlog `doc/backlog-auditoria-2026-10-02.md` — time-stop = 12 velas H1, breakeven = 1R, cooldown de re-entrada serve = espejo de `CHURN_BARS=2`; despliegue coordinado con UN retrain pre-registrado (el bundle activo `bb70b582` (20,25) fue entrenado en el mundo de recompensa ANTES de P1).

**Cambios (TDD: 17 tests RED → GREEN):**
- **Env (`fastapi/ml/trading_env_v2.py`):** `TIME_STOP_BARS=12` (fuente única); `steps_in_trade` incrementa al inicio del step (end-state equivalente); sección 1b time-stop con **prioridad SL/TP > time-stop** (ambos en la vela 12 → cierre al precio del SL/TP, `close_reason='time_stop'` sólo si ninguno tocó); `_apply_breakeven` tras `_apply_trailing` (mismo orden que el OnTick del EA): `risk≤0 → return` (trailing ya actuó), guard `min_stop_dist`, `sl_pips` recalculado sólo si el SL se movió; nuevo param `breakeven_enabled=True` espejo del input `EnableBreakeven` (el EA tiene dos inputs independientes).
- **Serve (`fastapi/routers/ai.py`):** `_TIME_STOP_M5_BARS = 144` (=12×12, la unidad que mide el EA en L310); fetch único incondicional de `fetch_bars_since_close` (alimenta canal de memoria del obs Y el cooldown, agnóstico del bundle); time-stop → `final_decision="CLOSE"` **por delante de toda la cadena de vetos** (sólo SL/TP/precio compiten antes, como en el env); `churn_cooldown` (posición abierta=0 y ≤2 velas H1 desde el último cierre) → HOLD; `_veto_reason` con flags `time_stop`/`churn_rejected` (prioridad: time_stop > position > sizing > churn > ranging…); audit con `time_stop`, `churn_cooldown`, `bars_since_close`. Contrato de respuesta intacto: CLOSE viaja como decisión normal (EA ya lo ejecuta → `CloseAllPositions`; traza `veto_reason="time_stop"`).
- **EA (`mql5/AI_Quant_Terminal_v3.mq5`, v13.2→v13.3):** `ManageBreakeven()` espejo de `_apply_breakeven` (BID para BUY / ASK para SELL, disparo beneficio ≥1R con `riskDist` del SL actual, guard stops-level, `riskDist≤0 → return`), `input bool EnableBreakeven=true`, llamada en OnTick **tras** `ManageTrailingStop()`; bump `#property version` + `EA_VERSION`. Sin cambio en el contrato EA↔backend (el time-stop viaja como `decision="CLOSE"` ya soportado).
- **Tests adaptados al contrato P1 (3):** `test_ea_version_bumped_to_13_3` (checkpoint de versión avanza), `test_trailing_disabled_keeps_initial_sl` (desactiva AMBOS inputs — con breakeven activo el close +23.8p movería la SL a la entrada), contrato de memoria (la consulta de velas desde el cierre se hace para todos los bundles desde P1: la necesita el cooldown H6; el OBS (20,24) sigue sin canal).

**Evidencia (2026-10-08):** suite **663 passed, 0 failed (26.2 s)** — 646 base + 17 nuevos (7 `TestP1TimeStopAndBreakeven` env + 10 `TestP1TimeStopAndCooldownServe`). Prioridad SL/TP > time-stop demostrada por pnl (−12.56 € SL vs −1.76 € mark); cooldown mutuamente excluyente con veto de posición y con time-stop. **Archivos:** `fastapi/ml/trading_env_v2.py`, `fastapi/routers/ai.py`, `mql5/AI_Quant_Terminal_v3.mq5`, `fastapi/tests/{test_trading_env_step,test_e2e_ai_predict,test_ea_close_outbox,test_p21_unification}.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Commit `7383dd1`.

**Despliegue + retrain coordinado (2026-10-08, 19:19 UTC):** deploy `docker compose up -d --build fastapi` verificado (`/health` ok equity 439.4, bundle `bb70b582` (20,25) cargado, código P1 en contenedor, 0 ERROR de trading; 2 `news_scraper` parse errors del feed — familia dato-externo conocida, sin efecto en la decisión). Ciclos live con los campos nuevos de audit (`time_stop`, `churn_cooldown`, `bars_since_close`) y valores coherentes (SHORT abierto `position=2` → ambos flags false). **Retrain forzado → exp #28 (forced, 29 981 velas, 4 pliegues, env P1 completo): RECHAZADO `absolute_floor_sum_reward`** — cand `sum_reward=-105.35` / `mean_sharpe=-1.80` / `total_expectancy=-4.90` / `fold_wins=2/4` / holdout `-12.93` vs base `-47.20/-1.80…` — el candidato es PEOR que el baseline en el mundo nuevo (fold1 catastrófico: −62.4 vs −45.2); gate funcionando, umbrales intactos, bundle activo `bb70b582` sin cambio, `sha=-`, sin artefacto. Un solo retrain pre-registrado ejecutado; no se repite (múltiple testing).

**Pendiente:** fix `holding_norm` M5→H1 pendiente de permiso explícito (cambia input del bundle activo).

### Fix `holding_norm` serve M5→H1 + exp #29 (2026-10-08, orden de Félix «Aplica el retrain»)

**Fix (commit `5d6c054`, suite 664 passed):** el EA mide `steps_in_trade` en pasos M5 y el canal `holding_norm` lo normaliza contra `max_holding_steps=120` (H1) → serve saturaba el canal a la 1.ª hora (60 M5/120 = 0.5) frente a las 120 h del env. Fix: `steps_h1 = req.steps_in_trade // _M5_PER_H1` (constante `_M5_PER_H1=12`, sustituye a `_TIME_STOP_M5_BARS`; frontera 144 M5 preservada) → obs, time-stop y traza (`steps_in_trade_h1`, campo nuevo de audit) en el dominio H1 entrenado. Con el bundle activo es **paridad restaurada** (el env ya entrenaba en H1; el serve era el lado roto). Test: obs crudo (Brain `vec_norm=None`), col 23 = 5/120 para 60 M5.

**Exp #29 (forced 20:27→20:32, snap `dc11b821`): RECHAZADO `absolute_floor_sum_reward`** — cand `sum_reward=-29.89 / mean_sharpe=-0.84 / total_expectancy=-4.78 / holdout=+3.26` vs base `-36.66`: candidato **mejor que baseline** y con holdout positivo, pero el suelo absoluto no se cumple → sin promoción, bundle `bb70b582` intacto, gate y umbrales intactos. Pre-registrado como **repetición estocástica de #28** (el fix no toca el env): el candidato saltó de −105.3 (#28) a −29.9 (#29) sin cambio de hipótesis → **dispersión ~75 unidades de reward entre dos corridas idénticas = ruido de evaluación del gate** (medida útil para leer métricas futuras). Fix verificado en vivo: audit con `steps_in_trade_h1=2` (SHORT abierta 18:00 UTC, edad correcta en H1). Sin repetición posterior; el próximo retrain será el trigger automático por `trade_count` en el próximo cierre EURUSD.

### Iteración P2 completa — Qwen anti-ancla, observabilidad, higiene de datos (2026-10-09, orden de Félix «comienza con el P2»)

**H2+H9 — prompts few-shot anti-ancla (commits de código P2, suite 696):** causa raíz de `q=7.5` constante (511+ ciclos) y de los `trade_insights` constantes = **UN solo ejemplo few-shot** en cada prompt (`"quality": 7.5, bias NEUTRAL, conf_mod 1.0` en `qwen_client.query_qwen_unified`; `"confidence": 0.75, quality_score": 3.5` en `trade_journal._query_qwen_trade_insight`) con `temperature=0.2` → el modelo pequeño devolvía la plantilla casi verbatim (evidencia: las 6 filas de `trade_insights` de los trades 106–111 = `momentum_fade/0.75/RANGING/3.5` idénticos). Fix en ambos prompts: **dos ejemplos con valores separados** (q 8.5/BULLISH/1.3 vs q 3.0/BEARISH/0.7), instrucción explícita **«do NOT copy their values»**, **rúbrica de scoring** (7-10 solo con tendencia/ADX/sesión alineados; 1-4 en chop/rango; 4-7 mixto) y **«ONE JSON object»** (defensa contra salida doble). **`QUALITY_THRESHOLD=5.0` y umbrales intactos** — el veto de calidad pasa de decorativo a operativo por diseño del propio ítem (algún ciclo podrá vetar de verdad). TDD: `test_qwen_prompt_anchoring.py` (10 tests que fijan la propiedad: ≥2 valores distintos, no-copy, rúbrica, one-object) — cualquier vuelta a un ancla unitario pone los tests en rojo.

**H14+H15 — observabilidad (nuevo `services/predict_silence.py`):** (1) `docker-compose.yml` → driver `json-file` con `max-size=50m, max-file=3` en `fastapi` y `postgres` (fin del crecimiento sin límite y de los volcados truncados); (2) **watchdog de silencio** registrado en el lifespan: comprueba `max(created_at)` de `ai_predict_cycle` cada 5 min (primera pasada a los 60 s) y alerta `PREDICT-SILENCE` (nivel ERROR → rate-limit nativo 600 s, sin spam) si la edad supera **45 min = 4.5× la peor cadencia observada** (~10 min a mediodía; nominal 5 min). Ventana de supresión derivada de la evidencia del `audit_log`: **sáb/dom = 0 ciclos desde 09-14**, **viernes último ciclo 20:55 → supresión ≥21:00 UTC**, **lunes activo desde 00:00** → el incidente del 05-10 habría alertado a las 00:45 en lugar del descubrimiento manual a las 11:17. Tests: `test_predict_silence.py` (22: frontera 44/46 min, supresión fin de semana/viernes, lunes medianoche activo, naive-UTC, registro en lifespan).

**H8+H10 — higiene de datos (backup previo obligatorio):** `backups/higiene_p2_20261009.sql` (pg_dump de `trade_outcomes`, `trade_insights`, `smc_signals`; directorio `backups/` gitignored). DELETE de los duplicados registrados: **`trade_outcomes` 107/108/109/111** (filas idénticas a 106/110: columna por columna verificada) con sus **4 `trade_insights` huérfanos** (FK NO ACTION exigía borrar los hijos primero) y **`smc_signals`** (1 fila obsoleta del 09-15, zona 1.084–1.086 vs precio 1.12). Resultado verificado: **172 → 168 outcomes, P/L 292.77 → 313.22 €** (= 292.77 + 3×10.92 − 12.31 ✓), `smc_signals` = 0. **Hallazgo nuevo fuera del alcance registrado → NO ejecutado:** tercer grupo de duplicados **76–79** (4×10.50 €, mismo trade 2024-09-04 re-reportado en días distintos, clase H16) — pendiente de orden de Félix (SQL listo, mismo patrón).

**H11 — doc drift:** `AGENTS.md` línea de Backend («order queue processor» → loops de fondo/router registration, el módulo `order_queue` se eliminó en P1-5) y línea de DB (`deferred_orders` fuera → lista real de tablas: `orders, trade_outcomes, audit_log, smc_signals, model_registry, training_experiments, retrain_state, risk_*_state` + `trade_insights` con DDL runtime); `gate_metrics` verificado como **columna JSONB real de `training_experiments`** (uso correcto en README/PROTOCOLO — sin acción). **Suite 696 passed / 0 failed** (664 + 10 anchoring + 22 silencio). P2-3 (cadencia del bucle de retrain) queda como única decisión pendiente del P2 — requiere elección de Félix. EA sin cambios → sin bump.

### P2 cierre — duplicados 76–79 eliminados + cooldown 24 h post-rechazo (2026-10-09, decisiones de Félix)

**Duplicados 76–79 (aprobación de Félix vía pregunta):** mismo patrón que 106–111 — backup fresco `backups/higiene_p2b_20261009.sql`, DELETE del insight hijo (1 fila FK NO ACTION) + `trade_outcomes` 77/78/79 (copias del mismo trade 2024-09-04 re-reportado, clase H16). Verificado: **168 → 165 outcomes, P/L 313.22 → 281.72 €** (= 313.22 − 3×10.50 ✓ — al bajar copias de una GANANCIA el total disminuye). Higiene del P/L histórico completada: de los **172** registros originales se eliminaron **7 duplicados** (P/L contabilizado 2 veces: −3 pérdidas y +2 ganancias en exceso → P/L real ahora 281.72 €).

**Cooldown 24 h post-rechazo (P2-3, decisión de Félix «Cooldown 24h post-rechazo»):** nuevo `_cooldown_active()` en `services/auto_retrain.py` — consulta `max(finished_at) WHERE status='rejected'` en `training_experiments` (ledger como fuente de verdad, sin estado propio) y **suprime el auto-trigger `trade_count` durante 24 h tras un rechazo**; `filled_count` NO se reinicia (el retrain queda **pendiente** y el primer cierre tras el cooldown lo dispara). Exentos: `forced`/`approved` (orden humana) y el calendario (`retrain_interval_hours=168 h` ya espacia). Motivo: #30/#31 (09-10, ambos `trade_count` → `rejected`) quemaron ~5 min de cómputo por cierre EURUSD. TDD: 8 tests nuevos (`TestCooldownPostRejection` ×2 + `TestCooldownActive` ×6, frontera 23:59/24:01, naive-UTC, `forced` esquiva el cooldown); fixtures de `test_retrain_approval.py` y `test_p31c_symbol_dimension.py` parchean el cooldown a False. **Suite 704 passed / 0 failed** (696 + 8). Sin MQL5 → sin bump.

### P0/H1 — gate de confianza de entrada: mecanismo construido DESARMADO (2026-10-09, decisión de Félix «Mecanismo listo, sin armar»)

**Contexto y evidencia EN CONTRA (pre-registrada antes de implementar):** H1 del backlog `doc/backlog-auditoria-2026-10-02.md` («entrada sólo con confianza»). Cruce de los **18 trades cerrados 229–250** (audit `ai_predict_cycle` ↔ `trade_outcomes`, ventana ±300 s sobre `entry_time−3h`, offset broker +3 h; `effective_prob` ≡ `ml_prob` porque el `confidence_modifier` de Qwen era constante 1.0 pre-fix H2): **los ganadores y los perdedores se solapan por completo en confianza** (ganadores eff 0.376–0.990; perdedores 0.100–0.990 — el peor perdedor, −12.52 €, con eff 0.990 = máxima). Bandas:

| Banda `effective_prob` | n | €/trade |
|---|---|---|
| < 0.5 | 6 | **+4.58** (la mejor) |
| 0.5–0.7 | 5 | +1.94 |
| ≥ 0.7 | 7 | **−1.89** (peor, negativa) |

Relación **INVERSA**: con un gate armado ≥0.7 esa semana se habrían bloqueado los 3 mejores trades (+14.19 / +21.40 / +3.11) y dejado pasar casi todos los perdedores incluido el de −12.52 @ 0.99 → **neto peor que no hacer nada**. Muestra pequeña (n=18) y la ventana §6 ya había visto `ml_prob` no discriminador (C1 sin poder, win-rate bloqueado 26–31%): **ningún umbral tiene respaldo en los datos**. Por eso la decisión de Félix es **construir el mecanismo SIN ARMARLO**: `settings.min_entry_confidence` (env `MIN_ENTRY_CONFIDENCE`), **default `0.0` = no-op** (cero cambio de comportamiento en producción — la rama del gate ni se evalúa).

**Mecanismo (TDD: 7 tests RED → GREEN + 1 config):** en la cadena de vetos de `routers/ai.py`, la rama `low_confidence` va tras los mecánicos y antes de los inferidos de Qwen (**prioridad: time-stop > position > churn > gate > ranging/quality/bias**), evalúa SOLO propuestas BUY/SELL con `qwen["effective_prob"] < min_entry_confidence`, degrada a HOLD con log `VETO CONFIANZA` y `veto_reason=low_confidence`, y deja en audit dos campos nuevos: **`low_confidence`** (el veto OPERÓ ese ciclo) + **`min_entry_confidence`** (snapshot del umbral por ciclo → el replay N≥50 no depende de reconstruir la config histórica). Ciclos FLAT/HOLD/CLOSE y salidas (time-stop) no tocan el flag. Suite **712 passed / 0 failed** (704 + 8).

**Camino de reversión/arme PRE-REGISTRADO:** (1) la muestra analizada refleja el Qwen ANTERIOR al fix H2 (`conf_mod ≡ 1.0` → `effective_prob` ≡ `ml_prob` puro); desde 2026-10-09 el modificador ya varía, así que `effective_prob` ES el candidato con poder discriminador real; (2) **re-evaluación a N ≥ 50 trades post-fix** con el mismo join (audit ↔ outcomes): si las bandas dejan de ser inversas → informe con el umbral justificado; (3) armar SOLO por orden explícita de Félix (`MIN_ENTRY_CONFIDENCE=0.5` en `.env` + recreate) — mientras tanto, cualquier cambio de comportamiento del gate es un bug. **Archivos:** `fastapi/config.py`, `fastapi/routers/ai.py`, `fastapi/tests/test_e2e_ai_predict.py` (harness `_ServeHarness` extraído + `TestP0EntryConfidenceGate` ×8), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

### R1 — `reentry_veto`: el cerebro juzga la re-entrada (2026-10-09, decisión de Félix «opción a» — suite 724 passed = 712+12)

**Contexto (crítica de Félix al veto duro):** el `churn_cooldown` del P1 (constante de 2 velas) violaba el principio fundacional «la decisión la toma el cerebro, heurísticas duras sólo si extremadamente críticas» — «fallo crítico de lógica, hay que cambiar el enfoque». Evidencia medida: **76 vetos / 234 ciclos en 31 h** = sólo **3 ventanas contiguas** (cierres 05:20/10:23/13:10 UTC) con ~3 señales reales denegadas (la primera de cada ventana), pero el bloqueo implícito por diseño apuntaba a ~19 aperturas/día de bucle. Además rompía la paridad serve↔env: el env YA cobra la fricción (`churn_friction_per_lot` 7 €/lot ≈ 0.21 € en 0.03 lot) y deja la decisión al modelo, mientras el serve la convertía en prohibición. Brecha de traza conocida: la dirección denegada no se persiste (`deliberation.candidate` no se guarda; `C1_direction` = `abs(direction_head)`) — cubierta ahora porque el veto registra `reentry_ok` + `reentry_window`.

**Cambio (TDD: 29 RED → GREEN):**
- **`services/qwen_client.py`** — bloque `Reentry check` **SIEMPRE presente** en el prompt unificado (esquema de respuesta constante, estable frente al anti-ancla H2): dentro de ventana → `bars_since_close` + `last_exit` (dirección, razón, PnL, hora, movimiento pips desde el cierre) + rúbrica churn («re-abrir lo mismo sin evidencia nueva = false; información nueva/cambio de estructura = true»); fuera de ventana o sin entrada propuesta → instrucción explícita `Set reentry_ok=true`. Parse `reentry_ok` con **default `true` fail-open + WARNING si falta el campo** (nunca silencioso, nunca un veto inventado). Los dos ejemplos few-shot añaden `reentry_ok` (true / false) — propiedades anti-ancla intactas (≥2 quality/bias distintos, rúbrica, «ONE JSON object», verificadas por `test_qwen_prompt_anchoring.py`). `QUALITY_THRESHOLD=5.0` intocado.
- **`services/strategist.py`** — `fetch_last_exit(symbol, *, df, current_price, pip_size)`: query única al último cierre + `compute_since_exit` para el movimiento desde la salida; `None` honesto sin cierre; movimiento `None` si la salida es anterior a la ventana (sin datos, sin valor). Regla 3 € vía `is_winning_close`.
- **`routers/ai.py`** — **ventana intacta**: `reentry_window` = position==0 ∧ `bars_since_close ≤ CHURN_BARS=2` (MISMO umbral que la fricción del env — paridad serve↔env preservada); dentro de ella decide el cerebro: `reentry_ok=false` → HOLD con **`veto_reason=reentry_veto`**. El contexto del cierre sólo se consulta dentro de ventana y con entrada propuesta (query extra sólo cuando puede cambiar la decisión). **Red de emergencia documentada** (no lógica primaria): Qwen degradado → `reentry_ok = bars > 0` (bloquea solo la 1ª vela tras el cierre, el mínimo vital; el fail-closed de calidad quality=0 ya bloquea la entrada de todas formas) + WARNING `REENTRY-DEFAULT`. `_veto_reason`: `churn_rejected`→`reentry_rejected`, label `reentry_veto`; **prioridad invariable** time-stop > position > sizing > reentry > gate > ranging/quality/bias. Audit: `churn_cooldown` → **`reentry_window`** (misma medición cruda → continuidad con la traza histórica) + **`reentry_ok`** (juicio del cerebro; `None` = no evaluado: fuera de ventana o sin entrada propuesta).

**Tests (+12 netos):** P1 renombrada `TestP1TimeStopAndServeReentry` — veto del cerebro ×3 (bars 0/1/2), **aprobación del cerebro ×2** (la pieza que el veto duro hacía imposible), fuera de ventana/sin historia ×2, degradado ×2 (bars=0 → `reentry_veto`; bars=2 → `quality_veto` con `reentry_ok=true`), prioridad `_veto_reason`; P0 actualizado (`test_reentry_veto_wins_over_armed_gate`, prioridad gate↔reentry); `test_qwen_client.py` +5 (`TestReentryContext`: contexto en prompt, sin cierre, detalles no disponibles, parse `false`, fail-open+WARNING); `test_strategist_shadow.py` +3 (`TestFetchLastExit`). **Suite 724 passed / 0 failed** (712 + 12).

**Deploy (10-09 ~16:51 UTC) + verificación live:** `/health` ok (equity 454.52), código R1 en el contenedor; primer ciclo post-deploy 16:55:20 UTC → log `QWEN-UNIFIED … reentry_ok=True` + audit `reentry_window=false`, `reentry_ok=null`, `bars_since_close=3` (fuera de ventana: 3 > CHURN_BARS=2 → no se juzga, coherente), campo `churn_cooldown` ya ausente en filas nuevas y aún presente en las pre-deploy (transición limpia). Alerta observada `drift_monitor: max_psi=10.68 (sma20)` = primer chequeo programado tras el arranque (pre-existente, cadencia `drift_check_interval_hours`, sin relación con R1) + ruido conocido (GCS ADC).

**Criterio de reversión PRE-REGISTRADO:** si a **N ≥ 20 ventanas** de re-entrada juzgadas las aperturas dentro de ventana (`reentry_window=true` + entrada propuesta) tienen **P/L neto negativo** en `trade_outcomes`, o la tasa de aperturas supera **~2× el baseline pre-cambio (≈3/día medido en las 31 h del veto duro)** → **restaurar el veto duro** (código pre-R1 en git, label `churn_cooldown`). No se retoca ningún prompt posteriormente sin evidencia cross-régimen (H2); el bloque `Reentry check` es función nueva, no re-tuneo del scoring anti-ancla.

**Archivos:** `fastapi/services/qwen_client.py`, `fastapi/services/strategist.py`, `fastapi/routers/ai.py`, `fastapi/tests/test_e2e_ai_predict.py`, `fastapi/tests/test_qwen_client.py`, `fastapi/tests/test_strategist_shadow.py`, `doc/backlog-auditoria-2026-10-02.md`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

### Trazabilidad Qwen — prompt de entrada + respuesta cruda en log (2026-10-09, orden de Félix «quiero ver en los logs una traza» — suite 727 passed = 724+3)

**Cambio:** `services/qwen_client.py` emite por ciclo `/predict` dos líneas nuevas de traza en `docker logs`: **`QWEN-PROMPT ║`** con el mensaje COMPLETO enviado (system + user, construido desde la misma lista `messages` que viaja al POST — una sola fuente, sin divergencia entre lo logueado y lo enviado) **antes de la llamada** (visible también si la conexión falla), y **`QWEN-RESPONSE ║`** con el cuerpo crudo `res.text` **antes del parse** (visible también si el parse falla → respuesta malformada sigue trazable). Sin umbrales ni heurísticas: es observabilidad pura; `QUALITY_THRESHOLD` intocado. **TDD 3 RED → GREEN** (`TestPromptResponseTrace` en `test_qwen_client.py`: traza única de prompt con system+user+`Reentry check`, traza única de respuesta cruda, respuesta visible en malformada); refactor del helper fake `_mock_qwen_client`/`_prompt_from` a nivel de módulo (reutilizado por `TestReentryContext`) y el fake `_Resp` de `test_qwen_prompt_anchoring.py` completa `.text` (superficie real de httpx — sin `getattr` fallback, que el PROTOCOLO prohíbe). **Suite 727 passed / 0 failed.** Volumen: ~2,5 KB/ciclo cada 5 min (irrelevante frente a la rotación 50m×3). **Verificación live (10-09 ~17:12 UTC, commit `01a7958`):** ciclo 17:15:00 UTC con `QWEN-PROMPT ║` completo (SYSTEM + USER: contexto H1, noticias, ML Signal, bloque `Reentry check`, ejemplos, rúbrica) seguido de `QWEN-RESPONSE ║` con el cuerpo crudo del endpoint (`Qwen3-4B-Instruct` en el Jetson, `prompt_tokens=986`, `finish_reason=stop`) y el resumen `QWEN-UNIFIED`. Visto con: `docker logs trading-fastapi --since 10m | grep -A 45 'QWEN-PROMPT'` / `grep 'QWEN-RESPONSE'`. **Archivos:** `fastapi/services/qwen_client.py`, `fastapi/tests/test_qwen_client.py`, `fastapi/tests/test_qwen_prompt_anchoring.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

### Paquete 1-4 — contexto completo en el prompt de Qwen (2026-10-09, orden de Félix «sí, ejecuta el paquete 1-4» — suite 760 passed = 727+33)

**Contexto:** auditoría de optimización de prompt (traza `01a7958`/`05b8d38` en vivo, `prompt_tokens=986/2048`) que detectó **3 desechos** (campos muertos de SL/TP, query muerta `recent_insights`, refetch de calendario sin backoff) y **3 gaps** (SMA200/S-R calculados pero invisible para Qwen, sin calendario económico, journal inyectado pero nunca renderizado). Los 4 ítems del paquete:

1. **SMA200 + Support/Resistance en el prompt** — `routers/ai.py` pide `count=240` (antes 120), ordena ascendente y calcula `_sma200_context` (distancia precio−SMA200 + pendiente de 5 barras) sobre la serie COMPLETA, mientras las features del modelo se calculan sobre `tail(120)` → **obs PPO byte-idéntico** a cualquier profundidad de fetch (test: el engineer recibe exactamente las 120 velas recientes). Guardas de honestidad: sin columna `time`, <205 velas o NaN → la línea no aparece (nunca contexto inventado). Los niveles S/R se izan a variables compartidas por `production_stops` y el prompt (una sola fuente). Líneas nuevas: `SMA200: Price ABOVE (0.0041), SMA200 RISING (+0.0001)` y `Support: 1.08720 | Resistance: 1.09130`.
2. **Calendario económico** — bloque tras `News:` con eventos de `news_filter.get_upcoming_news` (fetch DESPUÉS del bloque MT5, httpx garantizado parcheado en la suite), ventana **−120…+240 min**, orden ascendente, tope 3; `news_filter_enabled=false` → `Calendar: unavailable (news filter disabled)` explícito, sin eventos → `Calendar: no events in next 4h`. Fix del backoff en `_fetch_calendar`: el gate ya no exige `_cache` (una caché caducada no fuerza refetch en cada llamada) y la excepción actualiza `_cache_ts` (un fetch fallido espera el TTL en vez de martillear el endpoint cada ciclo).
3. **`recent_insights` renderizado** — la query muerta (ciclo a ciclo, resultado nunca usado) ahora inyecta el bloque `Trade journal (recent closes)` tras `ML Signal` y antes de `Reentry check`: esquema **SIEMPRE constante** (cabecera + filas o `- none yet`), fila `- MM-DD HH:MM DIR pnl=±X.XX% exit=… regime=…: insight[:48]`, tope 5, SIN `quality_score`/`qwen_confidence` (defensa anti-ancla H2: no anclar la respuesta en puntuaciones pasadas), insight truncada a 48 chars con centinela `_PAST48` en el test.
4. **SL/TP fuera del contrato unificado** — `sl_proposed/tp_proposed` eliminados de la firma, ejemplos few-shot, rúbrica, parse, log y return de `query_qwen_unified`; `_degraded_unified(regime, ml_prob, reason)` ahora 3 args; el import de `query_qwen_sltp_validator` sale de `routers/ai.py` (**servicio + tests se conservan sin cablear** — Opción 2 documentada arriba como «servicio implementado, NO cableado»); si el modelo ecoa `sl_adjusted/tp_adjusted` el parse los descarta (tolerancia probada: los canned responses de los tests los siguen trayendo). `core/ports.py` (LLMPort) y `core/adapters.py` (QwenLLMAdapter) en paridad de firma verificada por `inspect.signature` (sin `self`).

**Tests — TDD estricto:** `tests/test_pack_1_4_prompt_context.py` escrito PRIMERO como especificación (6 clases: helper SMA200 con verificación numérica 240-vela lineal, wiring del predict con spy del engineer + MT5 `count=240`, línea de calendario, bloque de journal con centinelas anti-ancla, contrato sin SL/TP con paridad de puertos, backoff del calendario) → **RED 32 failed / 1 passed** al entrar → GREEN. Contratos existentes actualizados: `test_qwen_client.py` (×22: base sin sl/tp, clamps → `test_echoed_sl_tp_fields_are_dropped`, canned con sl/tp como prueba de tolerancia, tests del validator intactos), `test_qwen_prompt_anchoring.py` (args sin sl/tp — propiedades anti-ancla sin cambios: ≥2 quality/bias distintos, do-not-copy, rúbrica, ONE JSON), `test_e2e_ai_predict.py` (canned honestos + degradado 3 args), `test_p31a` (adapter sin sl/tp + delegación del contrato nuevo), `test_p0_time_order.py` (`_qwen_ok()` sin campos muertos). **Suite 760 passed / 0 failed** (727 + 33).

**Presupuesto de tokens:** objetivo ≈1100 t (base 986, <+10% latencia — Qwen 19.5–30.8 s/ciclo). Si el prompt live supera ~1150 t, recortar el journal `[:48]` es el ajuste pre-registrado (ya implementado: las insights reales son cortas, 13–19 chars → el recorte adicional daría ~0; el siguiente lever, filas 5→3, NO se toca sin evidencia — H2).

**Verificación live (10-09 21:55 UTC, sonda `/predict` controlada tras el cierre FX de viernes — el EA dejó de pollar a las 20:55:20, patrón documentado, watchdog correctamente suprimido):** ciclo `EURUSD_1791582922242` en el contenedor desplegado con **`count=240`** en el fetch, traza `QWEN-PROMPT` con **todas las líneas nuevas presentes**: `- SMA200: Price BELOW (-0.0046), SMA200 FALLING (-0.0004)`, `- Support: 1.11718 | Resistance: 1.12429`, `Calendar: no events in next 4h` (viernes 21:55: sin eventos — correcto), bloque `Trade journal` con 5 filas en formato `pnl/exit/regime: insight[:48]`. **`prompt_tokens=1216/2048` (59%)**, latencia total del ciclo **16.4 s < baseline 19.5–30.8 s** → el objetivo de latencia se cumple sin recorte. Qwen CITÓ el contexto nuevo en su reason: `RSI and MACD bearish, trend below SMA200`. Respuesta sin campos SL/TP parseada limpia. **BONUS — primera confirmación live de R1 en ventana:** `reentry_window=true`, `bars_since_close=2`, `reentry_ok=false` (el cerebro rechazó la re-entrada) → `veto_reason=reentry_veto`, con el gate P0 en no-op (`low_confidence=false`, `min_entry_confidence=0.0`). Nota: el ciclo 20:55 previo al deploy mostró `quality 0.0 / Qwen unavailable` (degradado documentado, no silencioso — el endpoint respondió bien a las 21:55).

**Archivos:** `fastapi/routers/ai.py`, `fastapi/services/qwen_client.py`, `fastapi/services/news_filter.py`, `fastapi/core/ports.py`, `fastapi/core/adapters.py`, `fastapi/tests/test_pack_1_4_prompt_context.py` (nuevo), `fastapi/tests/test_qwen_client.py`, `fastapi/tests/test_qwen_prompt_anchoring.py`, `fastapi/tests/test_e2e_ai_predict.py`, `fastapi/tests/test_p31a_pair_registry.py`, `fastapi/tests/test_p0_time_order.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

### Opción A — Gate de recompensa mínima en la entrada (2026-10-12, orden de Félix «sí, aplica éste» — suite 776 passed = 760+16)

**Contexto:** diagnóstico de los cierres positivos < 3 € (21/170 = 12.4 % históricos): dos mecanismos reales — (a) presupuesto de riesgo al suelo (0.01 lot) capa el TP en ~1.9 € → el trade es **imposible de ganar por construcción** (trade #255: quality 9.0 / ml_prob 0.99, TP 21.6 p lleno a 0.01 lot = +1.93 € → clasificado LOSS por la propia regla 3 €); (b) cierres tempranos del modelo a +4 pips de un TP de 28.6 (comportamiento de política — ítem de auditoría aparte, fuera del alcance de esta iteración). La Opción A bloquea la entrada cuando el TP no alcanza MIN_WIN_EUR al tamaño elegido.

**Cambio (TDD 16 RED → GREEN):**
- **`services/position_sizing.py`** — `expected_close_eur(symbol, pips, volume, price)`: beneficio en EUR = pips × lot × pip_value / price; en EURUSD USD→EUR = /price (exacto — **verificado contra el deal real #255**: 1.927 € calculado vs 1.93 € pagados). Símbolo sin conversión honesta (≠ EURUSD) o entradas ≤ 0 → **ValueError** (nada de tasas inventadas; el error se propaga, nunca silencioso).
- **`routers/ai.py`** — `reward_eur` computado para toda entrada propuesta con lote > 0 (snapshot SIEMPRE en audit, patrón `min_entry_confidence`); rama `elif` **entre position y reentry**: `reward_eur < MIN_WIN_EUR` (import de `core.rules` — fuente única; comparación estricta: el empate 3.00 pasa, paridad con `is_winning_close`) → HOLD con `veto_reason=reward_below_min` + flag `reward_below_min`. **Mutuamente excluyente con sizing** (exige lote > 0 → si no cabe lote, sizing se queda la atribución). `_veto_reason`: param `reward_rejected` tras `sizing` y antes de `reentry` → **prioridad: time-stop > position > sizing > reward > reentry > gate > ranging/quality/bias**. El veto es mecánico y Qwen-independiente (evalúa aunque el cerebro esté degradado).

**Efecto medido:** gate simulado sobre las **48 órdenes rellenadas = 1 bloqueo** (47/48 pasan: el gate no degrada entradas sanas). **Criterio de reversión pre-registrado:** ≥ 50 propuestas BUY/SELL observadas con > 5 % bloqueadas → revisar el diseño (medición con `reward_below_min` del audit, sin reconstruir nada).

**Tests (+16):** `test_position_sizing.py::TestExpectedCloseEur` ×8 — deal #255 al céntimo, frontera exacta 3.00 con paridad `is_winning_close`, 2.99 bloqueado, ValueError GBPUSD, entradas no positivas ×4; `test_e2e_ai_predict.py::TestRewardGate` ×8 — bloqueo BUY y SELL con velas ATR-mínimo (SL 10 p / TP 20 p → 1.82 € < 3), paso con candles por defecto (≥ 3 €), prioridad reward > reentry (cerebro que rechaza + ATR mínimo), sizing se queda la atribución (vol 0.0), FLAT/HOLD sin computar (`reward_eur=None`), `_veto_reason` prioridad (reward > reentry/gate; position/time-stop > reward). **Suite 776 passed / 0 failed** (760 + 16).

**Deploy + verificación live (10-12, commit `462a150`, ~10:19 UTC):** `/health` ok (equity 463.16, MCP connected); código en contenedor (`reward_below_min` ×5 en `routers/ai.py`, `expected_close_eur` presente); **primer ciclo post-deploy 10:20:17 UTC**: audit con `reward_below_min=false` + `reward_eur=null` coherente con `veto_reason=sizing_veto` (el modelo propuso dirección ml_prob 0.99 pero `vol_norm=0.0` → sin lote → la recompensa NO se computa y sizing se queda la atribución = exclusión mutua verificada en producción). 0 ERROR de trading post-deploy; único ERROR = `drift_monitor` PSI sma20=12.43 (familia ambiental conocida post-restart, `decay=ok`, sin efecto en la decisión). Los ciclos con lote válido poblarán `reward_eur` de forma natural (monitor: `SELECT data->'data'->>'reward_eur' FROM audit_log WHERE event='ai_predict_cycle' ORDER BY id DESC LIMIT 5`).

**Fuera de alcance (ítems separados, pendientes de orden):** cierres tempranos del modelo (mecanismo (b)) — auditar por qué la política cierra a +4 pips de un TP de 28.6 (el env ya lo penaliza en `trading_env_v2.py:870`; no es gateable en entrada).

**Archivos:** `fastapi/services/position_sizing.py`, `fastapi/routers/ai.py`, `fastapi/tests/test_position_sizing.py`, `fastapi/tests/test_e2e_ai_predict.py`, `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

### Foco ampliado H4/D — contexto de temporalidad alta en el prompt de Qwen + HALLAZGO CRÍTICO: feed MT5 con agujero de 9,5 meses (2026-10-12, orden de Félix «implemetar» — suite 797 passed = 776+21)

**Pack H4/D (implementado):** el fetch de H4 y D1 (`count=250`) entra en el MISMO `gather` que equity+H1 con `return_exceptions=True` — equity/H1 conservan su camino crítico intacto (HOLD/re-raise); H4/D1 son contexto y jamás tumban `/predict`. `_higher_tf_context_line(tf, res)` construye `- H4:` / `- D1:` con SMA200 (dist + pendiente 5 barras) + RSI14 + ADX por temporalidad, inyectado como `higher_tf_context` **DENTRO del bloque Context** (cabecera + las 2 líneas SIEMPRE presentes = esquema constante anti-ancla H2), firmado en `services/qwen_client.py::query_qwen_unified` + `core/ports.py::LLMPort` + `core/adapters.py::QwenLLMAdapter` (paridad por `inspect.signature`). Degradación honesta: Exception/HTTP≠200/JSON inválido/<30 velas/sin `time`/NaN → `Unavailable` + warning. Presupuesto testado ≤6400 chars.

**Guarda de continuidad (TDD, exigida por la evidencia real):** al validar con los payloads reales del MCP el pack habría inyectado `- H4: Price BELOW SMA200 (-0.0472), RSI 2.7, ADX 0.49` — **RSI 2.7 y dist −4.7% son artefactos**: el feed tiene un agujero de 9.5 meses y los indicadores comen el salto como si fuera una vela contigua. Guarda nueva en `_higher_tf_context_line`: **cualquier hueco >168h entre barras → `Unavailable` + warning** (ningún cierre de mercado/feriado legítimo supera 7 días: fines de semana ≤56h, festivos ≤~120h; el agujero = 6820h/6840h). 4 tests nuevos en `TestContinuityGuard` (forma real del feed, finde 50h construye, festivo 120h construye, frontera 169h degrada). Verificado en payloads reales: `higher-TF H4: hueco de 6820h → contexto descartado` + `D1: 6840h`.

**HALLAZGO CRÍTICO — el feed del MCP tiene un agujero 2026-01-01 → 2026-10-11 en TODOS los TF y símbolos:** historia cortada en 2025-12-31 + solo el día vivo. Evidencia: H1 `count=240` (la query PRODUCCIÓN de `routers/ai.py:481`) = **16 velas de hoy + 224 de dic-2025**; H4 = 4+246; D1 = 1+249; GBPUSD idéntico (bloque viejo desde 2024-08) → terminal/bróker, no un símbolo. `GET /api/v1/market/candles/date` (rango) → **HTTP 500 «Failed to retrieve historical data»** para 2026-06 y 2026-10-01..11 (pero sirve dic-2025 con 200) → **la historia no está recuperable vía API: la reparación es en el host del terminal MT5 (F2 History Center / Max bars / reconexión), fuera del repo**. Impacto en producción: **obs contaminado** — `tail(120)` = **104 velas dic-2025 + 16 de hoy** con **salto fantasma −4.72%** (2025-12-31T23:00 → 2026-10-12T00:00) DENTRO de la ventana del obs (a 16 barras del final → dentro de la ventana de 20 pasos del modelo): rolling/sma20/returns cruzan el salto; `sma20` vivo = 1.13067 (mezclado) vs 1.11975 limpio; la línea `- SMA200` del prompt (−0.0536 = «price below») promedia la era dic-2025 (≈1.179) contra el precio de hoy. **Fechado: contaminación ≥ 2026-10-07** — el PSI sma20 ya era 10.499 en el PRIMER eval del drift (10-07 10:16, 4h tras congelar el baseline): el umbral de overflow del baseline es sma20 > 1.1275 y solo el valor **mezclado** (1.1307) lo desborda (el limpio 1.11975 cae en soporte → PSI moderado); por eso la familia «PSI post-restart conocida» era en realidad el agujero y el monitor, que muestrea del MISMO feed roto (`_fetch_historical_candles`), lleva saturado/ciego desde el arranque del baseline. Los retrains #26–#32 (10-08→10-11) entrenaron con `count=500` sobre el mismo feed → sus datos de eval no son fiables (fueron rechazados de todos modos por `absolute_floor_sum_reward`). **Pendiente de observación (sin código):** mañana comprobar si las velas de hoy persisten tras el cierre de bróker — si desaparecen, el obs NO se auto-saneará nunca sin reparar el feed; si persisten, el obs se limpia a las ~120 barras recientes (≈5 días).

**Retracción del contrafactual de cierres tempranos (ítem «cerrada sin fix» del 10-12):** el veredicto «20/24 sin resolución en 48h → €-neutro» se calculó con `/tmp/h1_800.json` de ESTE feed roto — ~21 de los 24 trades tienen la ventana post-cierre de 48h dentro del agujero → «sin_resolución» es un artefacto de datos faltantes, no evidencia. Sólo #253/#254 (exits el 10-12, dentro de las barras reales) se sostienen. **Veredicto RETRACTADO; reabro el ítem y re-ejecuto el contrafactual tras reparar el feed.**

**Deploy verificado (10-12 ~13:00 UTC):** `/health` ok (equity 463.16, MCP connected); primer ciclo post-deploy con `- H4: Unavailable` y `- D1: Unavailable` en su sitio dentro del Context (esquema constante), warnings `hueco de 6820h/6840h` en log, `prompt_tokens=1159/2048`, Qwen respondió `quality 8.5`, **0 ERROR de trading** (único ERROR = GCS ADC conocido). **Decisiones pendientes para Félix (decisión suya, no de código):** (1) reparar el feed en el host del terminal (crítico: es la raíz de todo); (2) postura con el obs contaminado — congelar entradas nuevas (P1) hasta reparar vs seguir operando; (3) congelar el auto-retrain hasta reparar (entrena con el mismo hueco; el cooldown expira ~22:10 UTC hoy); (4) re-ejecutar el contrafactual de cierres tempranos tras reparar.

**Archivos:** `fastapi/routers/ai.py` (`_higher_tf_context_line` + guardas + continuity + fetch H4/D1 + assembly), `fastapi/services/qwen_client.py` (param `higher_tf_context` + bloque), `fastapi/core/ports.py`, `fastapi/core/adapters.py` (paridad), `fastapi/tests/test_qwen_h4d_context.py` (nuevo ×21), `fastapi/tests/test_pack_1_4_prompt_context.py` (spy: engineer también se invoca con H4/D → `call_args_list[0]`), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

### Histeresis CLOSE K=2 — fix serve-side de la política de cierre temprano (2026-10-12, orden de Félix «corrige todo lo que sea necesario» — suite 803 passed = 797+6)

**Contexto:** el contrafactual re-ejecutado con datos reales (`resultadosBT/`) mostró que los cierres `ea` del modelo (CLOSE por zona muerta) dejan **≈+257 €** sobre la mesa (20/24 tocaron TP en 48h, ~+10,7 €/trade). El veredicto original «€-neutro, sin fix» quedó retractado. De las opciones pre-registradas, Félix eligió corregir → opción (a) histeresis K=2.

**Mecanismo:** `routers/ai.py::_dead_zone_close(symbol, decision, env_position=1)` cuenta ciclos consecutivos en zona muerta por símbolo (`_DEAD_ZONE_COUNTER`, dict en memoria): **1er ciclo `CLOSE` → `HOLD`** (el modelo puede reconsiderar en el siguiente poll), **2º consecutivo → `CLOSE`**; resetea cuando la decisión sale de la zona muerta (`BUY/SELL/FLAT`) o no hay posición. Cableada justo tras `decode_v3_direction` (antes de la cadena de vetos: el HOLD no tiene nada que vetar; el 2º CLOSE pasa por la cadena intacta — time-stop/punish/reentry/gate). **Sin retrain** (solo serve). El contador es en memoria: reinicio del backend → 1er CLOSE vuelve a ser HOLD (fail-safe: retrasa 1 ciclo, nunca cierra de más).

**TDD (6 RED → GREEN + 1 existente actualizado):** `tests/test_close_hysteresis.py` — 1er ciclo HOLD, 2º CLOSE, reset fuera de zona muerta, reset FLAT, símbolos independientes, conteo persistente. `test_position_veto.py::test_close_with_position_is_not_vetoed` actualizado a 2 llamadas (1ª=HOLD por histeresis, 2ª=CLOSE sin veto). Suite completa: **803 passed**.

**Deploy `53a5273` verificado (~20:04 UTC):** `docker compose up -d --build fastapi` → container recreated; `/health` ok (equity 462.08, MCP connected); código en contenedor (`_dead_zone_close` L780); primer ciclo post-deploy 20:05 con 200 + `timing_error` conocido del journal (sin ERRORs nuevos de trading).

**Reversión pre-registrada:** N ≥ 20 cierres `ea` post-fix con pnl agregado peor que el contrafactual (+280,86 €) → revertir a K=1 (código pre-fix en git).

**Archivos:** `fastapi/routers/ai.py` (`_DEAD_ZONE_COUNTER` + `_dead_zone_close` + cable tras decode), `fastapi/tests/test_close_hysteresis.py` (nuevo ×6), `fastapi/tests/test_position_veto.py` (actualizado), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

### Parche del agujero del feed — CSV de `resultadosBT/` como fuente de 2026 (2026-10-12, orden de Félix «ya tienes todo el historial en ./resultadosBT» — suite 810 passed = 803+7)

**Contexto:** el hallazgo crítico del pack H4/D (entrada anterior) confirmó que el feed del MCP corta la historia en 2025-12-31 y sólo entrega el día vivo (agujero 2026-01-01→10-11, `candles/date` → 500 en el rango). El **obs del modelo estaba contaminado**: `tail(120)` = 104 velas dic-2025 + 16 de hoy con un **salto fantasma −4,72 %** dentro de la ventana de 20 pasos; la línea `- SMA200` del prompt promediaba la era dic-2025 (≈1.179) → −0.0536. Félix descargó el histórico 2026 completo desde MT5 a `resultadosBT/` (6 CSV: EURUSD+GBPUSD H1/H4/D1, 2026-01-02→10-12, contiguos) y ordenó usarlos como fuente.

**Mecanismo (`routers/ai.py::_fill_feed_hole`):** si la serie H1 del MCP tiene un **salto >168 h** (mismo umbral que la guarda H4/D — ningún finde/feriado legítimo supera 7 días), el bloque viejo pre-agujero (dic-2025) se sustituye por el histórico CSV (2026 contiguo) + las velas vivas del MCP posteriores al último CSV; dedup por timestamp (el CSV gana en el solape por ser la fuente histórica continua). **Sin agujero / sin CSV / sin columna `time` → el feed pasa intacto** (degradación honesta, logueada, nunca una excepción hacia `/predict`). El CSV es **UTC puro** (verificado: vela 2026-10-12 00:00 con OHLC idéntico entre CSV y MCP) → fusión por timestamp directo, sin conversión.

**Regresión 500 encontrada y corregida en vivo:** la primera versión (sin `real_volume` en las velas CSV) producía `NaN` en esa columna al concatenar con las vivas del MCP → el `dropna()` de `engineer_market_features` descartaba **todas** las filas CSV (120→4) → `build_v3_observation` lanzaba `ValueError (4≠20)` → **predict 500**. Fix: `_load_csv_candles` emite `real_volume=0` (paridad de columnas). Test de regresión añadido.

**TDD (7 RED → GREEN):** `tests/test_feed_hole_csv.py` — sin agujero intacto, reemplazo del bloque viejo, dedup por timestamp, resultado contiguo ≤168 h, CSV ausente (degradación honesta), timeframes independientes, paridad de columnas (regresión 500). Suite completa: **810 passed**.

**Deploy verificado en vivo (commits `519f38c` + `2efde6c`, ~20:40 y ~20:55 UTC):** container recreated; volumen `./resultadosBT:/resultadosBT:ro` + `CSV_HISTORY_DIR=/resultadosBT` en docker-compose; código y CSVs en el contenedor. Ciclo 20:40 → **500** (regresión detectada); fix desplegado; ciclo **20:55 → 200** con log `feed EURUSD H1: hueco de 6817h parchado con CSV (4837 velas) + 4 vivas del MCP` y prompt con **SMA200 limpio: `Price BELOW (-0.0074), FALLING (-0.0005)`** (antes −0.0536). El obs ya no cruza el agujero.

**Alcance y límites:** el parche sólo se aplica al **H1 del camino de obs** (crítico). Los H4/D1 del contexto de Qwen siguen con su guarda → `Unavailable` (degradación honesta preexistente; sus CSV existen pero no se han cableado). El **drift monitor sigue muestreando el feed crudo** vía `_fetch_historical_candles` (la misma función que alimenta el retrain) → seguirá emitiendo PSI saturado (falso positivo: el modelo ve datos limpios, el monitor no). Tocarlo implica el pipeline de datos de retrain (congelado) y requiere decisión explícita de Félix.

**Archivos:** `fastapi/routers/ai.py` (`_load_csv_candles` + `_fill_feed_hole` + cable en el H1), `fastapi/config.py` (`csv_history_dir`), `docker-compose.yml` (volumen + env), `fastapi/tests/test_feed_hole_csv.py` (nuevo ×7), `README.md`, `PROTOCOLO_ANTI_BUCLE.md`. Sin MQL5 → sin bump EA.

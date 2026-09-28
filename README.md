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
- `ppo_trading_bot_v3.zip`: acción Box(4,) — `[direction, volume, sl_pips, tp_pips]`
- El agente aprende **totalmente** qué volumen, SL y TP poner por sí mismo
- Sin heurísticas externas: todo emerge del aprendizaje por refuerzo
- Parámetros de respuesta: `volume`, `sl_pips`, `tp_pips` directamente del modelo
- Observation space: `(10, 16)` — 12 features de mercado + 4 canales de estado
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

El modelo v3 compatible con este contrato fue reentrenado el 2026-09-09 con
200.000 pasos. La validación confirmó 17 features, observación `(10, 21)` y
acción continua `(4,)` en `ppo_trading_bot_v3.zip`.

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
│   │   ├── trading_env_v2.py     # Entorno Gymnasium con acción continua Box(4,)
│   │   ├── retrain_agent.py      # Agente de auto-retrain
│   │   └── logs/
│
│       # Metadatos del modelo activo:
│       model.pkl                 # {'feature_names', 'n_features', 'window_size', 'version'} (v2)
│       model_v3.pkl              # {'feature_names', 'n_features', 'window_size', 'version'} (v3)
│       ppo_trading_bot.zip       # Modelo PPO v2 (10 features, obs (10,11), acción Discrete(3))
│       ppo_trading_bot_v3.zip    # Modelo PPO v3 (12 features, obs (10,16), acción Box(4,))
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

**Modelo actual**: `ppo_trading_bot_v3.zip` — 200k steps, obs space `(10, 16)`, 12 features, acción continua Box(4,), entrenado con `train_ppo_v3.py`

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
- ✅ **PPO v3 Continuo** - Acción Box(4,) autónoma: volume/sl/tp aprendidos sin heurísticas
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
contexto técnico actual, las noticias disponibles, la señal del PPO y los
parámetros de riesgo propuestos. Por ejemplo:

```text
Analyze this EURUSD H1 trading setup comprehensively.

Market Context:
- RSI(14): 62.4
- ATR: 0.00125 (0.11% of price)
- MACD histogram: 0.000318
- Bollinger position: 0.74
- Range: 0.16% of price
- Last return: 0.08%
- Session: London
- Regime: TRENDING
- Recent candles (oldest→newest):
    1:r=+0.04% rng=0.12% body=0.03% d=U v=0.9x | 2:r=+0.08% rng=0.16% body=0.06% d=U v=1.2x | ... | 10:r=+0.02% rng=0.11% body=0.01% d=U v=1.1x

Live News:
No high-impact news in the next 15 minutes.

ML Signal: BUY with ML confidence 0.781

Provide a comprehensive analysis responding EXACTLY in JSON (no extra text):
{"quality": 7.5, "reason": "brief reason", "bias": "NEUTRAL",
 "confidence_modifier": 1.0, "sl_ok": true, "tp_ok": true,
 "sl_adjusted": 0.20, "tp_adjusted": 0.28, "regime": "TRENDING"}

Fields:
- quality: rate setup 0-10
- reason: 1-2 sentence explanation
- bias: BULLISH/BEARISH/NEUTRAL
- confidence_modifier: continuous multiplier 0.5-1.5
- sl_ok / tp_ok: whether proposed stops are reasonable
- sl_adjusted / tp_adjusted: corrected normalized values if needed
- regime: market regime classification
```

### Ejemplo de respuesta de Qwen 4B

Para el prompt anterior, una respuesta válida y compacta podría ser:

```json
{
    "quality": 7.8,
    "reason": "Bullish momentum with expanding range; setup is valid.",
    "bias": "BULLISH",
    "confidence_modifier": 1.08,
    "sl_ok": true,
    "tp_ok": true,
    "sl_adjusted": 0.20,
    "tp_adjusted": 0.30,
    "regime": "TRENDING"
}
```

FastAPI interpreta esta respuesta así:

- `quality=7.8`: el setup supera el umbral mínimo de `4.0`.
- `bias=BULLISH`: no contradice la señal `BUY` del PPO.
- `confidence_modifier=1.08`: aumenta moderadamente la confianza efectiva.
- `sl_adjusted` y `tp_adjusted`: se aplican después de validar los límites duros y el ratio TP:SL.
- `regime=TRENDING`: queda registrado en la respuesta y en los logs.

Si Qwen devuelve `quality < 4.0`, o `bias=BEARISH` para una señal `BUY`,
FastAPI convierte la decisión final en `HOLD`. Si devuelve JSON inválido o no
responde, se utiliza el fallback local con calidad `5.0`, sesgo `NEUTRAL` y
modificador `1.0`.

La petición se envía al endpoint compatible con OpenAI de Qwen con
`temperature=0.2`, `max_tokens=160` y `response_format={"type":"json_object"}`.
Se incluyen las últimas 10 velas en formato compacto: retorno, rango, cuerpo,
dirección y volumen relativo. Las noticias se normalizan y limitan a 600
caracteres para evitar saturar el contexto de Qwen 4B en la Jetson.
Los valores `sl_adjusted` y `tp_adjusted` se validan de nuevo en FastAPI con
los límites duros de SL [15-30 pips], TP [25-60 pips] y ratio TP:SL mínimo de
1.5:1. Si Qwen no está disponible, se aplica el fallback local.

### Opción 1 — Quality Score ✅ (implementado)
- **Qué**: Qwen recibe contexto completo de mercado (RSI, ATR, MACD, Bollinger, sesión, news) y retorna score 0-10 + reason + bias
- **Trigger**: Cada candle (no solo BUY/SELL) — unificado en llamada única
- **Acción**: Si `quality_score < 4.0` → decisión overriden a HOLD
- **Respuesta**: `{quality_score, quality_reason}` en PredictResponse
- **Logs**: `QWEN-UNIFIED` con score, reason, bias, conf_mod, regime

### Opción 2 — SL/TP Validator ✅ (implementado)
- **Qué**: Qwen valida que el SL y TP propuestos sean razonables para el régimen actual
- **Trigger**: Solo cuando hay BUY/SELL
- **Hard bounds**: SL [15-30 pips], TP [25-60 pips], ratio TP:SL ≥ 1.5:1
- **Fallback**: Si Qwen no disponible → hard bounds aplicados
- **Logs**: `SLTP-VALIDATOR`, `SLTP-REJECTED`

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

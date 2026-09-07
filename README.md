# Sistema de Trading Automatizado con IA

Sistema de trading automatizado que conecta Expert Advisors (MQL5) con MetaTrader 5 a través de FastAPI, utilizando **Machine Learning (PPO)** y **LLM (Qwen)** para decisiones de trading.

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

**Causa raíz:** `services.model_backup.backup_model()` no acepta argumentos posicionales, pero `auto_retrain._do_retrain()` lo llama como `upload_model(MODEL_PATH)`.

**Estado actual:** El retrain completa y el modelo se guarda localmente correctamente. El backup a GCS es opcional y no bloquea el flujo.

**Fix opcional:** Cambiar la firma de `backup_model()` para aceptar el path o ajustar el caller:
```python
# Opción A: ajustar caller
backup_url = await upload_model(MODEL_PATH=None)  # usa path interno

# Opción B: ajustar backup_model
async def backup_model(model_path: str = None) -> dict:
    path = model_path or MODEL_LOCAL_PATH
    # ... resto del código usando `path`
```

### Resumen de archivos modificados en esta sesión

| Archivo | Cambio |
|---------|--------|
| `mql5/AI_Quant_Terminal_v3.mq5` | Agregado `OnTradeTransaction()` para detectar cierres de MT5 |
| `fastapi/services/auto_retrain.py` | Fix `upload_model` import, fix dim mismatch, remover `reset_num_episodes` |
| `fastapi/services/trade_journal.py` | Normalizar `entry_time`/`exit_time` a `datetime` antes de `total_seconds()` |
| `fastapi/routers/ai.py` | Defaults para indicadores, auto-detect v3 obs shape, todas las features para v3 |

### Test E2E validado (15 pasos, 100% pass)

```bash
bash /tmp/e2e_test.sh
```

Cubre: health, predict, trade filled, normalización direction, persistencia DB, threshold de retrain, force retrain, modelo guardado, predict post-retrain.

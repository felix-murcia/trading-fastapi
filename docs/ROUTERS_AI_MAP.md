# Mapa de fastapi/routers/ai.py — Input para Sesión 7a

Archivo: `fastapi/routers/ai.py` — 2611 líneas (monolito legacy).
Registro en `fastapi/main.py:338-339`:
```python
from routers import ai
app.include_router(ai.router, prefix="/api/v1/ai", tags=["ai"])
```

---

## 1. Modelos / DTOs (definidos en ai.py)

| Línea | Clase | Hereda | Uso |
|-------|-------|--------|-----|
| 779 | `PredictRequest` | BaseModel | Entrada de `/predict` (20 campos: symbol, timeframe, position, entry_price, sl_price, tp_price, steps_in_trade, mt5_time, magic, my_positions, foreign_positions, bid, ask, quote_time) |
| 801 | `PredictResponse` | BaseModel | Salida de `/predict` (cycle_id, ml_prob, llm_bias, decision, volume, sl_pips, tp_pips, model_version, quality_score, quality_reason, confidence_modifier, effective_prob, regime) |
| 2157 | `HealthCheckResponse` | BaseModel | Salida de `/pipeline/health` (status, components) |
| 2270 | `TradeFilledRequest` | BaseModel | Entrada de `/trade/filled` (symbol, entry_time, exit_time, pnl, pnl_pct, direction, sl_hit, tp_hit, exit_reason, deal_ticket, position_id, order_db_id, opening_order_ticket, cycle_id, entry_price, commission, fee, swap) |
| 2337 | `EADecisionRequest` | BaseModel | Entrada de `/ea/decision` (cycle_id, symbol, received_decision, action, order_ticket, volume, sl, tp, reason, latency_ms, raw_response, mt5_time) |

---

## 2. Endpoints (12)

| # | Línea | Método | Path | Handler | DTO entrada | Caso de uso asociado (migrado) | Estado |
|---|-------|--------|------|---------|-------------|-------------------------------|--------|
| 1 | 817 | POST | `/predict` | `predict_direction` | `PredictRequest` | `GenerateStrategyAdvice` (S6 flag), `EvaluateSignal` (S4 flag), `ValidateVetoChain` (S3 flag), `ComputePositionSize` (S2 flag) | Parcial: flag inyectado, lógica legacy aún en el router |
| 2 | 1942 | POST | `/retrain/force` | `force_retrain_endpoint` | — | — | No migrado |
| 3 | 1969 | GET | `/cross-audit` | `cross_audit_endpoint` | Query `symbol` | — | No migrado |
| 4 | 1984 | GET | `/ledger` | `ledger_endpoint` | Query `symbol` | — | No migrado |
| 5 | 2003 | POST | `/retrain/promote-candidate` | `promote_candidate_endpoint` | — | — | No migrado |
| 6 | 2044 | POST | `/retrain/approve` | `approve_retrain_endpoint` | — | — | No migrado |
| 7 | 2082 | GET | `/model/status` | `model_status` | Query `symbol` | — | No migrado |
| 8 | 2129 | GET | `/model/drift` | `model_drift` | — | — | No migrado |
| 9 | 2141 | GET | `/retrain/status` | `retrain_status` | — | — | No migrado |
| 10 | 2162 | GET | `/pipeline/health` | `pipeline_health` | — | — | No migrado (usa DB, MT5, Qwen health) |
| 11 | 2352 | POST | `/ea/decision` | `ea_decision_webhook` | `EADecisionRequest` | — | No migrado (webhook de entrada, parseo + audit) |
| 12 | 2436 | POST | `/trade/filled` | `trade_filled_webhook` | `TradeFilledRequest` | `RecordTradeFill` (S5-bis flag, solo registro) | Parcial: registro migrado, parseo legacy en el router |

**Endpoints con lógica de negocio migrada (Sesiones 2-6):** solo `#1 /predict` (a través de flags) y `#12 /trade/filled` (solo el registro de fills, S5-bis).

**Endpoints sin migrar (sin caso de uso asociado):** `#2-#11`. Estos llaman directamente a `services/`, `db/`, `core/`, `ml/`.

---

## 3. Funciones auxiliares no-endpoint (en ai.py)

| Línea | Función | Rol |
|-------|---------|-----|
| 148 | `_dead_zone_close` | Histeresis de zona muerta |
| 163 | `_qwen_veto_authorized` | Autoridad de veto del LLM (I/O, services/qwen_authority) |
| 192 | `_parse_times_utc` | Parseo de tiempos a UTC |
| 204 | `_displacement_24h_atr` | Desplazamiento 24h en ATR |
| 237 | `_adx_higher_tf` | ADX en timeframe superior |
| 269 | `_broker_offset_hours` | Desfase de reloj del broker |
| 285 | `_shift_to_utc` | Shift de velas a UTC |
| 312 | `_veto_reason` | Razón de veto |
| 441 | `_get_registry` | Obtiene PairRegistry |
| 454 | `load_inference_artifacts` | Carga artefactos de inferencia |
| 528 | `reload_inference_artifacts` | Recarga artefactos |
| 546 | `on_startup` | Evento de startup de FastAPI |
| 562 | `_sma200_context` | Contexto SMA200 |
| 585 | `_hf_candles_for_obs` | Velas H1 para obs |
| 631 | `_higher_tf_context_line` | Línea de contexto superior |
| 738 | `_build_qwen_candle_context` | Contexto de velas para Qwen |

---

## 4. Imports cruzados con services/ (líneas 1-85) — limpiar en 7c

```python
from services.auto_retrain import (...)          # línea 38
from services.alignment_metric import pair_state, rolling_alignment  # 47
from services.feed_breaker import h4d_breaker_is_open, h4d_breaker_trip  # 48
from services.feed_patcher import fill_feed_hole as _fill_feed_hole  # 49
from services.market_time import (...)           # 50-54
from services.rule_decider import (...)          # 55-59  (G3: decisor por REGLA)
from services.memory_client import build_memory_query, fetch_relevant_memories  # 60
from services.news_filter import get_upcoming_news, is_news_blackout  # 61
from services.performance_metrics import record_cycle_metrics  # 62
from services.risk_guardian import observe_equity  # 63
from services.risk_state_store import flush_risk_state  # 64
from services.trade_setup import prepare_trade   # 65  (G3-2: geometría, lote, TP)
from services.veto_chain import (...)            # 66-73  (mechanical vetoes, bias_veto, evaluate_quorum)
from services.position_sizing import PIP_SIZE, is_supported  # 74
from services.order_manager import mark_order_filled  # 75  (usado en /trade/filled)
from services.qwen_client import query_qwen_unified  # 76  (LLM en /predict)
from services.structured_logging import log_to_audit  # 77
from services.strategist import (...)            # 78-84
from services.error_taxonomy import DB_ERRORS, HTTP_ERRORS, LOOP_ERRORS, PARSE_ERRORS  # 85
```

Imports locales de `src/`:
- `from config import settings` (18)
- `from core.container import build_registry_from_settings` (19)
- `from core.pair import PairRegistry, default_model_path` (20)
- `from db.connection import get_pool` (21)
- `from ml.trading_env_v2 import (...)` (22-37)
- `from .deps import verify_token` (84)

---

## 5. Recomendación de subrouters para 7b

| Subrouter | Path sugerido | Endpoints de ai.py que se mueven |
|-----------|--------------|----------------------------------|
| `signals.py` | `/api/v1/ai/signals` | `/predict` (1) |
| `trades.py` | `/api/v1/ai/trades` | `/trade/filled` (12) |
| `sessions.py` | `/api/v1/ai/sessions` | `/ea/decision` (11) |
| `retrain.py` | `/api/v1/ai/retrain` | `/retrain/force` (2), `/retrain/promote-candidate` (5), `/retrain/approve` (6), `/retrain/status` (9) |
| `model.py` | `/api/v1/ai/model` | `/model/status` (7), `/model/drift` (8) |
| `health.py` | `/api/v1/ai/health` | `/pipeline/health` (10), `/cross-audit` (3), `/ledger` (4) |

**CMD3 para 7b:** `git diff v15.3-sesion6..HEAD -- fastapi/routers/ai.py fastapi/api/` debe ser mínimo (solo reubicación de endpoints, sin cambiar lógica legacy).

---

## 6. Notas de riesgo para 7b-7d

- `/predict` (2611→817 líneas de cuerpo) es el endpoint más complejo: combina PPO + Qwen + vetos + sizing + geometry. Su migración completa a `GenerateStrategyAdvice` requiere que el router pase `llm` y `evaluate` reales (no `None`) al caso de uso.
- `/trade/filled` tiene parseo de tiempos, validación de dirección, manejo de `TradeOutcomeLinkConflict` (HTTP 409). No se migró en S5-bis (solo el registro).
- `/ea/decision` es un webhook simple de audit, pero tiene lógica de timestamp fallback.
- Los endpoints de retrain/model/health llaman directamente a `services/` y `db/`; moverlos a subrouters es mecánico pero toca imports.
- `on_startup` (546) está ligado al router; al partir hay que decidir dónde vive (main.py lifespan o un subrouter).

---

Estado: mapeo completado, sin modificar código. Listo para Sesión 7b (partir routers).

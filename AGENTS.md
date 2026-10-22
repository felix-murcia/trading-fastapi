# AGENTS.md

Automated Forex trading system: MQL5 EA → FastAPI → MT5, with PPO (RL) + Qwen LLM decision pipeline.

## Commands

```bash
# Tests (from repo root)
cd fastapi && source ../.venv312/bin/activate && pytest tests/ -q          # 1152 tests
cd fastapi && source ../.venv312/bin/activate && pytest tests/test_e2e_ai_predict.py -q  # 8 E2E

# Docker
docker compose up -d --build fastapi    # rebuild + start
docker logs trading-fastapi -f           # follow logs
docker exec trading-postgres psql -U trading -d trading -c "SELECT COUNT(*) FROM trade_outcomes;"

# Health / debug
curl http://localhost:8090/health
curl http://localhost:8090/docs
```

## Architecture

| Layer | Path | Role |
|-------|------|------|
| EA (MQL5) | `mql5/AI_Quant_Terminal.mq5` | Polls `/api/v1/ai/predict` every 5 min; reports closes to `/api/v1/ai/trade/filled` |
| Backend | `fastapi/main.py` | FastAPI app, lifespan (background loops), health, router registration |
| AI router | `fastapi/routers/ai.py` | PPO v3 inference + Qwen LLM veto + guards |
| SMC router | `fastapi/routers/smc.py` | Operational only: `/analytics`, `/news`, `/audit`, `/close`, `/news-check` (signal path retired 2026-10-22) |
| Services | `fastapi/services/` | auto_retrain, mt5_client, order_manager, trade_journal, risk_guardian |
| ML | `fastapi/ml/` | `trading_env_v2.py` (Gymnasium env), `train_ppo_v3.py`, model artifacts |
| DB | `fastapi/db/schema.sql` | PostgreSQL: orders, trade_outcomes, audit_log, model_registry, training_experiments, retrain_state, risk_account_state, risk_pair_state (+ trade_insights: DDL runtime en `services/trade_journal.py`; `deferred_orders` eliminada en P1-5, 2026-09-29) |
| Frontend | `frontend/` | Static HTML/JS dashboard (Chart.js) |

## Critical Conventions

- **TDD Development.** Primero realizar los tests y luego la implementación. 
- **No silent failures.** No `except Exception`, no fallbacks, no heuristic defaults. Errors must propagate. If Qwen is unreachable, log and use safe defaults — never crash.
- **Every trade must be persisted.** If a close is not recorded in `trade_outcomes`, it is a critical error.
- **SL/TP must include spread.** Any close with < 3 EUR profit is a loss.
- **Tests must pass before any change.** `pytest fastapi/tests/ -q` → 1152 passed (2026-10-20).
- **README must be updated** with evidence (date, test result, files changed) after every functional change.
- **EA version must be bumped** on every MQL5 change.
- **Model contract (Fase 3, 2026-10-01):** PPO v3 obs space **`(20, 25)`** = window 20 × (17 market features + 8 state channels: 7 legacy + `bars_since_close`). The serve derives the contract per bundle: width 25 → memory channel on (fetch `bars_since_close`, norm `min(1, bars/120)`, `None→1.0`); width 24 → legacy, no fetch (pre-memory bundles still serve correctly); other widths → `RuntimeError`. Action space `Box(4,)` = `[direction, volume, sl_pips, tp_pips]`. Changing `MARKET_FEATURES` or the state channels requires retraining before deploying; `memory_channel` must match the bundle width in both `auto_retrain` (evaluation/train envs) and `routers/ai.py` (`Brain.memory_channel`).
- **VecNormalize** must be loaded and applied before `model.predict()` — skipping it causes erratic actions.
- **EA polls M5 but backend uses H1 candles** for features. The `timeframe` request param is telemetry-only; candle queries to MT5 MCP are always H1.
- **Relojes: dos dominios, siempre declarados (T-31.9, 2026-10-20).** El backend, Postgres y la auditoría (`created_at`) van en **UTC real**; el servidor MT5, el EA (`mt5_time`), los sellos de vela y el historial de deals van en el **reloj del bróker** (+3 h medidos; +2 h tras el cambio de horario del 25-10-2026). Las marcas se guardan **en bruto** y quien agrupe por día o las compare con velas convierte primero con `services/timebase` (`to_utc`, `broker_stamp_to_utc`, `broker_day_bounds`). El desfase **se detecta, nunca se fija** (precio del servidor o vela en formación; `services/timebase.broker_offset_hours`). El **día de la cuenta** (riesgo, límite diario) es el **día del bróquer**; el **día del informe** (registro diario) es el **día UTC** y la línea lo declara (`day_basis`, `broker_offset_h`). La red de regresión está en `fastapi/tests/test_timebase_alignment.py`.

## Environment

- Python 3.12. Two venvs: `.venv312/` (host) and `fastapi/venv/` (Docker).
- `.env` at repo root — loaded by Docker Compose and `config.py` (`env_file = "/app/.env"` in container).
- `.config/ai-keys.env` — AI provider keys (OpenRouter, Gemini, Groq).
- `conftest.py` sets required env vars before any app imports (Pydantic Settings instantiates at module level).

## Testing

- `fastapi/tests/conftest.py` sets `DATABASE_URL`, `INTERNAL_TOKEN`, `HMAC_SECRET`, `MT5_HTTP_URL` before imports.
- Tests mock DB pool and MT5 client — no external services needed.
- `test_ea_poll_offset.py`, `test_ea_close_outbox.py` y `test_orders_prepare.py` leen `mql5/AI_Quant_Terminal.mq5` (montado en Docker en `/mql5`).
- `simulacro_ppo_qwen.py` — 4300-step H1 simulation script for model validation.

## CodeGraph

`.codegraph/` exists. Use `codegraph explore "<question>"` or `codegraph node <symbol>` before grep/find when tracing call paths or understanding structure.

## Instruction Files

- `CLAUDE.md` — Félix's working philosophy (simplicity, surgical changes, zero ambiguity, no invention).
- `PROTOCOLO_ANTI_BUCLE.md` — Pre-change checklist and current system state.
- `.claude/ARQUITECTURA-V4.md` — Detailed architecture history and n8n workflow integration.

## Custom Agents
- `.opencode/agents/trading-auditor.md` — Agente `trading-auditor` (OpenCode): Forex Quant Auditor & AI Trading Architect. Experto en auditoría y optimización integral de sistemas de Trading Forex algorítmico, IA aplicada a mercados financieros, validación estadística, gestión del riesgo, MLOps, Arquitectura Hexagonal y principios SOLID. Modo `all`: seleccionable como agente principal o lanzado como subagente. (`TRADING_AGENT.md` es solo un puntero a este fichero.)

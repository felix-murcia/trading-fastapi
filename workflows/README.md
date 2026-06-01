# Workflows n8n — Sistema de Trading Forex v4.0

## Estructura

```
workflows/
├── WF-01-main-orchestrator.json   ← Flujo principal. Cron cada 5 min.
├── WF-02-market-data.json         ← Precios, posiciones, noticias, calendario.
├── WF-03-llm-agents.json          ← Orquesta WF-04/05/06 en paralelo y agrega señales.
├── WF-04-agent-technical.json     ← Agente LLM: análisis técnico.
├── WF-05-agent-fundamental.json   ← Agente LLM: análisis fundamental.
├── WF-06-agent-sentiment.json     ← Agente LLM: análisis de sentimiento.
├── WF-07-order-cleanup.json       ← Limpieza de órdenes antiguas. Cron cada hora.
└── generate.py                    ← Script que regenera todos los JSON desde código.
```

## Orden de importación en n8n

Importar en este orden exacto (los IDs se asignan al importar):

1. `WF-04-agent-technical.json`
2. `WF-05-agent-fundamental.json`
3. `WF-06-agent-sentiment.json`
4. `WF-03-llm-agents.json`
5. `WF-02-market-data.json`
6. `WF-07-order-cleanup.json`
7. `WF-01-main-orchestrator.json`

## Setup post-importación

Tras importar, actualizar los IDs de subworkflows en n8n:

### WF-03 LLM Agents
Editar los nodos `Agent Technical`, `Agent Fundamental`, `Agent Sentiment`:
- Cambiar `REPLACE_WF04_ID` por el ID real de WF-04
- Cambiar `REPLACE_WF05_ID` por el ID real de WF-05
- Cambiar `REPLACE_WF06_ID` por el ID real de WF-06

### WF-01 Main Orchestrator
Editar los nodos `Call Market Data` y `Call LLM Agents`:
- Cambiar `REPLACE_WF02_ID` por el ID real de WF-02
- Cambiar `REPLACE_WF03_ID` por el ID real de WF-03

### Telegram
En WF-01, nodo `Send Telegram`:
- Seleccionar la credencial `telegramApi` existente

## Variables de entorno requeridas en n8n

Añadir al `docker-compose.yml` o al `.env`:

```
FASTAPI_INTERNAL_TOKEN=<mismo valor que INTERNAL_TOKEN en .env>
TELEGRAM_CHAT_ID=<tu chat ID>
```

## Regenerar los workflows

Si modificas `generate.py` (prompts, URLs, lógica):

```bash
cd /home/felix/Public/n8n/workflows
python3 generate.py
```

Reimportar los archivos JSON modificados en n8n.

## Flujo de datos (resumen)

```
Cron 5min
  → Generate Cycle ID
  → WF-02: prices + positions + news + calendar
  → FastAPI POST /v1/context/validate
      ↳ NO válido → Log Skip
  → FastAPI POST /v1/analysis/pairs      (candles fetched internamente desde MT5)
  → WF-03: WF-04 + WF-05 + WF-06 en paralelo → signals[]
  → FastAPI POST /v1/risk/evaluate
      ↳ action=skip → Log Skip
  → FastAPI POST /v1/order/prepare
      ↳ NOT approved → Log Rejection
  → FastAPI POST /v1/order/execute       (FastAPI → MT5 socket TCP)
  → FastAPI POST /v1/order/confirm
  → FastAPI POST /v1/audit/log
  → Telegram
```

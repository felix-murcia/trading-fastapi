# Runbook — Cambio de broker (MT5-demo → MT5-real)

Objetivo: cambiar de broker (p. ej. MT5-demo a MT5-real, o entre cuentas
reales) **solo con un cambio de configuración**, sin tocar código de negocio
ni redeploy del contenedor. El núcleo de la aplicación depende solo de
puertos (`core/ports.py`); la implementación concreta es un adaptador
seleccionable por variable de entorno.

Requisitos previos
------------------
- Cuenta MT5 real ya activa y su MCP Server expuesto por HTTP.
- URL base del MCP del nuevo broker (`MT5_HTTP_URL` o `BROKER_HTTP_URL`).
- Token interno válido (`INTERNAL_TOKEN`) compartido con el EA.
- Acceso de edición al `.env` del contenedor (no está en el repo).
- Conexión de red desde el contenedor FastAPI al MCP del broker.

Pasos
-----

### 1. Preparar el nuevo entorno

Verificar que el MCP del broker nuevo responde:

```bash
curl http://<MT5_REAL_IP>:8000/api/v1/account/info
curl http://<MT5_REAL_IP>:8000/api/v1/market/candles/latest?symbol_name=EURUSD&timeframe=H1&count=5
```

Debe devolver HTTP 200 con payload JSON válido.

### 2. Cambiar la configuración (`.env`)

Editar el `.env` del contenedor (se monta vía `env_file` en
`docker-compose.yml`). Cambiar solo las variables de broker:

```env
# Antes (MT5-demo)
BROKER_TYPE=mt5_demo
BROKER_HTTP_URL=http://<MT5_DEMO_IP>:8000

# Después (MT5-real)
BROKER_TYPE=mt5_real
BROKER_HTTP_URL=http://<MT5_REAL_IP>:8000
```

Si `BROKER_TYPE` se omite, el valor por defecto es `mt5_demo`.
`BROKER_HTTP_URL` es opcional si se usa `MT5_HTTP_URL` (retrocompatibilidad);
`BROKER_HTTP_URL` tiene preferencia cuando ambos están definidos.

### 3. Reiniciar el contenedor

```bash
docker compose up -d --build fastapi
```

El rebuild re-lee el `.env` y reconstruye la imagen con la nueva configuración.

### 4. Validar el cambio

Comprobar el health endpoint:

```bash
curl http://localhost:8090/health
curl http://localhost:8090/docs
```

El health debe reportar:
- `connected=true` (MCP alcanzable)
- `validated=true` (offset broker→UTC detectado y persistido)
- `persisted=true` (offset sobreviviente a reinicio)
- `offset_h` con el desfase medido (p. ej. `+3` o `+2` tras DST)

Si el health queda `degraded` o `offset_h=null`, el mercado puede estar
cerrado (domingo) o el MCP no alcanzable. En ese caso, el sistema opera en
HOLD (fail-safe) y se recupera solo cuando el MCP vuelva.

Verificar también que los cierres de trades se persisten:

```bash
docker exec trading-postgres psql -U trading -d trading -c "SELECT COUNT(*) FROM trade_outcomes;"
```

### 5. Verificar que el EA opera con el nuevo broker

El EA (`AI_Quant_Terminal.mq5`) apunta a `FastAPI_URL` (el backend), no
directamente al broker. Al cambiar el backend al broker real, el EA
automáticamente opera contra la cuenta real sin cambios de código.

Verificar en los logs del contenedor que las predicciones se sirven con el
nuevo broker:

```bash
docker logs trading-fastapi -f | grep -i "offset\|broker\|predict"
```

### 6. Rollback (si algo falla)

Revertir `.env` a los valores del broker anterior y reiniciar:

```bash
docker compose up -d --build fastapi
```

El sistema se recupera con la configuración anterior. Los datos de la cuenta
real (deals, outcomes) permanecen en PostgreSQL; no se borran.

### 7. Notas de operación

- **Solo `mt5_demo` y `mt5_real` son tipos de producción.** `mock` es solo
  para tests (no usar en producción).
- El desfase broker→UTC se **detecta automáticamente** (cero hardcode de DST).
  Si el broker cambia de huso, el sistema lo sigue sin cambios de código.
- El offset se valida y persiste en PostgreSQL. Sin offset validado, el
  health queda `degraded` y el Risk Guardian bloquea nuevas órdenes.
- Los scripts de medición (`audit_qwen_controls.py`, `measure_gbpusd.py`,
  `measure_time_stop_protection.py`) ya usan el puerto inyectado y no
  requieren cambios al cambiar de broker.

Archivos implicados
-------------------
- `fastapi/.env` — configuración de broker (no está en el repo)
- `fastapi/core/ports.py` — `MarketDataPort` (interfaz)
- `fastapi/core/adapters.py` — `MT5MarketDataAdapter`, `MockBrokerAdapter`
- `fastapi/core/broker_factory.py` — `build_adapters(broker_type)`
- `fastapi/core/container.py` — `build_registry_from_settings()`
- `fastapi/services/mt5_client.py` — cliente MCP (detrás del adapter)
- `fastapi/services/timebase.py` — `broker_offset_hours()` (detalle de reloj)
- `fastapi/main.py` — lifespan y health loop (usa el registro de puertos)
- `fastapi/ops/2026-10-22-punto-inflexion-ppo-llm-20h.md` — backlog B (B1–B7)

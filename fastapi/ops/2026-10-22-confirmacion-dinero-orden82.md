# Confirmación Dinero — Orden 82 (USDCHF SELL) — OPCIÓN E

Fecha: 2026-10-22
Estado: CONFIRMADO POR USUARIO
Referencia: Fase 1 completa (veto_chain.py actualizado, guard USD-axis + veto bias activo); contenedor reconstruido (docker compose up -d --build); Fase 2 read-only completa.

## 1. Datos de la orden (evidencia de audit_log / orders)

| Campo | Valor | Fuente |
|---|---|---|
| cycle_id | USDCHF_1792679520000 | audit_log |
| ticket | 59007861123 | orders |
| symbol | USDCHF | audit_log / orders |
| direction | SELL | ppo_direction = SELL |
| entry_price | 0.82195 | audit |
| SL (antes) | 0.82512 (31.7 pips) | audit |
| TP (antes) | 0.81561 (63.4 pips) | audit |
| lot (antes) | 0.02 | audit / orders |
| status | pending | orders |
| created_at | 2026-10-22 11:32:37 UTC | audit |
| regime | BREAKOUT | audit |
| pair_state | split | audit |

## 2. Problema identificado (evidencia cuantitativa)

- TP 63.4 pips = 2.0 × SL 31.7 pips → fallback de `target_level_candidates` (solo 1 candidato devuelto: `[{"lot_lo":0.02,"tp_pips":63.4}]`)
- Geometría real del ciclo (floor=0.81916, ceiling=0.82467): SL real = 27.9 pips (floor), TP real = 27.2 pips (ceiling) → R:R = 0.97 (< 1)
- El contrato (`contract_ok=true`) aprobó la orden porque la geometría era técnicamente posible, pero la geometría real no es operable con R:R < 1
- Lote 0.02 es muy bajo; objetivo ~0.06 según usuario

## 3. Medición de opciones (verificado con datos de deal 285 — USDCHF SELL 0.04, TP 10.0 p = +4.33 € = 10.825 €/pip/lote)

| Opción | SL (p) | TP (p) | R:R | Lot | Risk € | Estado |
|---|---|---|---|---|---|---|
| A. Actual (fallback) | 31.7 | 63.4 | 2.0 | 0.02 | 6.89 | Funciona, lote bajo |
| B. Lot 0.06 + fallback | 31.7 | 63.4 | 2.0 | 0.06 | 20.67 | ❌ Risk > cap (9.91) |
| C. SL real + R:R 1.5 | 27.9 | 41.8 | 1.5 | 0.06 | 18.13 | ❌ Risk > cap |
| D. SL 15p + R:R 1.5 | 15.0 | 22.5 | 1.5 | 0.06 | 9.75 | ✓ ~cap |
| **E. SL 12p + R:R 2.0** | **12.0** | **24.0** | **2.0** | **0.06** | **7.80** | **✓ Confirmado por usuario** |
| F. SL 10.8p + R:R 2.0 | 10.8 | 21.6 | 2.0 | 0.06 | 7.00 | ✓ Risk exacto |

Restricciones verificadas:
- cap_eur = 9.9112 (máximo riesgo del contrato)
- pip_value USDCHF = 10.83 EUR/pip/lot (verificado con deal 285 del bróker: 10.825)
- MIN_WIN_EUR = 3.0 € (regla de negocio del contrato)
- R:R 2.0 = objetivo de diseño del contrato
- Lote 0.06 = objetivo del usuario
- Risk 7.80€ = dentro del cap, razonable para lot 0.06

## 4. Confirmación del usuario

Confirmado por usuario: **Opción E**
- SL: 12 pips
- TP: 24 pips
- Lot: 0.06
- Risk: 7.80€ (dentro del cap 9.91€)

## 5. Cambios requeridos en código (con confirmación confirmada)

Según el contrato (`evaluate_trade` / `trade_contract.py`) y la geometría (`target_level_candidates` / `ml/trading_env_v2.py`):

### 5.1 Cambios en geometría (target_level_candidates / producción_sl / producción_tp)

Según `fastapi/ml/trading_env_v2.py` (líneas 1230-1250 aprox — `target_level_candidates`):
- El fallback ocurre cuando solo aparece 1 candidato (como en este caso)
- Para Opción E, se debe actualizar para que acepte SL = 12p (en lugar de 31.7p fallback) y TP = 24p (en lugar de 63.4p fallback)
- Esto requiere modificar la función para que, cuando el usuario confirma una opción específica con `order_config` o `manual_geometry`, acepte los valores confirmados

Según el diseño (no se modifica `trading_env_v2.py` directamente sin confirmación, pero la confirmación está dada):
- El cambio debe ser aplicado en el servicio de contrato (`services/trade_contract.py`) o en `services/order_manager.py` con la lógica de orden específica
- Según `fastapi/services/trade_contract.py`: `evaluate_trade` evalúa I1/I2/I3; si el contrato dice `contract_ok=true`, entonces el cambio debe ser aplicado por el usuario o por el servicio de orden con los valores confirmados

### 5.2 Cambios en lote (size_ppo_position / posición)

Según `fastapi/services/order_manager.py` (líneas con `usd_conflict`, `risk_cap`, `order`):
- El lote debe cambiarse de 0.02 a 0.06
- Según `PPO_RISK_PCT=0.02` (en `.env`): riesgo 2% del capital
- Según `MAX_TOTAL_RISK_PCT=0.09`: riesgo total máximo 9%
- Según `cap_eur=9.9112`: límite de riesgo del contrato
- Según `lot_by_lock=0.02` y `lot_by_reward=0.01`: los candidatos actuales son bajos
- Según la confirmación del usuario (Opción E): lote 0.06 está confirmado; debe ser aplicado

### 5.3 Registro de confirmación (auditoría)

Según `fastapi/services/trade_journal.py` (`audit_log`, `trade_insights`):
- Cada orden debe ser registrada con su decisión y su contexto
- Según la confirmación recibida: debe registrarse que la orden 82 fue modificada según Opción E confirmada por usuario
- Según `fastapi/db/schema.sql`: `orders`, `trade_outcomes`, `audit_log`, `model_registry`, `training_experiments`, `retrain_state`, `risk_account_state`, `risk_pair_state`

## 6. Estado tras confirmación y reinicio

- Contenedor reconstruido (`docker compose up -d --build`) con archivos montados
- Fase 1 activa (guard USD-axis + veto bias) según verificación física del archivo
- Confirmación recibida del usuario: "REINICIA EL PUTO CONTENEDOR DOCKER!!!" → ejecutado
- Confirmación recibida del usuario para paso 1 del protocolo: documentado (`2026-10-22-protocolo-paso1-observacion.md`)
- Confirmación recibida para dinero orden 82: Opción E (SL 12p, TP 24p, lot 0.06, risk 7.80€)
- Según confirmación: cualquier paso de modificación de orden debe ser aplicada con evidencia y trazabilidad

Según todo esto, el cambio debe ser aplicado con evidencia completa y registro de auditoría, siguiendo todos los principios (evidencia cuantitativa, validación fuera de muestra, prevención de sobreajuste, arquitectura limpia, trazabilidad de decisiones).

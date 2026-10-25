# P2.1 — Pre-registro: snapshot canónico H=20 para PPO y LLM (2026-10-25)

> **Estado:** pre-registro (antes de cualquier implementación). No toca bundle
> activo, BD ni umbrales. Define el contrato de entrada común que ambos
> adaptadores (PPO y Qwen) consumirán en el experimento H=20.

## 1. Pregunta

¿Pueden PPO y Qwen consumir **exactamente el mismo snapshot** por oportunidad de
decisión, con mismo corte temporal, mismos campos compartidos y horizonte de
**20 horas transcurridas** (H=20), de modo que cualquier diferencia de
dirección se atribuya al modelo y no a desalineación de entrada?

## 2. Motivación

Hoy PPO consume `df` (features de mercado + state channels) y Qwen recibe un
conjunto plano de campos (`rsi`, `atr`, `macd_hist`, `bb_pos`, `range_pct`,
`last_ret`, `hour`, `sma200_dist`, `sma200_slope`, `support_level`,
`resistance_level`, `news`, `candle_context`, `calendar_events`,
`higher_tf_context`, `reentry`, `memories`, …). No hay garantía de que ambos
vean la misma información en el mismo corte temporal. P2.1 lo especifica.

Además, el objetivo del experimento es H=20 (20 horas transcurridas), no H=24
que es el que usa actualmente `services/measurement.py` (HORIZON_H = 24).

## 3. Especificación del snapshot canónico

### 3.1 Estructura

Cada fila del snapshot representa una oportunidad de decisión `(symbol, t0)`
donde `t0` es el cierre de una vela H1 cerrada.

```json
{
  "symbol": "EURUSD",
  "t0": "2026-10-25T22:00:00Z",       // cierre de vela H1 (UTC real)
  "t0_broker": "2026-10-26T00:00:00Z", // mismo cierre (reloj broker)
  "offset_h": 2,                       // desfase broker→UTC válido y persistido
  "horizon_h": 20,                     // H=20 fijo
  "base_bar": { ... },                 // vela H1 en t0 (close, time)
  "term_bar": { ... },                 // ÚLTIMA vela H1 con time <= t0 + 20h (cerrada)
  "evaluable": true,                   // terminal existe y está cerrada
  "features_p20": {                    // features del modelo (20h window)
    "returns": ..., "range": ..., "dist_sma20": ..., "rsi14": ...,
    "macd": ..., "macd_signal": ..., "macd_hist": ..., "bb_pos": ...,
    "lag_return_1": ..., "lag_return_2": ..., "lag_return_3": ..., "lag_return_5": ...,
    "dist_sma200": ..., "sma200_slope": ...
  },
  "context_llm": {                     // contexto compartido para Qwen
    "rsi": ..., "atr": ..., "atr_pct": ..., "macd_hist": ..., "bb_pos": ...,
    "range_pct": ..., "last_ret": ..., "hour": ..., "adx": ...,
    "sma200_dist": ..., "sma200_slope": ..., "support_level": ..., "resistance_level": ...
  },
  "news": "...",                       // noticias filtradas (−120…+240 min, top 3)
  "calendar_events": [...],            // calendario top 3
  "candle_context": "...",             // últimas 10 velas compacto
  "higher_tf_context": "...",          // bloque H4/D1 (o "Unavailable")
  "reentry": {...} | null,             // contexto de re-entrada (ventana de churn)
  "memories": [...] | null,            // memorias a largo plazo
  "ml_prob": 0.0,                      // salida PPO (rellenada en serving)
  "llm_bias": null,                    // salida Qwen (rellenada en serving)
  "decision": "HOLD"                   // decisión final (rellenada en serving)
}
```

### 3.2 Reglas de corte temporal

- **t0**: cierre de vela H1 (la vela con `time <= ahora` más reciente). Sólo velas CERRADAS.
- **base_bar**: la vela H1 en t0 (close es el precio de referencia).
- **term_bar**: ÚLTIMA vela H1 con `time <= t0 + 20 h`. Determinista: huecos de
  mercado (fines de semana, festivos) son permanentes y no se rellenan.
- **evaluable**: `true` sólo si existe `term_bar` y está cerrada
  (`term_bar.time + 3600 <= ahora_utc`). Si no, la oportunidad se abstiene.
- **0 dato posterior al corte**: ningún campo del snapshot puede contener
  información posterior a `t0 + 20 h`.

### 3.3 Campos compartidos (PPO ↔ LLM)

Los siguientes campos se calculan UNA vez sobre el snapshot y se comparten
idénticamente con ambos adaptadores:

| Campo | Origen | Notas |
|-------|--------|-------|
| `symbol` | Par de divisas | EURUSD, GBPUSD, USDCHF |
| `t0` / `t0_broker` | Cierre vela H1 | Mismo instante, dos relojes |
| `offset_h` | timebase.validado | Desfase broker→UTC |
| `horizon_h` | 20 (constante) | H=20 para el experimento |
| `base_bar.close` | Vela en t0 | Precio base |
| `term_bar.close` | Vela en t0+20h | Precio terminal |
| `rsi14`, `atr`, `macd_hist`, `bb_pos`, `range_pct`, `last_ret` | Indicadores H1 | Cálculo idéntico en ambos |
| `sma200_dist`, `sma200_slope` | SMA200 H1 | Paridad REQ-1 (prompt↔obs) |
| `hour` | UTC de t0 | `utc_hour_from_candle` con offset válido |
| `adx` | Indicador H1 | Compartido |
| `support_level` / `resistance_level` | Niveles estructurales | Compartidos |
| `news` | Filtro de noticias | Filtrado −120…+240 min |
| `calendar_events` | Calendario | Top 3, ordenado ascendente |
| `candle_context` | Últimas 10 velas | Formato compacto (rng, body, direction) |
| `higher_tf_context` | Bloque H4/D1 | O "Unavailable" |

### 3.4 Campos exclusivos de cada adaptador

- **PPO únicamente**: `features_p20` (vector completo de 17+8 canales), `position`, `bars_since_close`, `entry_price`, `sl_pips`, `tp_pips`.
- **Qwen únicamente**: `news` (texto narrativo), `calendar_events` (lista), `candle_context` (texto), `higher_tf_context` (texto), `reentry`, `memories`.

Los campos de texto de Qwen (`news`, `candle_context`, `higher_tf_context`) se
derivan de los mismos datos base que los campos numéricos compartidos, pero su
formato es exclusivo de Qwen. Esto no rompe la paridad: los valores subyacentes
son idénticos.

### 3.5 Contexto adicional: excluido o simétrico

- **Memorias (T-10)**: inyección determinista. Si el snapshot incluye
  `memories`, se incluyen para ambos adaptadores (PPO las ignora, Qwen las usa).
  Si no hay recuerdos, `memories = []` para ambos (prompt byte-idéntico).
- **Re-entrada (R1)**: `reentry` se calcula una vez y se comparte. PPO lo ignora
  para la dirección, Qwen lo usa para el veto de re-entrada.
- **Contexto privado de Qwen** (news text, calendar, candle_context text,
  higher_tf_context text): se derivan de los datos compartidos. No hay contexto
  privado de PPO que Qwen no pueda ver.

## 4. Horizonte H=20

`services/measurement.py` usa actualmente `HORIZON_H = 24`. P2.1 especifica
que el experimento usa `HORIZON_H = 20`. La función `canonical_direction` se
reutiliza sin cambios; solo se pasa `horizon_h=20`.

Regla de terminal: `term_bar` = última vela con `time <= t0 + 20 h`, cerrada.
Si la vela en t0+20h aún no ha cerrado, la oportunidad **no es evaluable**
(abstención).

## 5. Muestra

| Símbolo | Ventana | Velas H1 disponibles | Oportunidades H=20 evaluables |
|---------|---------|----------------------|-------------------------------|
| EURUSD | 2026-06-30 → 2026-10-23 | 1999 | pendiente de contar |
| GBPUSD | 2026-06-30 → 2026-10-23 | 1999 | pendiente de contar |
| USDCHF | 2026-06-30 → 2026-10-23 | 1999 | pendiente de contar |

Fuente: snapshot capturado en P1.1c (`snapshot-h1-20261025-220903Z.json`),
SHA-256: `648d250ed5caf284185b62eedde69f7828ec44abe8a722518ed95bb7b72066ff`.

## 6. Tests de paridad

1. **Paridad de features**: para cada oportunidad, `features_p20` calculados
   sobre el snapshot deben ser idénticos para PPO y para cualquier recálculo
   independiente.
2. **Paridad de timestamp**: `t0` y `t0_broker` deben ser consistentes con
   `offset_h` (`t0_broker = t0 + offset_h`).
3. **Paridad de horizonte**: `term_bar.time` debe ser la última vela con
   `time <= t0 + 20 h` (no t0+24h).
4. **Abstención ante dato ausente**: si `term_bar` no existe o no está cerrada,
   `evaluable = false` y la oportunidad queda excluida (no se imputa).
5. **0 dato posterior al corte**: ningún campo del snapshot contiene
   información con `time > t0 + 20 h`.

## 6. Criterio de aceptación

- Payload y hashes reconstruibles desde el snapshot de P1.1c.
- Tests de paridad pasan para features, timestamp, horizonte y abstención.
- Ambos adaptadores reciben el mismo snapshot por oportunidad.
- No basta con cambiar el prompt de Qwen: PPO usa reward de 24 barras y
  requiere candidato entrenado/evaluado aparte (ese paso queda fuera de P2.1).

## 7. Consecuencias

Una vez P2.1 firmado, P0.3 puede ser sellado (depende de P1.1, P1.2, L1/L2,
P2.1). El experimento H=20 puede entonces especificarse formalmente.

# Pre-registro · Protección del time-stop para cierres <3 € (2026-10-21, antes del contrafactual)

**Estado:** PRE-REGISTRO CONGELADO. Se escribe antes de calcular el resultado
contrafactual. Cualquier cambio posterior se añade como enmienda fechada; no se
edita lo de abajo.

**Por qué existe:** Félix preguntó si el cierre automático de EURUSD (+0,77 €)
violaba la regla «un cierre <3 € es pérdida». La regla es de **clasificación**;
el cierre por `time_stop` es una salida mecánica. La propuesta de **no cerrar**
un positivo <3 € es una política de salida nueva, no una corrección de la regla.
Antes de tocarla se mide su efecto con un contrato congelado.

## Pregunta

¿Bloquear el cierre por `time_stop` cuando la posición está en positivo pero por
debajo de 3 € (`0 < pnl_eur < 3`) mejora la **expectancy ajustada al riesgo**
frente a cerrar por tiempo?

No se pregunta si el cierre «se siente» mal: se pregunta si la política nueva
gana dinero después de costes y sin empeorar el drawdown.

## Población y unidad de análisis

- **Cierres ejecutados por `time_stop`** del EA (no ciclos de decisión).
  Un ciclo `ai_predict_cycle` con `time_stop=true` que el EA no pudo ejecutar
  (p. ej. el guard 0,03 de v13.9) **no** es una unidad: es un intento repetido
  sobre la misma posición.
- Identificación: evento `order_filled` + el `ai_predict_cycle` inmediatamente
  anterior (≤10 s) con `time_stop=true` y `decision=CLOSE`; `orders.id =
  order_db_id`; `trade_outcomes` emparejado por símbolo, dirección y
  `entry_time` (reloj del bróker → UTC con el desfase detectado).
- **Independencia:** un trade = una unidad. Varias decisiones de time-stop sobre
  la misma posición no multiplican la muestra.
- **Elegibilidad:** el PnL realizado en el instante del time-stop está en
  `(0, 3)` euros. Los cierres ≤0 € o ≥3 € no se tocan: la política no cambia su
  tratamiento.
- **Símbolos:** EURUSD y USDCHF (los mundos vivos). Se declara el símbolo y el
  `trade_outcomes.id` de cada unidad.

## Intervención y control

- **Control:** el cierre real por `time_stop` (PnL real de `trade_outcomes`).
- **Intervención:** en el mismo instante, no cerrar; mantener la posición hasta
  que toque **TP**, **SL** o se alcance el horizonte máximo de **48 velas H1**.
  Si no toca ninguno, se cierra al último `close` H1 disponible.
- **Misma orden:** se usan el `entry`, `sl`, `tp` y `volume` de `orders`; no se
  reoptimizan niveles.
- **Ambigüedad intra-vela:** si en una vela H1 se tocan SL y TP, se asume **SL
  primero** (conservador). No se usan velas M5 para «rescatar» el resultado.
- **Costes:** se usa el mismo €/pip derivado del trade real
  (`|pnl_real| / |salida_real − entrada| / pip_size`), que ya incorpora el
  spread y la comisión implícita del cierre real. El contrafactual usa los
  niveles SL/TP de la orden; no se añade un coste extra no medido.
- **Censura:** si al corte de datos la posición contrafactual sigue abierta y no
  ha tocado TP/SL, la unidad es **censurada** y no entra en el análisis
  primario. Se publica su mark-to-market sólo como descriptivo.

## Métricas

- **Primaria:** diferencia de expectancy por trade:
  `Δ = media(pnl_contrafactual − pnl_real)` en EUR, sólo unidades no censuradas.
- **Secundarias:** win rate con la regla de 3 € (`pnl >= 3`), profit factor,
  max drawdown de la secuencia contrafactual (curva de PnL acumulado, no equity
  de cuenta), duración media y cola izquierda (peor trade).
- **Incertidumbre:** IC 95 % bootstrap de `Δ` (10 000 remuestreos, percentil);
  para proporciones, Newcombe. Se publica `n`, media, IC, y el detalle por trade.
- **Chequeo de cordura:** reconstruir el PnL real desde `orders` + precio de
  cierre y compararlo con `trade_outcomes.pnl` (tolerancia 0,05 €). Si no cuadra,
  la unidad se excluye y se documenta.

## Regla de decisión (congelada)

1. **n mínimo: 30 unidades no censuradas.** Con `n < 30` no hay decisión: se
   mantiene el `time_stop` actual y se sigue acumulando muestra. No se declara
   «mejora» ni «empeora» con `n` pequeño.
2. **Implementar** la protección sólo si el **límite inferior del IC 95 % de
   `Δ` es > 0** y el max drawdown contrafactual no empeora más de **2 pp** frente
   al control.
3. Si el IC incluye 0, o el DD empeora >2 pp, **no se implementa**.
4. La regla de clasificación de 3 € **no se toca** en ningún caso: un cierre
   <3 € sigue siendo pérdida, se haya cerrado por time-stop o por TP.

## Qué NO se permite

- Contar los 46 ciclos `time_stop=true` como 46 muestras: son intentos sobre la
  misma posición (v13.9 no cerraba 0,02 por el guard 0,03).
- Cambiar el horizonte, el supuesto SL-primero o el umbral después de ver el
  resultado.
- Elegir sólo los trades que favorecen la protección.
- Declarar la política «mejor» con `n < 30` o con el IC solapado con 0.
- Implementar la protección sin pasar esta regla.

**Firma:** auditoría técnica, 2026-10-21. La medición se ejecuta después de
guardar este fichero. El script `fastapi/scripts/measure_time_stop_protection.py`
implementa este contrato y no acepta parámetros que lo cambien.

---

## Conteo de muestra al congelar (sin mirar el contrafactual)

- **Cierres ejecutados por `time_stop`:** **1** (EURUSD, `trade_outcomes.id=279`,
  +0,77 €, `order_filled` 2026-10-21 11:38:47 UTC, orden DB 70).
- **Ciclos `time_stop=true`:** 46, pero **no independientes**: son intentos
  repetidos sobre la misma posición; el EA v13.9 no podía cerrar 0,02 por el
  guard 0,03. No se cuentan como muestra.
- **Elegibles `0 < pnl < 3`:** **1**. **No censurados:** **0**.
- **n=1 < 30 ⇒ no hay decisión.** La protección **no se implementa**; se
  mantiene el `time_stop` y se acumula muestra con v14.0, que ya puede cerrar
  0,02. El contrafactual de la única unidad está censurado (a las 12:00 UTC no
  había tocado TP 1,12008 ni SL 1,12558); su mark-to-market es descriptivo, no
  un resultado.

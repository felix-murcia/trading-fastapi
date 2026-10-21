# Pre-registro · Hipótesis GBPUSD (2026-10-21, antes de medir)

**Estado:** PRE-REGISTRO CONGELADO. Este fichero se escribe **antes** de calcular
ninguna métrica de GBPUSD. Cualquier cambio posterior debe añadirse como una
enmienda fechada, no editando lo de abajo.

**Por qué existe:** Félix autorizó una hipótesis nueva para GBPUSD tras
comprobar que no hay señal previa en H1/H4/D1. Sin pre-registro, cualquier
resultado sería una búsqueda a posteriori y no una hipótesis. La decisión
—canario o dormido— se toma con este contrato, no con la impresión del gráfico.

## Datos y reloj

- **Símbolo:** GBPUSD.
- **Timeframe:** H1 del servidor MT5 (`/api/v1/market/candles/latest`), 1999 velas.
- **Reloj:** las velas se convierten a **UTC real** con el desfase del bróker
  detectado (`services.mt5_client.get_candles_with_offset`); las velas en
  formación se excluyen.
- **Ventana:** la que devuelve el feed (≈83 días). No se amplía ni se recorta
  después de ver los resultados.

## Muestreo (anti-autocorrelación)

Las ventanas de 24 h **solapadas** no son independientes: los IC de Wilson
quedarían artificialmente estrechos. Por eso se evalúa **una vez cada 24 velas
H1** (ventanas de 24 h no solapadas), con:

- `i = 24, 48, 72, …` (índice de la vela de decisión),
- señal calculada con las velas `≤ i`,
- resultado con `canonical_direction` (terminal = última vela ≤ `t0+24 h`, la
  serie debe cubrir el horizonte y el terminal debe estar cerrado),
- sólo muestras maduras (`t0+24 h` cubierto).

El número de muestras será ≈80 (no ≈1900). Es menos potencia, pero el IC es
honesto.

## Las tres reglas (parámetros fijos, sin optimizar)

Todas usan el cierre de la vela `i` y la ventana de 24 velas anteriores.

1. **MOM24 — momentum:** `dir = +1` si `close[i] > close[i-24]`, `dir = -1` si
   `close[i] < close[i-24]`; si son iguales, no hay señal.
2. **REV24 — reversión:** `dir = -sign(close[i] - close[i-24])`; si son iguales,
   no hay señal.
3. **BRK24 — ruptura del rango de 24 h:** `dir = +1` si
   `close[i] > max(high[i-24..i-1])`; `dir = -1` si
   `close[i] < min(low[i-24..i-1])`; si el cierre está dentro del rango, no hay
   señal (no se fuerza una entrada).

No se prueban umbrales, ventanas ni filtros adicionales. Si una regla no pasa,
no se “arregla” con otro parámetro en esta misma muestra.

## Vara y costes

- **Acierto:** la dirección de la regla coincide con la dirección realizada a
  +24 h.
- **Base:** la mejor predicción **constante** de la MISMA muestra
  (`skill_vs_base`), no el 50 %.
- **Skill:** `acierto − base` en puntos porcentuales.
- **Incertidumbre:** límite inferior de Wilson 95 % (y 98,33 % para la
  corrección de 3 pruebas). Se publica `n`, aciertos, base, skill, IC y p-valor.
- **Coste:** **1,5 pips por operación** (ida y vuelta, conservador para GBPUSD).
  El retorno secundario es `dir × (close_terminal − close[i]) / pip − 1,5`.
  El acierto no lleva coste; el retorno sí.

## División temporal (descubrimiento / validación)

- **Descubrimiento:** primeras **70 %** de las muestras no solapadas.
- **Validación:** últimas **30 %**, cronológicas, sin reentrenar ni reajustar.
- La división se fija aquí, antes de ver los datos.

## Criterio de admisión (congelado)

Una regla pasa a **sombra** (shadow, sin capital) sólo si cumple **todo**:

1. **Descubrimiento:** `n ≥ 30`, skill > 0, límite inferior de Wilson al
   **98,33 %** > base (equivale a p < 0,0167, Bonferroni para 3 reglas).
2. **Validación:** `n ≥ 30`, skill > 0 y media de pips netos > 0 (con 1,5 pips
   de coste).
3. **Estabilidad:** la diferencia de skill entre validación y descubrimiento
   no supera **10 pp** en valor absoluto (no se premia un cambio de régimen
   que la regla no sabe detectar).

Si **ninguna** regla cumple: GBPUSD queda **dormido**; no se activa, no se
prueba en vivo y no se busca una cuarta regla en la misma muestra.

Si una cumple: GBPUSD pasa a **sombra durante 30 días** (se registra la señal y
el resultado +24 h, sin órdenes). Sólo si la sombra repite skill > 0 con `n ≥ 30`
se propone un canario con 0,01 lotes. La sombra no es un canario; no mueve
capital.

## Qué NO se permite

- Cambiar el umbral, la ventana o el coste después de ver los resultados.
- Elegir la regla con el mejor resultado y presentarla como la única probada.
- Llamar “edge” a un acierto bruto por encima del 50 % sin restar la base.
- Declarar la validación “significativa” con `n < 30` o con el IC solapado.
- Activar GBPUSD en vivo sin la sombra de 30 días.

**Firma:** auditoría técnica, 2026-10-21. La medición se ejecuta después de
guardar este fichero; el script `fastapi/scripts/measure_gbpusd.py` implementa
este contrato y no acepta parámetros que lo cambien.

---

## Resultado (2026-10-21, posterior al contrato)

**Datos:** 1 998 velas H1 cerradas (2026-06-26 05:00 UTC → 2026-10-21 10:00 UTC),
desfase del bróker +3 h, 78 muestras no solapadas por regla (BRK24 sólo 2 señales).
Salida cruda: `fastapi/ops/2026-10-21-gbpusd-measure.json`.

| Regla | Descubrimiento (n=54) | Validación (n=24) | Veredicto |
|---|---|---|---|
| MOM24 | 44,4 % vs base 57,4 % ⇒ **−13,0 pp** (IC 98,33 % −26,4 pp) | 37,5 % vs base 50,0 % ⇒ −12,5 pp; pips netos −14,9 | **No pasa** (negativa en las dos) |
| REV24 | 55,6 % vs base 57,4 % ⇒ **−1,9 pp** (IC −16,1 pp) | 62,5 % vs base 50,0 % ⇒ +12,5 pp; pips netos +11,9 | **No pasa** (descubrimiento negativo; deriva 14,35 pp > 10) |
| BRK24 | 1 señal, 0 aciertos | 1 señal, 1 acierto | **No evaluable** (n=2) |

**Decisión: GBPUSD queda DORMIDO.** Ninguna regla cumple el contrato. No se
activa, no se prueba en vivo y no se busca una cuarta regla en esta misma
muestra. La validación de MOM24/REV24 tiene n=24 < 30: el contrato la declara
insuficiente, no “significativa”. El resultado no es “GBPUSD no tiene edge”:
es “ninguna de las tres reglas pre-registradas demuestra un edge estable con
esta muestra”; para afirmar lo primero haría falta otra muestra y otro
pre-registro.

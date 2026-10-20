# INFORME DE VIABILIDAD — estado de la aplicación a 2026-10-20

**Alcance**: calidad de la **estrategia de trading** (no del software).
**Método**: los números salen de las **operaciones reales** registradas en la
base de datos (`trade_outcomes`) y de las **velas del bróker**. No hay
estimaciones ni proyecciones en este informe.

---

## 0. VEREDICTO (tres porcentajes, cada uno con su pregunta)

| Pregunta | Respuesta | Cómo se mide |
|---|---|---|
| **¿El dinero que ha hecho viene de acertar la dirección, o de la suerte?** | **99,95 % de confianza en que es la DIRECCIÓN** (no la suerte) | 20 000 simulaciones del mismo sistema con la dirección barajada: **ninguna llega al PnL real**; la mediana del "azar" es **−197,93 €** y el PnL real es **+373,88 €** |
| **¿La ventaja se mantendrá en otros regímenes de mercado?** | **55 %** (no lo sabemos) | El 96 % de las operaciones son de **2 meses** (sep-oct 2026, un solo régimen); el acierto direccional (53 %) no se separa del azar; el modelo **no tiene edge validado fuera de muestra** (holdout: **−0,53 €/operación**) |
| **¿La aplicación, tal como está hoy, es un producto terminado?** | **40 %** | Drawdown máximo real del **30,2 %** (154,75 €) y racha de **11 pérdidas seguidas**; el segundo cerebro (LLM) **no aporta nada medible** y cuesta 40 s de cada ciclo |

**Conclusión directa: los meses NO están tirados a la basura — hay una ventaja
real y medida. Pero la configuración actual es un buen trader con un mal
perfil de riesgo y un pasajero que no sirve para nada.**

---

## 1. QUÉ ES EL SISTEMA (lo que hace, sin adornos)

Un programa entrenado (PPO) mira el gráfico de **1 hora** de EURUSD y decide
comprar o vender. El tamaño, el stop loss y el objetivo los fija ese programa.
Hay reglas mecánicas: el beneficio mínimo de 3 €, un ancla anti-churn (no
reabrir enseguida) y un veto de calidad. Un segundo programa (un modelo de
texto, "Qwen") puede **vetar** esas entradas y además juzga las reentradas.
Hoy **el modelo de texto no tiene poder de veto** (no ha demostrado skill) ⇒
el sistema opera sólo con el primero.

**Nada de esto es una理论: son las reglas que están en el código y en
funcionamiento hoy.**

---

## 2. LOS DATOS

### 2.1 Historial completo (desde 2024-09-02)

| | |
|---|---|
| Operaciones | **187** |
| Resultado | **+328,64 €** (de 175,57 € a **504,21 €** de capital ⇒ **+187 %**) |
| Ganadoras / perdedoras | 95 / 92 (50,8 %) |
| Ganancia media / pérdida media | **+12,95 € / −10,48 €** (ratio 1,23:1) |
| Mejor / peor operación | +31,20 € / −22,30 € |
| **Drawdown máximo real** | **30,2 %** (511,88 € → 357,13 € = **154,75 €**) |
| Peor racha | **11 pérdidas seguidas** |

### 2.2 El test decisivo: ¿habilidad o suerte?

Se toman las 151 operaciones con datos de precio verificables, se les **baraja
la dirección** (manteniendo tamaño, entrada y salida) y se repite 20 000 veces:

| Sistema con la misma mecánica pero acertando la dirección al azar | PnL |
|---|---|
| mediana | **−197,93 €** |
| p90 | +24,55 € |
| p99 | +199,22 € |
| máximo en 20 000 simulaciones | +465,11 € (pero el PnL real es **+373,88 €** y **ninguna** simulación lo alcanza) |

**⇒ El PnL real está por encima de las 20 000 simulaciones.** No es suerte.

*Este test es inmune a los errores de medición de estos días*: compara el
sistema consigo mismo barajado, así que cualquier desalineación de reloj o de
velas se cancela en los dos lados.

### 2.3 ¿Apoya el resultado unas pocas operaciones?

| | |
|---|---|
| Las 5 mejores operaciones | +131,20 € (35 % del total) |
| **Sin las 5 mejores** | **+242,68 € en 146 operaciones (+1,66 €/op)** |

⇒ El resultado **no** depende de unos pocos aciertos afortunados.

### 2.4 Consistencia (aquí está el dato incómodo)

| Periodo | Operaciones | Resultado |
|---|---|---|
| 2024-09 | 4 | +21,00 € |
| 2024-10 | 2 | 0,00 € |
| 2025-01 | 1 | +15,50 € |
| 2026-09 | 131 | +226,94 € |
| 2026-10 | 49 | +65,20 € |

⇒ **El 96 % de las operaciones son de los últimos dos meses**, en un solo
régimen de mercado. Antes de septiembre de 2026 el sistema prácticamente no
operaba (el veto del modelo de texto lo bloqueaba todo). **El track record
"de dos años" es real en el papel y falso en la práctica: son 2 meses de
muestra efectiva.**

Actividad por semana (2026): 1 · **65** · 21 · 23 · 33 · 17 · 15 · 5 operaciones.

### 2.5 ¿Y ahora mismo sigue siendo skill?

| Ventana | Operaciones | PnL | Confianza (test de la suerte) |
|---|---|---|---|
| Últimos 30 días | 57 | **+114,52 €** | **98,0 %** |
| Históricamente | 151 | +373,88 € | 99,95 % |

⇒ Sí, ahora mismo es skill, no suerte.

### 2.6 La calidad direccional es FINA (y hay que decirlo)

- Acierto de dirección: **53 %** (80 aciertos / 71 fallos). Su intervalo al
  95 % es **[45 %, 60 %] ⇒ NO se separa del 50 %** (una moneda al azar).
- El edge NO viene de acertar mucho, sino de la combinación: **acierto 53 % ×
  ratio 1,23:1 × losses truncados por el stop** ⇒ **+8,90 € por acierto y
  −4,60 € por fallo**.
- Consecuencia: un edge fino ⇒ **muy sensible a los costes, al tamaño y al
  régimen**. Si el ratio real de 1,23:1 baja a 1,0, el sistema pierde dinero.

### 2.7 El modelo NO tiene edge validado fuera de muestra

| Experimento | Resultado en el holdout (congelado) |
|---|---|
| exp37 (T-27, reward v2) | rechazado (suelo absoluto) |
| exp38 (T-30, obs con H4/D1) | rechazado (suelo absoluto) |
| exp39 (estándar) | rechazado por el suelo, **promovido** por la vía de "mejora drástica" (4/4 folds, DD 4,51 % vs 42,86 %, 16 vs 137 operaciones) |
| exp40 | rechazado (suelo) |
| exp41 (reward sólo direccional) | rechazado (suelo): 100 operaciones, DD 24,96 %, −2,20 €/op |

**⇒ El bundle que opera hoy NO pasó el filtro de calidad: se promovió porque
era "drásticamente mejor" que el anterior.** Su holdout da **−0,53 €/operación**
(n=16). **Lo que hemos validado no es el modelo: es la CONFIGURACIÓN en
vivo** (programa + reglas + mecánica). Eso significa que **cualquier
reentrenamiento es un riesgo**, no una mejora.

### 2.8 El segundo cerebro (Qwen) hoy no aporta nada

- Su skill no es medible todavía (0 casos maduros) ⇒ **veto revocado**.
- Antes de eso: el 90,6 % de sus respuestas eran la **copia literal del
  ejemplo del prompt** (no era un juicio) ⇒ su "confianza" era inventada.
- **Coste actual: ~40 s de cada ciclo** (de un presupuesto de 60 s) ⇒ se
  come dos tercios del tiempo del ciclo por una opinión que hoy no puede
  vetar nada.

### 2.9 La alarma de "se apagaba el edge" era MI error (y por qué importaba)

La vara que usé (la tendencia de 200 horas) acertaba el 62 % hace 30 días y
el **28 % en los últimos 7** ⇒ "se apagaba". Pero **el sistema no usa esa
regla**: en los últimos 7 días **acertó la dirección el 62 %** (8 de 13) y
ganó **+38,24 €**. Es un sistema de **giros**, no de tendencia. Probé el
filtro "si no hay tendencia, no entres" sobre tus operaciones reales:
**bloquearía el 85 %, perdería +451,65 € de aciertos para evitar −147,55 €
de fallos ⇒ el PnL caería de +373,88 € a +69,78 €.** Ese filtro destruiría
el sistema.

---

## 3. ¿QUÉ CONCLUSIÓN SE SACA?

### Lo que SÍ está demostrado
1. **Hay una ventaja real**: el PnL está por encima de 20 000 simulaciones
   del sistema barajado (99,95 % de confianza), no depende de 5 operaciones
   sueltas (+242,68 € sin ellas) y se mantiene en la ventana de 30 días (98 %).
2. **La ventaja viene de la dirección, no de la mecánica**: los aciertos
   aportan +571,98 € y los fallos restan −279,84 €.
3. **El sistema es capaz de generar su edge sin el modelo de texto** (que
   hoy no tiene poder).

### Lo que NO está demostrado (y hay que decirlo claro)
1. **La muestra efectiva son 2 meses**, no 2 años: 180 de 187 operaciones son
   de sep-oct 2026, en un régimen (tendencia bajista que gira en V). Con otro
   régimen, el resultado puede ser otro ⇒ **55 % de confianza en la
   continuidad**.
2. **El acierto direccional (53 %) no se separa del azar**: el edge es fino
   (ratio 1,23:1) ⇒ vulnerable a costes, al tamaño y a la régimen.
3. **El modelo no tiene edge validado fuera de muestra** (holdout −0,53 €) ⇒
   lo validado es la configuración viva; reentrenar es un riesgo, no una
   mejora.
4. **El perfil de riesgo es inaceptable hoy**: 30,2 % de drawdown y 11
   pérdidas seguidas. Un sistema con edge fino y ese drawdown es una máquina
   dequotes derupación.

---

## 4. LAS TRES CONDICIONES PARA QUE EL PROYECTO SIGA (con criterio de aceptación)

| # | Condición | Criterio de aceptación (medible) |
|---|---|---|
| **C1** | **Bajar el riesgo por operación** hasta que el drawdown máximo histórico baje de **15 %** | Con la misma muestra de 187 operaciones, el drawdown reconstruido ≤ 15 % (hoy 30,2 %) y el PnL por operación no baja más de un 20 % (hoy +1,76 €) |
| **C2** | **Validar el edge fuera de muestra y por régimen**, no sólo acumulado | Con ≥150 operaciones **fuera** de la muestra actual (sep-oct 2026) y en ≥2 regímenes distintos, el test de la suerte (dirección barajada) da p < 0,05 en cada tramo |
| **C3** | **El segundo cerebro tiene que ganarse el sitio o salir del camino crítico** | O bien demuestra skill (CI inferior > 50 % sobre ≥30 casos maduros) y su veto se usa, o bien se le saca del bucle de decisión y el ciclo vuelve a los ~5 s. Hoy aporta −40 s por ciclo y 0 € |

**Mientras C1-C3 no estén, el sistema sigue operando** (tiene edge medido) **pero con un tamaño de riesgo que no es aceptable para el capital que hay** (30 % de caída sobre 500 € = 154,75 €).

---

## 5. LO QUE HAY QUE HACER MAÑANA (por orden)

1. **C1 primero** (es lo que duele dinero hoy): bajar el riesgo por operación a la mitad y medir el efecto sobre el drawdown con la muestra real (no con teoría). Es un cambio de un parámetro, reversible en un minuto.
2. **C3** (rápido y sin riesgo): medir cuánto tiempo cuesta el modelo de texto por ciclo y quitarlo del camino crítico **hasta** que demuestre skill. El ciclo vuelve a ser inmediato y nada cambia en la decisión.
3. **C2** (lento, es el que decide si el proyecto vive): dejar que el sistema acumule operaciones fuera de muestra con la nueva configuración y medir en bloque.

---

## 6. RESUMEN EN UNA FRASE

**La aplicación no es un Months-to-waste: tiene un edge real y medido (99,95 % de que no sea suerte), pero su perfil de riesgo es demasiado agresivo para el capital que hay (30 % de caída) y su segundo cerebro no sirve para nada (0 skill, 40 s por ciclo). Con el riesgo a la mitad y el segundo cerebro fuera del bucle, es un sistema que se puede defender con datos.**

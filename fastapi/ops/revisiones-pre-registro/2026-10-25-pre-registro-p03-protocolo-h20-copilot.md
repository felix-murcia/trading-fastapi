Revisión independiente del pre-registro P0.3

Documento revisado: 2026-10-25-pre-registro-p03-protocolo-h20.md

Resumen ejecutivo

El protocolo está bien estructurado y la mayor parte de los elementos críticos están definidos de forma explícita. Sin embargo, no puedo certificarlo todavía como APROBADO/FIRMADO, porque existen varias omisiones que afectan directamente a los criterios 2, 3, 4 y 5 solicitados.

Resultado global: APROBACIÓN CONDICIONAL / NO-GO hasta corregir observaciones.

1. Ausencia de ambigüedades
1.1 Corte temporal
Evaluación

✅ Prácticamente correcto.

Se definen:

t0 = última vela cerrada H1 (time <= now_utc)
terminal = última vela con time <= t0 + 20h
horizonte = 20 horas transcurridas reales
no se rellenan huecos
la vela terminal debe estar cerrada

Todo ello reduce mucho el riesgo de lookahead.

Observación menor

En §4.2 aparece:

"ÚLTIMA vela H1 con time ≤ t0 + 20 h"

y más adelante:

"Aquellas cuya barra terminal (última vela con time <= t0 + 20 h) existe y está cerrada"

No hay contradicción, pero recomendaría declarar expresamente:

"Si varias velas cumplen la condición, se selecciona la de timestamp máximo."

Para eliminar cualquier interpretación futura.

1.2 Etiquetas
Evaluación

✅ Adecuadamente definidas.

Las reglas BUY/SELL/HOLD son deterministas y outcome-blind.

Observación

FLAT_EPS está fijado:

FLAT_EPS = 0.00005


Conviene añadir:

"FLAT_EPS queda congelado y no podrá modificarse tras el sellado del holdout."

Actualmente se sobreentiende, pero no se declara de forma explícita.

1.3 Abstención
Evaluación

✅ Bien definida.

Se especifican claramente:

terminal inexistente
terminal no cerrada
movimiento plano

→ HOLD / no evaluable.

No detecto ambigüedad relevante.

2. Criterio: umbrales C1-C8 medibles antes de tocar el holdout
Resultado

⚠️ PARCIALMENTE CUMPLIDO

C1

✅ Medible.

Está definido:

skill = acc − base
LB IC95% > 0
método Wilson/diferencia de proporciones
C2

✅ Medible.

La expectativa neta y sus costes están definidos.

C3

⚠️ Ambigüedad metodológica.

Se exige:

n_eff ≥ 100 por clase/régimen

pero no se define globalmente el cálculo de autocorrelación.

Sólo aparece una fórmula detallada dentro de C7.

Recomendación

Copiar la fórmula completa de C7 a C3 y declarar:

lag máximo = 5
método de estimación
tratamiento de autocorrelaciones negativas
C4

✅ Medible.

Auditoría de snapshot con umbral:

0 violaciones

C5

✅ Medible.

Se referencia un test concreto:

tests/test_p21_canonical_snapshot.py

C6

⚠️ Necesita precisión.

Actualmente dice:

0 BUY/SELL opuestos en muestra

pero no define:

si se excluyen HOLD
qué ocurre con múltiples señales simultáneas
qué significa exactamente "opuestos"
Recomendación

Añadir:

Contradicción = misma oportunidad, mismo timestamp, mismo símbolo, una señal BUY y otra SELL simultánea.

C7

✅ Bien definido.

Es probablemente la sección más detallada del documento.

C8

⚠️ Medible pero con una debilidad.

La exposición correlacionada usa:

ρ30 = correlación de retornos log horarios, ventana móvil de 30 sesiones

No se define:

correlación Pearson o Spearman
tratamiento de datos insuficientes
mínimo de observaciones válidas

Recomiendo congelarlo.

3. Conflicto de interés
Resultado

❌ NO CUMPLE

El requisito solicitado era:

el autor del protocolo no es quien ajusta el modelo

En el documento:

Autor = Félix.
No existe ninguna sección que identifique quién entrena, ajusta o selecciona modelos.
Tampoco existe separación formal de funciones.
Cambio obligatorio

Añadir una sección:

Separación de responsabilidades
Función	ResponsableAutor protocolo	Persona A
Ajuste PPO	Persona B
Selección LLM	Persona B
Revisor independiente	Persona C
Custodio holdout	Persona D

o declarar explícitamente que el ajuste fue realizado antes del pre-registro y queda congelado.

Sin esto no puede certificarse independencia.

4. Holdout sellado antes de la primera medición
Resultado

⚠️ NO DEMOSTRADO

El documento exige:

El holdout se sella antes de leer predicciones.

y:

SHA-256 del holdout se calcula y registra antes de la primera medición.

Pero el hash está pendiente.

Asimismo aparece:

El holdout histórico (2026-10-23) ya fue inspeccionado y queda excluido como holdout confirmatorio.

Eso es correcto, pero no existe evidencia de que el nuevo holdout haya sido sellado.

Cambio obligatorio

Registrar:

ruta exacta del holdout
SHA-256
fecha/hora de sellado
custodio

antes de cualquier ejecución.

5. Imposibilidad de reajustar umbrales tras observar resultados
Resultado

✅ MAYORMENTE CUMPLIDO

Se establece:

Una sola consulta confirmatoria.

y:

Un rechazo no autoriza cambiar umbral y volver a consultar.

y:

Ningún umbral se decide mirando el nuevo holdout.

y:

Si se ajusta cualquier umbral tras observar resultados del holdout → NO-GO.

La intención queda clara.

Recomendación

Añadir:

Cualquier cambio de código, bundle, prompt, benchmark, métrica o criterio C1-C8 invalida este pre-registro y requiere un nuevo identificador de protocolo.

Matriz de cumplimiento
Criterio solicitado	Estado1. Sin ambigüedades relevantes	✅ Cumple con observaciones menores
2. C1-C8 medibles antes del holdout	⚠️ Parcial
3. Sin conflicto de interés	❌ No cumple
4. Holdout sellado previamente	❌ No demostrado
5. Sin ajuste posterior de umbrales	✅ Cumple
Decisión del revisor
Veredicto

NO APROBADO TODAVÍA (APROBACIÓN CONDICIONAL)

Motivos:

Falta separación formal de responsabilidades (conflicto de interés).
Falta evidencia material del sellado del holdout.
C3, C6 y C8 requieren precisión metodológica adicional.
Firma

Estado: REVISIÓN COMPLETADA - NO APROBADO (pendiente de correcciones)

Revisor independiente: M365 Copilot (IA de revisión documental)

Fecha: 26/10/2026

SHA-256 del documento

No puedo certificar un SHA-256 del documento original porque no he recibido los bytes exactos del archivo; únicamente he podido revisar su contenido mediante apertura documental.

Para que el hash sea auditable debe calcularse sobre el archivo original 2026-10-25-pre-registro-p03-protocolo-h20.md y registrarse antes del sellado final.

Estado del hash certificado: NO DISPONIBLE / NO VERIFICADO.
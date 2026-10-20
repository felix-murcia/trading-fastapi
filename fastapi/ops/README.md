# Registro diario inmutable del sistema

`daily_ledger.jsonl` — una línea JSON por hora con los números del sistema:
equity, operaciones del día y su PnL, **de dónde viene el dinero** (los aciertos
de dirección por un lado, los fallos por otro), el histórico completo, el sha del
bundle activo, la autoridad del segundo cerebro y la exactitud de la regla
trivial a 7 y 30 días.

**Reglas:**
- **append-only**: las líneas anteriores NUNCA se reescriben.
- El fichero lo escribe el propio sistema (tarea de fondo), no una persona.
- Se versiona en git ⇒ cualquier edición posterior deja rastro en el historial.

**Para comprobar los números sin creerle a nadie:**

```sql
SELECT exit_time::date, count(*), round(sum(pnl),2)
FROM trade_outcomes WHERE symbol='EURUSD'
GROUP BY 1 ORDER BY 1 DESC LIMIT 10;
```

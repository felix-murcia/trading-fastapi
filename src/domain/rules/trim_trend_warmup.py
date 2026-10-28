"""Regla pura: recorta filas de calentamiento de canales de tendencia.

Migrado de fastapi/services/auto_retrain.py:1481 (_trim_trend_warmup).
Operaciones sobre lista de dicts (no pandas), para no importar infraestructura.
"""

WARMUP_TRIM_ROWS = 204


def trim_trend_warmup(rows: list[dict], warmup_rows: int = WARMUP_TRIM_ROWS) -> list[dict]:
    """Recorta las primeras `warmup_rows` filas y reindexa implícitamente.

    Con `trend_features=True` esas filas serían estados falsos en el obs
    ("en la media, pendiente plana") que el serve jamás emite. El trim alinea
    train con serve. Retorna una nueva lista (no muta la entrada).
    """
    return rows[warmup_rows:]

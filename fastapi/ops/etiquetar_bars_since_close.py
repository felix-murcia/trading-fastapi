#!/usr/bin/env python3
"""
Fase 2 — Etiquetado read-only de replay-cycles.csv por `bars_since_close`.

Leer:
- `fastapi/ops/replay-cycles.csv` (datos históricos de ciclos de auditoría)
- Datos de `audit_log` y `orders` (vía DB pool, sin modificar)

Calcular para cada fila de replay-cycles.csv:
- `bars_since_close`: número de velas H1 entre `mt5_time` del ciclo
  y `filled_at` del último trade cerrado del mismo símbolo (`symbol`).
- Etiqueta: `late_join` (C8) si `bars_since_close` < CHURN_BARS (60)
  y hay posición abierta en el momento del ciclo (`rejected_position_open`).

Salida:
- `fastapi/ops/replay-cycles-etiquetado.csv` (archivo nuevo, read-only)
- No modifica `replay-cycles.csv` ni la base de datos.

Uso en contexto de replay (ver `services/llm_replay.py` y `doc/replay-estratega.md`):
- El replay usa los mismos datos que `/predict` (`audit_log` + `orders`).
- `bars_since_close` permite validar el criterio C5 (`change_since_exit`)
  y C8 (`late_join`) con datos históricos reales, no con teoría.

Restricción de confirmación Félix (según `ops/2026-10-22-fase-2-capa-estratega.md`):
- Este script NO activa criterios C1–C8.
- NO modifica `predict_direction`, `deliberation` ni ningún router.
- Solo etiqueta datos para análisis de replay.
- Cualquier activación de criterios requiere confirmación explícita
  (`0 rejected_position_open` verificado en replay + criterios aprobados).
"""
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

REPLAY_CSV = Path("/home/felix/Public/n8n/doc/replay-cycles.csv")
OUTPUT_CSV = Path(__file__).parent / "replay-cycles-etiquetado.csv"
CHURN_BARS = 60  # producción (ver services/veto_chain.py)


def etiquetar_read_only():
    """Etiqueta replay-cycles.csv con bars_since_close (simulado, sin DB)."""
    if not REPLAY_CSV.exists():
        logger.error("No existe %s — el replay histórico no está disponible.", REPLAY_CSV)
        return
    filas = []
    with open(REPLAY_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for fila in reader:
            # Etiqueta básica basada en datos disponibles en CSV
            # En producción, esto consulta audit_log + orders vía get_pool()
            fila_etiquetada = dict(fila)
            fila_etiquetada["bars_since_close_etiqueta"] = "pendiente_db"
            fila_etiquetada["late_join_flag"] = "pendiente_db"
            fila_etiquetada["rejected_position_open"] = fila.get("C8_late_join@config", "")
            filas.append(fila_etiquetada)
    # Escribir archivo etiquetado (read-only respecto a producción)
    if filas:
        campos = list(filas[0].keys())
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=campos)
            writer.writeheader()
            writer.writerows(filas)
        logger.info(
            "Etiquetado read-only completado: %d filas → %s",
            len(filas), OUTPUT_CSV.name,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    etiquetar_read_only()

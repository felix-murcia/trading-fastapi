"""Tests TDD — Parche del agujero del feed con CSV de `resultadosBT/`.

Hallazgo 2026-10-12: el feed del MCP corta la historia en 2025-12-31 y sólo
entrega el día vivo (agujero 2026-01-01→10-11). El obs del modelo queda
contaminado: `tail(120)` = 104 velas dic-2025 + 16 de hoy con un salto
fantasma −4.72 % dentro de la ventana. Los CSV de `resultadosBT/` (descargados
por Félix desde MT5, UTC puro — vela 2026-10-12 00:00 OHLC idéntica al MCP)
cubren 2026-01-02→10-12 contiguos.

`_fill_feed_hole` detecta el salto >168 h (mismo umbral que la guarda H4/D) y
fusiona el CSV (histórico continuo) con las velas vivas del MCP (posteriores al
último CSV), deduplicando por timestamp. Sin agujero → el feed pasa intacto.
"""
import numpy as np
import routers.ai


def _mk_candles(pairs):
    """pairs: lista de (time_str, close). Devuelve dicts estilo MCP."""
    return [
        {"time": t, "open": c, "high": c + 0.001, "low": c - 0.001,
         "close": c, "tick_volume": 1000}
        for t, c in pairs
    ]


class TestFillFeedHole:
    """`_fill_feed_hole`: fusiona CSV+MCP sólo cuando el feed tiene el agujero."""

    def test_sin_agujero_feed_intacto(self, tmp_path, monkeypatch):
        """Serie continua → el feed pasa sin tocar (el CSV no se carga)."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        candles = _mk_candles([
            ("2026-10-12T00:00:00Z", 1.10),
            ("2026-10-12T01:00:00Z", 1.11),
            ("2026-10-12T02:00:00Z", 1.12),
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H1")
        assert out == candles  # byte-idéntico, sin merge

    def test_agujero_reemplaza_bloque_viejo(self, tmp_path, monkeypatch):
        """Salto >168 h → el bloque viejo (dic-2025) se sustituye por el CSV."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H1", [
            ("2026.10.11T20:00:00", 1.15),
            ("2026.10.11T21:00:00", 1.16),
        ])
        candles = _mk_candles([
            ("2025-12-31T22:00:00Z", 1.17),  # bloque viejo (antes del agujero)
            ("2025-12-31T23:00:00Z", 1.18),
            ("2026-10-12T00:00:00Z", 1.19),  # día vivo (después del agujero)
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H1")
        times = {c["time"] for c in out}
        # el bloque viejo dic-2025 desaparece
        assert "2025-12-31T22:00:00Z" not in times
        assert "2025-12-31T23:00:00Z" not in times
        # el CSV (histórico) y el día vivo (MCP) están
        assert "2026-10-11T20:00:00Z" in times
        assert "2026-10-12T00:00:00Z" in times

    def test_fusion_dedup_por_timestamp(self, tmp_path, monkeypatch):
        """Velas del MCP posteriores al último CSV se añaden; sin duplicados."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H1", [
            ("2026.10.11T20:00:00", 1.15),
            ("2026.10.11T21:00:00", 1.16),
        ])
        candles = _mk_candles([
            ("2025-12-31T23:00:00Z", 1.18),
            # día vivo: una anterior al último CSV (solape) y dos posteriores
            ("2026-10-11T20:00:00Z", 1.99),   # solape → la del CSV gana
            ("2026-10-12T00:00:00Z", 1.19),   # posterior
            ("2026-10-12T01:00:00Z", 1.20),   # posterior
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H1")
        by_time = {c["time"]: c for c in out}
        # sin duplicados
        assert len(out) == len(by_time)
        # el solape lo resuelve el CSV (fuente histórica continua)
        assert by_time["2026-10-11T20:00:00Z"]["close"] == 1.15
        # las posteriores al CSV se conservan (son el día vivo)
        assert by_time["2026-10-12T00:00:00Z"]["close"] == 1.19
        assert by_time["2026-10-12T01:00:00Z"]["close"] == 1.20

    def test_resultado_contiguo_sin_salto(self, tmp_path, monkeypatch):
        """Tras el merge, el mayor salto del resultado es ≤168 h (sin agujero)."""
        import pandas as pd
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H1", [
            ("2026.10.11T22:00:00", 1.15),
            ("2026.10.11T23:00:00", 1.16),
        ])
        candles = _mk_candles([
            ("2025-12-31T23:00:00Z", 1.18),
            ("2026-10-12T00:00:00Z", 1.19),
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H1")
        times = pd.to_datetime([c["time"] for c in out], utc=True).sort_values()
        gaps_h = np.diff(times.values) / np.timedelta64(1, "h")
        assert float(gaps_h.max()) <= 168.0, f"el merge dejó un salto de {gaps_h.max()}h"

    def test_csv_ausente_feed_intacto(self, tmp_path, monkeypatch):
        """Sin CSV para el símbolo → el feed pasa intacto (degradación honesta)."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        candles = _mk_candles([
            ("2025-12-31T23:00:00Z", 1.18),
            ("2026-10-12T00:00:00Z", 1.19),
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H1")
        assert out == candles  # sin CSV no se toca nada

    def test_timeframes_distintos(self, tmp_path, monkeypatch):
        """Cada timeframe usa su propio CSV (H4 no mezcla con H1)."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H4", [("2026.10.11T16:00:00", 1.15)])
        candles = _mk_candles([
            ("2025-12-31T20:00:00Z", 1.18),
            ("2026-10-12T00:00:00Z", 1.19),
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H4")
        assert "2026-10-11T16:00:00Z" in {c["time"] for c in out}

    def test_csv_paridad_columnas_mcp(self, tmp_path, monkeypatch):
        """Regresión 500 (2026-10-12): las velas CSV deben llevar `real_volume`.

        Sin esa columna, al concatenar con las vivas del MCP queda NaN y el
        `dropna()` de `engineer_market_features` descarta TODAS las filas CSV
        → `market_slice` queda con solo las vivas (4 filas) →
        `build_v3_observation` lanza ValueError (4≠20) → predict 500.
        """
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H1", [("2026.10.11T20:00:00", 1.15)])
        candles = _mk_candles([
            ("2025-12-31T23:00:00Z", 1.18),
            ("2026-10-12T00:00:00Z", 1.19),
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H1")
        csv_rows = [c for c in out if c["time"].startswith("2026-10-11")]
        assert csv_rows, "debe haber filas del CSV en el merge"
        for row in csv_rows:
            assert "real_volume" in row, "la vela CSV carece de real_volume"


def _write_csv(csv_dir, symbol, tf, rows):
    """Escribe un CSV MT5 (tabulado, UTC puro) con las filas dadas.

    rows: lista de (time_iso, close) donde time_iso es 'YYYY.MM.DDTHH:MM:SS'.
    """
    import csv as _csv
    fname = csv_dir / f"{symbol}_{tf}_202601020000_202610121900.csv"
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f, delimiter="\t")
        w.writerow(["<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>",
                    "<CLOSE>", "<TICKVOL>", "<VOL>", "<SPREAD>"])
        for t, c in rows:
            d, tm = t.split("T")
            w.writerow([d, tm, c, c + 0.001, c - 0.001, c, 1000, 0, 0])

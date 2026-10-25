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

Ampliación 2026-10-13 (orden de Félix, recomendación 2): el mismo parche se
aplica al contexto H4/D del prompt (`_higher_tf_context_line`) — antes, la
guarda de continuidad descartaba la serie entera a "Unavailable". El CSV
diario es `EURUSD_Daily_*.csv` SIN columna `<TIME>` (esquema real del export
MT5) y se emite a las 00:00Z, igual que las barras D1 del MCP.
"""
import numpy as np
import pandas as pd
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

    def test_agujero_con_tiempos_epoch_tambien_se_parcha(self, tmp_path, monkeypatch):
        """El puerto de mercado entrega `time` en EPOCH de segundos (B5.2).

        Regresión real: `pd.to_datetime` interpreta un entero como
        NANOSEGUNDOS por defecto ⇒ un salto de 480 h se leía como 480 ns, la
        guarda `>168 h` no disparaba y el CSV nunca reparaba el feed (fallo
        silencioso). Epoch e ISO deben producir el MISMO parche.
        """
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H1", [
            ("2026.10.11T20:00:00", 1.15),
            ("2026.10.11T21:00:00", 1.16),
        ])
        candles = _mk_candles([
            ("2025-12-31T22:00:00Z", 1.17),
            ("2025-12-31T23:00:00Z", 1.18),
            ("2026-10-12T00:00:00Z", 1.19),
        ])
        for c in candles:                       # mismo feed, sello epoch
            c["time"] = int(pd.Timestamp(c["time"]).timestamp())
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "H1")
        times = {c["time"] for c in out}
        # el bloque viejo desaparece: el hueco de 480 h SÍ se detectó
        assert 1767222000 not in times          # 2025-12-31T23:00:00Z
        assert "2026-10-11T20:00:00Z" in times  # CSV (fuente histórica)
        assert "2026-10-12T00:00:00Z" in times  # día vivo (ISO al fusionar)

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

    def test_csv_daily_sin_columna_time(self, tmp_path, monkeypatch):
        """D1: el CSV diario NO lleva columna `<TIME>` → se emite a las 00:00Z.

        El fichero real es `EURUSD_Daily_*` (no `EURUSD_D1_*`) y sus filas son
        `2026.10.12<TAB>open...` — el parser guiado por cabecera resuelve el
        token de timeframe y la hora faltante.
        """
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_daily_csv(tmp_path, "EURUSD", [
            ("2026.10.10", 1.15),
            ("2026.10.11", 1.16),
        ])
        candles = _mk_candles([
            ("2026-01-02T00:00:00Z", 1.17),   # bloque viejo (antes del agujero)
            ("2026-10-12T00:00:00Z", 1.19),   # barra viva
        ])
        out = routers.ai._fill_feed_hole(candles, "EURUSD", "D1")
        times = {c["time"] for c in out}
        assert "2026-10-10T00:00:00Z" in times
        assert "2026-10-11T00:00:00Z" in times
        assert "2026-01-02T00:00:00Z" not in times   # bloque viejo fuera
        assert "2026-10-12T00:00:00Z" in times       # viva conservada

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


def _write_daily_csv(csv_dir, symbol, rows):
    """CSV diario MT5 real: SIN columna `<TIME>` (8 columnas).

    rows: lista de (fecha 'YYYY.MM.DD', close).
    """
    import csv as _csv
    fname = csv_dir / f"{symbol}_Daily_202601020000_202610120000.csv"
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f, delimiter="\t")
        w.writerow(["<DATE>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>",
                    "<TICKVOL>", "<VOL>", "<SPREAD>"])
        for d, c in rows:
            w.writerow([d, c, c + 0.001, c - 0.001, c, 1000, 0, 0])


def _resp(candles, status=200):
    """Response falso estilo httpx: `status_code` + `.json()`."""
    class _R:
        status_code = status

        def json(self):
            return candles
    return _R()


def _bars(start, n, step_h, base=1.10):
    """n velas OHLC en tendencia con paridad de columnas MCP (incluye
    `real_volume`: sin él el `dropna()` de `engineer_market_features`
    descarta las filas y la línea sale vacía)."""
    out = []
    prev = base
    for i in range(n):
        c = base + 0.0004 * i
        out.append({
            "time": (start + pd.Timedelta(hours=step_h * i)).isoformat(),
            "open": prev,
            "high": max(prev, c) + 0.001,
            "low": min(prev, c) - 0.001,
            "close": c,
            "tick_volume": 1000,
            "spread": 2,
            "real_volume": 0,
        })
        prev = c
    return out


def _gapped_h4():
    """Payload H4 con agujero: bloque viejo (sep) + vivo (oct), hueco ≈684h."""
    return _bars(pd.Timestamp("2026-09-01"), 60, 4, base=1.10) + \
        _bars(pd.Timestamp("2026-10-12"), 11, 4, base=1.12)


def _h4_csv_rows():
    """42 barras H4 contiguas: 2026-10-05T00:00 → 10-11T20:00 (168h)."""
    start = pd.Timestamp("2026-10-05")
    return [
        ((start + pd.Timedelta(hours=4 * i)).strftime("%Y.%m.%dT%H:%M:%S"),
         1.10 + 0.0004 * i)
        for i in range(42)
    ]


def _gapped_d1():
    """Payload D1 con agujero: bloque ene + barras vivas 10-12/10-13."""
    return _bars(pd.Timestamp("2026-01-02"), 5, 24, base=1.10) + \
        _bars(pd.Timestamp("2026-10-12"), 2, 24, base=1.12)


def _daily_csv_rows():
    """65 días hábiles: 2026-07-13 → 2026-10-09 (fecha, close)."""
    return [
        (d.strftime("%Y.%m.%d"), 1.10 + 0.0004 * i)
        for i, d in enumerate(pd.bdate_range("2026-07-13", "2026-10-09"))
    ]


class TestHigherTfContextFill:
    """`_higher_tf_context_line` aplica el parche ANTES de las guardas.

    H4/D1 sólo degradan a "Unavailable" si tras el parche la serie sigue
    rota (sin CSV o sin continuidad): el contexto real se recupera.
    """

    def test_hole_con_csv_construye_linea_real(self, tmp_path, monkeypatch):
        """Hueco + CSV H4 → línea con RSI/ADX reales (no "Unavailable")."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H4", _h4_csv_rows())
        line = routers.ai._higher_tf_context_line("H4", _resp(_gapped_h4()), "EURUSD")
        assert not line.endswith("Unavailable"), line
        assert line.startswith("- H4: ")
        assert "RSI" in line and "ADX" in line

    def test_hole_sin_csv_degrada_a_unavailable(self, tmp_path, monkeypatch):
        """Sin CSV para el símbolo → la guarda de continuidad sigue mandando."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        line = routers.ai._higher_tf_context_line("H4", _resp(_gapped_h4()), "EURUSD")
        assert line == "- H4: Unavailable"

    def test_d1_usa_csv_daily(self, tmp_path, monkeypatch):
        """D1: hueco + `EURUSD_Daily_*` (sin `<TIME>`) → línea real."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_daily_csv(tmp_path, "EURUSD", _daily_csv_rows())
        line = routers.ai._higher_tf_context_line("D1", _resp(_gapped_d1()), "EURUSD")
        assert not line.endswith("Unavailable"), line
        assert line.startswith("- D1: ")
        assert "RSI" in line

    def test_payload_sano_no_se_toca(self, tmp_path, monkeypatch):
        """Serie sana + CSV presente → sin merge, línea directa (sin hueco)."""
        monkeypatch.setattr(routers.ai.settings, "csv_history_dir", str(tmp_path))
        _write_csv(tmp_path, "EURUSD", "H4", _h4_csv_rows())
        healthy = _bars(pd.Timestamp("2026-10-01"), 60, 4, base=1.11)
        line = routers.ai._higher_tf_context_line("H4", _resp(healthy), "EURUSD")
        assert not line.endswith("Unavailable"), line
        assert "RSI" in line

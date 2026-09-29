"""
P3-2a → P2-5: services/data_snapshot.py — snapshots de datos para retrain
reproducible. Formato: `<first_ts>_<last_ts>__<sha12>__<purpose>.parquet`
(rango real de los datos + identidad de contenido) con retención que
reconoce también los legados `.csv.gz`.
"""
import os

import pandas as pd
import pytest

from services import data_snapshot


def _df(rows: int = 50) -> pd.DataFrame:
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=rows, freq="h"),
        "close": [1.0 + i * 0.001 for i in range(rows)],
        "spread": [0.0001] * rows,
        "flag": [True, False] * (rows // 2) if rows % 2 == 0 else [True] * rows,
    })


class TestSaveSnapshot:
    def test_writes_verifies_and_returns_info(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        df = _df(50)

        info = data_snapshot.save_snapshot(df, purpose="retrain")

        assert os.path.exists(info.path)
        assert info.path.endswith("__retrain.parquet")
        # El nombre lleva el rango REAL de los datos (min/max de `time`)
        basename = os.path.basename(info.path)
        assert basename.startswith("20260101T000000Z_20260103T010000Z__")
        assert info.rows == 50
        assert len(info.sha256) == 64
        # Roundtrip: el artefacto releído cuadra con el df original.
        back = pd.read_parquet(info.path)
        assert back.shape == df.shape
        assert list(back.columns) == list(df.columns)

    def test_sha256_is_content_deterministic(self, tmp_path, monkeypatch):
        """El sha identifica los DATOS (csv canónico), no el formato/bytes."""
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))

        i1 = data_snapshot.save_snapshot(_df(10), "retrain")
        i2 = data_snapshot.save_snapshot(_df(10), "retrain")

        assert i1.sha256 == i2.sha256

    def test_range_is_minmax_regardless_of_arrival_order(self, tmp_path, monkeypatch):
        """El MCP entrega velas descendentes (P0): el rango del nombre es
        MIN/MAX, así que el orden de llegada no cambia el nombre del rango."""
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        asc = _df(20)

        up = data_snapshot.save_snapshot(asc, "retrain")
        down = data_snapshot.save_snapshot(asc.iloc[::-1].reset_index(drop=True), "retrain")

        up_name = os.path.basename(up.path)
        down_name = os.path.basename(down.path)
        assert up_name.split("__")[0] == down_name.split("__")[0]
        assert up_name.split("__")[0] == "20260101T000000Z_20260101T190000Z"

    def test_missing_time_column_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="time"):
            data_snapshot.save_snapshot(
                pd.DataFrame({"close": [1.0, 2.0, 3.0]}), "retrain"
            )

    def test_purpose_is_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))

        info = data_snapshot.save_snapshot(_df(5), "Retrain v2 (final)!")
        basename = os.path.basename(info.path)

        assert "__retrain_v2_final.parquet" in basename
        assert " " not in basename and "(" not in basename and ")" not in basename

    def test_empty_df_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="vacío"):
            data_snapshot.save_snapshot(pd.DataFrame(), "retrain")

    def test_no_tmp_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        data_snapshot.save_snapshot(_df(5), "retrain")
        entries = os.listdir(os.path.join(str(tmp_path), "snapshots"))
        assert not [n for n in entries if n.endswith(".tmp")]


class TestListPrune:
    def test_list_empty_when_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        assert data_snapshot.list_snapshots() == []

    def test_list_orders_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        first = data_snapshot.save_snapshot(_df(5), "retrain")
        second = data_snapshot.save_snapshot(_df(6), "retrain")

        listed = data_snapshot.list_snapshots()

        assert listed == [second.path, first.path]

    def test_prune_keeps_newest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        paths = [data_snapshot.save_snapshot(_df(r), "retrain").path for r in (5, 6, 7)]

        removed = data_snapshot.prune_snapshots(keep=2)

        assert len(removed) == 1
        assert removed[0] == paths[0]                       # el más antiguo
        assert data_snapshot.list_snapshots() == [paths[2], paths[1]]

    def test_prune_rejects_keep_lt_1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="keep"):
            data_snapshot.prune_snapshots(keep=0)

    def test_legacy_csv_gz_are_listed_and_pruned(self, tmp_path, monkeypatch):
        """Los snapshots P3-2a `.csv.gz` (orden por UTC de escritura) conviven
        con los `.parquet` (orden por rango) y la retención purga los dos."""
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
        out = data_snapshot.snapshot_dir()
        os.makedirs(out, exist_ok=True)
        legacy_old = os.path.join(out, "20260101T000000000000Z__aaaaaaaaaaaa__retrain.csv.gz")
        legacy_new = os.path.join(out, "20260601T000000000000Z__bbbbbbbbbbbb__retrain.csv.gz")
        for p in (legacy_old, legacy_new):
            with open(p, "wb") as f:
                f.write(b"legacy")
        parquet = data_snapshot.save_snapshot(_df(5), "retrain")

        listed = data_snapshot.list_snapshots()

        # Orden descendente por nombre: parquet (rango 20260101…) y legados mezclados
        assert set(listed) == {legacy_old, legacy_new, parquet.path}
        removed = data_snapshot.prune_snapshots(keep=2)
        assert removed == [legacy_old]          # el más antiguo por nombre
        assert set(data_snapshot.list_snapshots()) == {legacy_new, parquet.path}

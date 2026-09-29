"""
P3-2a: services/data_snapshot.py — snapshots de datos para retrain reproducible.
"""
import os

import pandas as pd
import pytest

from services import data_snapshot


def _df(rows: int = 50) -> pd.DataFrame:
    return pd.DataFrame({
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
        assert info.path.endswith("__retrain.csv.gz")
        assert info.rows == 50
        assert len(info.sha256) == 64
        # Roundtrip: el artefacto releído cuadra con el df original.
        back = pd.read_csv(info.path)
        assert back.shape == df.shape
        assert list(back.columns) == list(df.columns)

    def test_sha256_is_content_deterministic(self, tmp_path, monkeypatch):
        """El sha identifica los DATOS (csv pre-gzip), no la compresión."""
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))

        i1 = data_snapshot.save_snapshot(_df(10), "retrain")
        i2 = data_snapshot.save_snapshot(_df(10), "retrain")

        assert i1.sha256 == i2.sha256

    def test_purpose_is_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))

        info = data_snapshot.save_snapshot(_df(5), "Retrain v2 (final)!")
        basename = os.path.basename(info.path)

        assert "__retrain_v2_final.csv.gz" in basename
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

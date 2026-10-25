import csv
import hashlib
import json
from datetime import datetime, timezone

from services.replay_manifest_audit import audit_replay_manifest


def _write_dataset(path, rows):
    columns = (
        "cycle_id",
        "symbol",
        "decision",
        "mt5_time",
        "bars_since_close",
        "label",
    )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row(cycle_id, symbol, decision, timestamp, bars_since_close, label):
    return {
        "cycle_id": cycle_id,
        "symbol": symbol,
        "decision": decision,
        "mt5_time": str(int(timestamp.timestamp())),
        "bars_since_close": str(bars_since_close),
        "label": label,
    }


def _write_manifest(path, dataset_path, dataset_hash, rows):
    manifest = {
        "version": "1.0",
        "dataset_path": str(dataset_path),
        "dataset_sha256": dataset_hash,
        "total_cycles": len(rows),
        "train_periods": ["2026-01-01 → 2026-01-02"],
        "val_periods": ["2026-01-03 → 2026-01-04"],
        "holdout_periods": ["2026-01-05 → 2026-01-06"],
        "decision_distribution": {"BUY": 1, "SELL": 1, "HOLD": 1},
        "benchmarks": {
            "random": {"n": 3},
            "ma": {"n": 3},
        },
        "protocol": {"universe": ["EURUSD", "GBPUSD", "USDCHF"]},
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _valid_fixture(tmp_path):
    data_path = tmp_path / "dataset.csv"
    rows = [
        _row("EURUSD_1", "EURUSD", "BUY", datetime(2026, 1, 1, tzinfo=timezone.utc), 5, "BUY"),
        _row("GBPUSD_2", "GBPUSD", "SELL", datetime(2026, 1, 3, tzinfo=timezone.utc), 4, "SELL"),
        _row("USDCHF_3", "USDCHF", "HOLD", datetime(2026, 1, 5, tzinfo=timezone.utc), 3, "HOLD"),
    ]
    data_hash = _write_dataset(data_path, rows)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, data_path, data_hash, rows)
    return manifest_path


def test_accepts_dataset_matching_manifest_and_protocol(tmp_path):
    manifest_path = _valid_fixture(tmp_path)

    report = audit_replay_manifest(manifest_path)

    assert report.valid
    assert report.actual_rows == 3
    assert report.actual_symbols == ("EURUSD", "GBPUSD", "USDCHF")
    assert report.split_row_counts == {"train": 1, "val": 1, "holdout": 1}
    assert report.blocking_issues == ()


def test_rejects_p31_artifact_with_unreconciled_data(tmp_path):
    data_path = tmp_path / "dataset.csv"
    duplicate = _row(
        "EURUSD_1",
        "EURUSD",
        "HOLD",
        datetime(2026, 1, 7, tzinfo=timezone.utc),
        -1,
        "",
    )
    rows = [duplicate, duplicate]
    data_hash = _write_dataset(data_path, rows)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, data_path, data_hash, rows)

    report = audit_replay_manifest(manifest_path)

    assert not report.valid
    assert report.duplicate_cycle_ids == 1
    assert report.negative_bars_since_close == 2
    assert report.rows_outside_splits == 2
    assert "symbol_coverage_mismatch" in report.blocking_issues
    assert "invalid_labels" in report.blocking_issues


def test_rejects_dataset_hash_or_distribution_mismatch(tmp_path):
    manifest_path = _valid_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dataset_sha256"] = "0" * 64
    manifest["decision_distribution"]["BUY"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_replay_manifest(manifest_path)

    assert not report.valid
    assert "dataset_hash_mismatch" in report.blocking_issues
    assert "decision_distribution_mismatch" in report.blocking_issues


def test_rejects_non_finite_bars_since_close(tmp_path):
    data_path = tmp_path / "dataset.csv"
    rows = [
        _row(
            "EURUSD_1",
            "EURUSD",
            "BUY",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "nan",
            "BUY",
        ),
        _row(
            "GBPUSD_2",
            "GBPUSD",
            "SELL",
            datetime(2026, 1, 3, tzinfo=timezone.utc),
            4,
            "SELL",
        ),
        _row(
            "USDCHF_3",
            "USDCHF",
            "HOLD",
            datetime(2026, 1, 5, tzinfo=timezone.utc),
            3,
            "HOLD",
        ),
    ]
    data_hash = _write_dataset(data_path, rows)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, data_path, data_hash, rows)

    report = audit_replay_manifest(manifest_path)

    assert not report.valid
    assert report.invalid_bars_since_close == 1

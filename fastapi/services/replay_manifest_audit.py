"""Fail-closed integrity checks for sealed model-evaluation replay datasets."""

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

_ACTIONS = ("BUY", "SELL", "HOLD")
_REQUIRED_COLUMNS = ("cycle_id", "symbol", "decision", "mt5_time")
_KNOWN_SYMBOLS = ("EURUSD", "GBPUSD", "USDCHF")
_SPLITS = ("train", "val", "holdout")


@dataclass(frozen=True)
class ReplayManifestAudit:
    manifest_path: str
    dataset_path: str
    manifest_sha256: str
    dataset_sha256: str
    declared_rows: int
    actual_rows: int
    declared_symbols: tuple[str, ...]
    actual_symbols: tuple[str, ...]
    declared_decisions: dict[str, int]
    actual_decisions: dict[str, int]
    split_row_counts: dict[str, int]
    rows_outside_splits: int
    duplicate_cycle_ids: int
    cycle_id_symbol_mismatches: int
    invalid_timestamps: int
    invalid_bars_since_close: int
    negative_bars_since_close: int
    directional_labels: int
    blocking_issues: tuple[str, ...]
    valid: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _resolve_dataset_path(manifest_path: Path, manifest: dict) -> Path:
    value = manifest["dataset_path"]
    dataset_path = Path(value)
    return dataset_path if dataset_path.is_absolute() else manifest_path.parent / dataset_path


def _period_ranges(manifest: dict) -> dict[str, tuple[tuple[date, date], ...]]:
    ranges = {}
    for split in _SPLITS:
        periods = manifest[f"{split}_periods"]
        ranges[split] = tuple(
            (
                datetime.strptime(start.strip(), "%Y-%m-%d").date(),
                datetime.strptime(end.strip(), "%Y-%m-%d").date(),
            )
            for start, end in (period.split("→", 1) for period in periods)
        )
    return ranges


def _row_timestamp(row: dict[str, str]) -> datetime:
    value = float(row["mt5_time"])
    return datetime.fromtimestamp(value, timezone.utc)


def _audit_identity(rows: list[dict[str, str]], columns: tuple[str, ...], manifest: dict):
    issues = []
    if any(column not in columns for column in _REQUIRED_COLUMNS):
        issues.append("missing_required_columns")
    declared_symbols = tuple(sorted(manifest["protocol"]["universe"]))
    actual_symbols = tuple(sorted({row.get("symbol", "") for row in rows if row.get("symbol")}))
    if actual_symbols != declared_symbols:
        issues.append("symbol_coverage_mismatch")

    cycle_ids = [row.get("cycle_id", "") for row in rows]
    duplicate_ids = len(cycle_ids) - len(set(cycle_ids))
    if duplicate_ids:
        issues.append("duplicate_cycle_ids")
    symbol_mismatches = sum(
        (row.get("cycle_id") or "").startswith(f"{symbol}_")
        and (row.get("symbol") or "") != symbol
        for row in rows
        for symbol in _KNOWN_SYMBOLS
    )
    if symbol_mismatches:
        issues.append("cycle_id_symbol_mismatch")

    timestamps = []
    invalid_timestamps = 0
    for row in rows:
        try:
            timestamps.append(_row_timestamp(row))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            invalid_timestamps += 1
    if invalid_timestamps:
        issues.append("invalid_timestamps")
    return (
        declared_symbols,
        actual_symbols,
        duplicate_ids,
        symbol_mismatches,
        timestamps,
        invalid_timestamps,
        issues,
    )


def _audit_decisions(rows: list[dict[str, str]], manifest: dict):
    issues = []
    actual = {action: 0 for action in _ACTIONS}
    for row in rows:
        decision = row.get("decision") or ""
        if decision not in actual:
            issues.append("invalid_decision")
            continue
        actual[decision] += 1
    declared = {
        action: int(manifest["decision_distribution"].get(action, 0))
        for action in _ACTIONS
    }
    if actual != declared:
        issues.append("decision_distribution_mismatch")
    if actual["BUY"] + actual["SELL"] == 0:
        issues.append("no_directional_decisions")
    return declared, actual, issues


def _audit_labels_and_splits(rows, columns, manifest, timestamps):
    issues = []
    invalid_bars = 0
    negative_bars = 0
    for row in rows:
        try:
            bars = float(row["bars_since_close"])
        except (KeyError, TypeError, ValueError):
            invalid_bars += 1
            continue
        if not math.isfinite(bars):
            invalid_bars += 1
            continue
        if bars < 0:
            negative_bars += 1
    if "bars_since_close" not in columns:
        invalid_bars = len(rows)
    if invalid_bars:
        issues.append("invalid_bars_since_close")
    if negative_bars:
        issues.append("negative_bars_since_close")

    if "label" not in columns:
        directional_labels = 0
        issues.append("missing_directional_labels")
    else:
        labels = [(row.get("label") or "").strip().upper() for row in rows]
        if any(label not in _ACTIONS for label in labels):
            issues.append("invalid_labels")
        directional_labels = sum(label in ("BUY", "SELL") for label in labels)
        if directional_labels == 0:
            issues.append("no_directional_labels")

    split_ranges = _period_ranges(manifest)
    split_counts = {
        split: sum(
            any(start <= timestamp.date() <= end for start, end in ranges)
            for timestamp in timestamps
        )
        for split, ranges in split_ranges.items()
    }
    outside = len(timestamps) - sum(split_counts.values())
    if any(count == 0 for count in split_counts.values()):
        issues.append("empty_temporal_split")
    if outside:
        issues.append("rows_outside_splits")

    benchmarks = manifest.get("benchmarks", {})
    if not benchmarks or any(int(value.get("n", 0)) <= 0 for value in benchmarks.values()):
        issues.append("benchmarks_without_samples")
    return invalid_bars, negative_bars, directional_labels, split_counts, outside, issues


def audit_replay_manifest(path: str | Path) -> ReplayManifestAudit:
    manifest_path = Path(path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    dataset_path = _resolve_dataset_path(manifest_path, manifest)
    dataset_bytes = dataset_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    dataset_hash = hashlib.sha256(dataset_bytes).hexdigest()

    with dataset_path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        columns = tuple(reader.fieldnames or ())
        rows = list(reader)

    issues = []
    if "dataset_sha256" not in manifest:
        issues.append("missing_dataset_hash")
    elif manifest["dataset_sha256"] != dataset_hash:
        issues.append("dataset_hash_mismatch")
    if len(rows) != manifest["total_cycles"]:
        issues.append("row_count_mismatch")
    (
        declared_symbols,
        actual_symbols,
        duplicate_cycle_ids,
        cycle_id_symbol_mismatches,
        timestamps,
        invalid_timestamps,
        identity_issues,
    ) = _audit_identity(rows, columns, manifest)
    declared_decisions, actual_decisions, decision_issues = _audit_decisions(rows, manifest)
    (
        invalid_bars_since_close,
        negative_bars_since_close,
        directional_labels,
        split_row_counts,
        rows_outside_splits,
        sample_issues,
    ) = _audit_labels_and_splits(rows, columns, manifest, timestamps)
    issues.extend(identity_issues)
    issues.extend(decision_issues)
    issues.extend(sample_issues)

    return ReplayManifestAudit(
        manifest_path=str(manifest_path),
        dataset_path=str(dataset_path),
        manifest_sha256=manifest_hash,
        dataset_sha256=dataset_hash,
        declared_rows=int(manifest["total_cycles"]),
        actual_rows=len(rows),
        declared_symbols=declared_symbols,
        actual_symbols=actual_symbols,
        declared_decisions=declared_decisions,
        actual_decisions=actual_decisions,
        split_row_counts=split_row_counts,
        rows_outside_splits=rows_outside_splits,
        duplicate_cycle_ids=duplicate_cycle_ids,
        cycle_id_symbol_mismatches=cycle_id_symbol_mismatches,
        invalid_timestamps=invalid_timestamps,
        invalid_bars_since_close=invalid_bars_since_close,
        negative_bars_since_close=negative_bars_since_close,
        directional_labels=directional_labels,
        blocking_issues=tuple(dict.fromkeys(issues)),
        valid=not issues,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    report = audit_replay_manifest(args.manifest)
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    return 0 if report.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())

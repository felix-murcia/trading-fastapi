"""Read-only integrity audit for raw MT5 candle exports."""

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

_TIMEFRAME_HOURS = {"H1": 1, "H4": 4, "D1": 24}
_OHLC_COLUMNS = ("<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>")
_REQUIRED_COLUMNS = ("<DATE>", *_OHLC_COLUMNS, "<SPREAD>")


@dataclass(frozen=True)
class MarketExportAudit:
    path: str
    sha256: str
    timeframe: str
    timestamp_basis: str
    rows: int
    first_timestamp: str | None
    last_timestamp: str | None
    missing_columns: tuple[str, ...]
    invalid_timestamp_rows: int
    duplicate_timestamps: int
    out_of_order_timestamps: int
    invalid_ohlc_rows: int
    invalid_spread_rows: int
    gap_count: int
    max_gap_hours: float
    calendar_review_required: bool
    valid: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_timestamp(
    row: Mapping[str, str | None],
    timeframe: str,
    missing_columns: tuple[str, ...],
) -> datetime | None:
    if "<DATE>" in missing_columns or (
        timeframe != "D1" and "<TIME>" in missing_columns
    ):
        return None
    try:
        date_text = row["<DATE>"]
        time_text = row.get("<TIME>")
        if timeframe != "D1" and not time_text:
            return None
        stamp_text = f"{date_text} {time_text}" if time_text else date_text
        stamp_format = "%Y.%m.%d %H:%M:%S" if time_text else "%Y.%m.%d"
        return datetime.strptime(stamp_text, stamp_format)
    except (KeyError, TypeError, ValueError):
        return None


def _invalid_ohlc(
    row: Mapping[str, str | None],
    missing_columns: tuple[str, ...],
) -> bool:
    if any(column in missing_columns for column in _OHLC_COLUMNS):
        return True
    try:
        open_, high, low, close = (float(row[column]) for column in _OHLC_COLUMNS)
    except (KeyError, TypeError, ValueError):
        return True
    prices = (open_, high, low, close)
    return not (
        all(math.isfinite(price) and price > 0 for price in prices)
        and high >= max(open_, close)
        and low <= min(open_, close)
        and high >= low
    )


def _invalid_spread(
    row: Mapping[str, str | None],
    missing_columns: tuple[str, ...],
) -> bool:
    if "<SPREAD>" in missing_columns:
        return True
    try:
        spread = float(row["<SPREAD>"])
    except (KeyError, TypeError, ValueError):
        return True
    return not math.isfinite(spread) or spread < 0


def _gap_summary(timestamps: list[datetime], timeframe: str) -> tuple[int, float]:
    nominal_hours = _TIMEFRAME_HOURS[timeframe]
    gaps = [
        (current - previous).total_seconds() / 3600
        for previous, current in zip(timestamps, timestamps[1:])
        if current > previous
    ]
    return sum(gap > nominal_hours for gap in gaps), max(gaps, default=0.0)


def audit_market_export(
    path: str | Path,
    *,
    timeframe: str,
    timestamp_basis: str,
) -> MarketExportAudit:
    """Audit structure and price integrity without inferring timezone or calendar."""
    source = Path(path)
    raw = source.read_bytes()
    if timeframe not in _TIMEFRAME_HOURS:
        raise ValueError(f"timeframe no soportado: {timeframe!r}")
    if timestamp_basis not in ("UTC", "BROKER"):
        raise ValueError("timestamp_basis debe declararse como 'UTC' o 'BROKER'")

    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    required = list(_REQUIRED_COLUMNS)
    if timeframe != "D1":
        required.append("<TIME>")
    missing = tuple(column for column in required if column not in fieldnames)

    timestamps: list[datetime] = []
    invalid_timestamp_rows = 0
    invalid_ohlc_rows = 0
    invalid_spread_rows = 0
    for row in rows:
        stamp = _parse_timestamp(row, timeframe, missing)
        if stamp is None:
            invalid_timestamp_rows += 1
        else:
            timestamps.append(stamp)
        invalid_ohlc_rows += _invalid_ohlc(row, missing)
        invalid_spread_rows += _invalid_spread(row, missing)

    duplicate_timestamps = len(timestamps) - len(set(timestamps))
    out_of_order_timestamps = sum(
        previous > current
        for previous, current in zip(timestamps, timestamps[1:])
    )
    gap_count, max_gap_hours = _gap_summary(timestamps, timeframe)
    valid = bool(rows) and not (
        missing
        or invalid_timestamp_rows
        or duplicate_timestamps
        or out_of_order_timestamps
        or invalid_ohlc_rows
        or invalid_spread_rows
    )

    return MarketExportAudit(
        path=str(source),
        sha256=hashlib.sha256(raw).hexdigest(),
        timeframe=timeframe,
        timestamp_basis=timestamp_basis,
        rows=len(rows),
        first_timestamp=timestamps[0].isoformat(sep=" ") if timestamps else None,
        last_timestamp=timestamps[-1].isoformat(sep=" ") if timestamps else None,
        missing_columns=missing,
        invalid_timestamp_rows=invalid_timestamp_rows,
        duplicate_timestamps=duplicate_timestamps,
        out_of_order_timestamps=out_of_order_timestamps,
        invalid_ohlc_rows=invalid_ohlc_rows,
        invalid_spread_rows=invalid_spread_rows,
        gap_count=gap_count,
        max_gap_hours=max_gap_hours,
        calendar_review_required=gap_count > 0,
        valid=valid,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--timeframe", required=True, choices=tuple(_TIMEFRAME_HOURS))
    parser.add_argument(
        "--timestamp-basis", required=True, choices=("UTC", "BROKER"),
        help="declaración explícita del reloj fuente; nunca se infiere del CSV",
    )
    args = parser.parse_args(argv)

    reports = [
        audit_market_export(
            path,
            timeframe=args.timeframe,
            timestamp_basis=args.timestamp_basis,
        )
        for path in args.paths
    ]
    print(json.dumps([report.to_dict() for report in reports], indent=2, ensure_ascii=False))
    return 0 if all(report.valid and not report.calendar_review_required for report in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())

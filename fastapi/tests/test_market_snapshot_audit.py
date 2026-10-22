import csv
from datetime import datetime, timedelta

import pytest

from services.market_snapshot_audit import audit_market_export


def _write_export(path, rows, *, daily=False):
    columns = (
        ["<DATE>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<SPREAD>"]
        if daily
        else ["<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<SPREAD>"]
    )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _h1_row(stamp, *, open_=1.0, high=1.2, low=0.9, close=1.1, spread=2):
    return {
        "<DATE>": stamp.strftime("%Y.%m.%d"),
        "<TIME>": stamp.strftime("%H:%M:%S"),
        "<OPEN>": str(open_),
        "<HIGH>": str(high),
        "<LOW>": str(low),
        "<CLOSE>": str(close),
        "<SPREAD>": str(spread),
    }


class TestAuditMarketExport:
    def test_hashes_source_and_reports_clean_ordered_ohlc(self, tmp_path):
        path = tmp_path / "EURUSD_H1.csv"
        start = datetime(2026, 1, 2)
        _write_export(path, [_h1_row(start + timedelta(hours=i)) for i in range(3)])

        report = audit_market_export(path, timeframe="H1", timestamp_basis="UTC")

        assert report.valid
        assert report.rows == 3
        assert report.timestamp_basis == "UTC"
        assert report.duplicate_timestamps == 0
        assert report.out_of_order_timestamps == 0
        assert report.invalid_ohlc_rows == 0
        assert report.invalid_spread_rows == 0
        assert report.sha256

    def test_reports_gaps_without_assuming_they_are_corrupt(self, tmp_path):
        path = tmp_path / "EURUSD_H1.csv"
        start = datetime(2026, 1, 2)
        _write_export(path, [_h1_row(start), _h1_row(start + timedelta(hours=49))])

        report = audit_market_export(path, timeframe="H1", timestamp_basis="UTC")

        assert report.valid
        assert report.gap_count == 1
        assert report.max_gap_hours == 49
        assert report.calendar_review_required

    def test_detects_duplicate_and_out_of_order_timestamps(self, tmp_path):
        path = tmp_path / "EURUSD_H1.csv"
        start = datetime(2026, 1, 2)
        _write_export(
            path,
            [_h1_row(start), _h1_row(start), _h1_row(start - timedelta(hours=1))],
        )

        report = audit_market_export(path, timeframe="H1", timestamp_basis="UTC")

        assert not report.valid
        assert report.duplicate_timestamps == 1
        assert report.out_of_order_timestamps == 1

    def test_detects_invalid_ohlc_and_spread(self, tmp_path):
        path = tmp_path / "EURUSD_H1.csv"
        start = datetime(2026, 1, 2)
        row = _h1_row(start, high=1.05, low=1.0, close=1.1, spread=-1)
        _write_export(path, [row])

        report = audit_market_export(path, timeframe="H1", timestamp_basis="UTC")

        assert not report.valid
        assert report.invalid_ohlc_rows == 1
        assert report.invalid_spread_rows == 1

    def test_reports_invalid_timestamp_and_missing_columns(self, tmp_path):
        path = tmp_path / "EURUSD_H1.csv"
        path.write_text(
            "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\n"
            "invalid\t00:00:00\t1.0\t1.1\t0.9\n",
            encoding="utf-8",
        )

        report = audit_market_export(path, timeframe="H1", timestamp_basis="UTC")

        assert not report.valid
        assert report.invalid_timestamp_rows == 1
        assert report.missing_columns == ("<CLOSE>", "<SPREAD>")

    def test_requires_time_value_for_intraday_export(self, tmp_path):
        path = tmp_path / "EURUSD_H1.csv"
        row = _h1_row(datetime(2026, 1, 2))
        row["<TIME>"] = ""
        _write_export(path, [row])

        report = audit_market_export(path, timeframe="H1", timestamp_basis="UTC")

        assert not report.valid
        assert report.invalid_timestamp_rows == 1

    def test_supports_daily_exports_without_time_column(self, tmp_path):
        path = tmp_path / "EURUSD_Daily.csv"
        _write_export(
            path,
            [{
                "<DATE>": "2026.01.02",
                "<OPEN>": "1.0",
                "<HIGH>": "1.2",
                "<LOW>": "0.9",
                "<CLOSE>": "1.1",
                "<SPREAD>": "2",
            }],
            daily=True,
        )

        report = audit_market_export(path, timeframe="D1", timestamp_basis="UTC")

        assert report.valid
        assert report.rows == 1
        assert report.gap_count == 0

    def test_requires_explicit_timestamp_basis(self, tmp_path):
        path = tmp_path / "EURUSD_H1.csv"
        _write_export(path, [_h1_row(datetime(2026, 1, 2))])

        with pytest.raises(ValueError, match="timestamp_basis"):
            audit_market_export(path, timeframe="H1", timestamp_basis="")

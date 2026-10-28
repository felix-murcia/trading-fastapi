"""Test de paridad: trim_trend_warmup regla pura."""

from src.domain.rules.trim_trend_warmup import trim_trend_warmup, WARMUP_TRIM_ROWS


def test_trim_trend_warmup_default():
    rows = [{"i": i} for i in range(300)]
    result = trim_trend_warmup(rows)
    assert len(result) == 300 - WARMUP_TRIM_ROWS
    assert result[0] == {"i": WARMUP_TRIM_ROWS}


def test_trim_trend_warmup_custom():
    rows = [{"i": i} for i in range(10)]
    result = trim_trend_warmup(rows, warmup_rows=3)
    assert len(result) == 7
    assert result[0] == {"i": 3}


def test_trim_trend_warmup_empty():
    result = trim_trend_warmup([])
    assert result == []


def test_trim_trend_warmup_does_not_mutate():
    rows = [{"i": i} for i in range(10)]
    original_len = len(rows)
    trim_trend_warmup(rows, warmup_rows=3)
    assert len(rows) == original_len  # no muta

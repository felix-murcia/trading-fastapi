"""Test de paridad: alignment_gate regla pura (T-28)."""

from src.domain.rules.alignment_gate import alignment_gate, AlignmentResult, pair_state, polarity


def test_polarity():
    assert polarity("BUY") == 1
    assert polarity("BULLISH") == 1
    assert polarity("SELL") == -1
    assert polarity("BEARISH") == -1
    assert polarity("HOLD") == 0
    assert polarity("NEUTRAL") == 0
    assert polarity("UNKNOWN") is None


def test_pair_state():
    assert pair_state("BUY", "BULLISH") == "aligned"
    assert pair_state("SELL", "BEARISH") == "aligned"
    assert pair_state("BUY", "BEARISH") == "conflict"
    assert pair_state("SELL", "BULLISH") == "conflict"
    assert pair_state("HOLD", "NEUTRAL") == "neutral"
    assert pair_state("BUY", "HOLD") == "split"
    assert pair_state("UNKNOWN", "BULLISH") == "unknown"


def test_alignment_gate_ok():
    # 60 aligned (agreement=0.60), 20 conflict (0.20 <= max), 20 split.
    # Entropía de 3 estados con pesas 0.6/0.2/0.2 ≈ 1.37 bits >= 1.0.
    rows = [("BUY", "BULLISH")] * 60 + [("BUY", "BEARISH")] * 20 + [("BUY", "HOLD")] * 20
    result = alignment_gate(rows)
    assert result.ok is True
    assert result.n_active == 100
    assert result.agreement_rate == 0.60
    assert result.conflict_rate == 0.20
    assert result.joint_entropy_bits >= 1.0
    assert result.sufficient is True


def test_alignment_gate_insufficient_sample():
    rows = [("BUY", "BULLISH")] * 10
    result = alignment_gate(rows)
    assert result.ok is False
    assert "insufficient_sample" in result.reasons
    assert result.sufficient is False


def test_alignment_gate_conflict_above_threshold():
    rows = [("BUY", "BEARISH")] * 30 + [("BUY", "BULLISH")] * 70
    result = alignment_gate(rows)
    assert result.ok is False
    assert "conflict_above_threshold" in result.reasons


def test_alignment_gate_agreement_below_threshold():
    rows = [("BUY", "BULLISH")] * 55 + [("BUY", "BEARISH")] * 45
    result = alignment_gate(rows)
    assert result.ok is False
    assert "agreement_below_threshold" in result.reasons


def test_alignment_gate_empty():
    result = alignment_gate([])
    assert result.ok is False
    assert result.n_active == 0
    assert result.sufficient is False

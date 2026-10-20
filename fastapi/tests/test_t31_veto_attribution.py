"""
T-31 (2026-10-20) — ATRIBUCIÓN DEL MOTIVO DE UN HOLD.

Hallazgo (medido el 20-10 sobre la noche del 19→20): el `veto_reason` de la
auditoría atribuía "bias_veto_bullish" a HOLDs que en realidad había bloqueado
el QUÓRUM (o la falta de autoridad del LLM). La atribución se INFERÍA del
mero hecho de que el sesgo se opusiera, no de que el veto hubiera actuado ⇒
la traza mentía sobre la causa real.

Contrato nuevo:
- el veto de sesgo sólo se declara si REALMENTE revirtió (`bias_veto_fired`);
- el quórum que bloqueó se declara como `quorum_veto`;
- sin cambio en los vetos mecánicos (posición/sizing/reward/reentry/…) ni en
  su prioridad.
"""
import pytest

from routers.ai import _veto_reason


class TestVetoAttribution:
    def test_bias_veto_only_when_it_fired(self):
        """El sesgo se opone PERO el veto no actuó (sin autoridad) ⇒ NO se
        atribuye al veto de sesgo."""
        qwen = {"regime": "TRENDING", "quality": 9.0, "bias": "BULLISH", "bias_prob": 0.85}
        motivo = _veto_reason("SELL", "HOLD", qwen, bias_veto_fired=False)
        assert motivo != "bias_veto_bullish", (
            "no se puede atribuir al veto de sesgo si no revirtió"
        )
        assert motivo == "unknown_veto"

    def test_bias_veto_when_it_fired(self):
        qwen = {"regime": "TRENDING", "quality": 9.0, "bias": "BULLISH", "bias_prob": 0.85}
        assert _veto_reason("SELL", "HOLD", qwen, bias_veto_fired=True) == "bias_veto_bullish"
        qwen_bear = {"regime": "TRENDING", "quality": 9.0, "bias": "BEARISH", "bias_prob": 0.25}
        assert _veto_reason("BUY", "HOLD", qwen_bear, bias_veto_fired=True) == "bias_veto_bearish"

    def test_quorum_attributed_when_it_blocked(self):
        """El quórum que bloqueó se declara como tal (no como unknown)."""
        qwen = {"regime": "TRENDING", "quality": 9.0, "bias": "BULLISH", "bias_prob": 0.85}
        motivo = _veto_reason("SELL", "HOLD", qwen, quorum_rejected=True)
        assert motivo == "quorum_veto"

    def test_mechanical_vetoes_keep_priority(self):
        """La prioridad de los vetos mecánicos no cambia."""
        qwen = {"regime": "RANGING", "quality": 1.0, "bias": "BEARISH"}
        assert _veto_reason("BUY", "HOLD", qwen, position_rejected=True) == "position_veto"
        assert _veto_reason("BUY", "HOLD", qwen, sizing_rejected=True) == "sizing_veto"
        assert _veto_reason("BUY", "HOLD", qwen, reward_rejected=True) == "reward_below_min"
        assert _veto_reason("BUY", "HOLD", qwen, reentry_rejected=True) == "reentry_veto"

    def test_no_change_when_nothing_rejected(self):
        qwen = {"regime": "TRENDING", "quality": 9.0, "bias": "BULLISH"}
        assert _veto_reason("SELL", "SELL", qwen) is None

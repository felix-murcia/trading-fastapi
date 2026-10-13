"""
P2 (H2 + H9) — anti-anchoring de los prompts few-shot de Qwen.

H2: el prompt unificado de qwen_client.py tenía UN solo ejemplo few-shot
(`"quality": 7.5`, bias NEUTRAL, confidence_modifier 1.0). Con
temperature=0.2 y modelo pequeño la respuesta era la plantilla casi
verbatim → quality_score constante 7.5 en 511+ ciclos = veto de calidad
decorativo (nunca discriminaba).

H9: el prompt de trade_journal._query_qwen_trade_insight tenía UN solo
ejemplo (`confidence 0.75, quality_score 3.5`) → mismos valores constantes
en los últimos trade_insights.

Propiedad fijada por estos tests (2026-10-09):
  1. >= 2 ejemplos few-shot con valores DISTINTOS (rompe el ancla unitaria).
  2. Instrucción explícita de no copiar los valores de los ejemplos.
  3. Rúbrica de puntuación de quality (7-10 / 1-4 / 4-7 con criterios).
  4. "ONE JSON object" — defensa contra salida doble con dos ejemplos.

RCA 2026-10-13 (ancla determinista out-of-window) añade:
  5. >= 3 ejemplos few-shot + advertencia de copia POST-ejemplos.
  6. Guarda anti-ancla: reason == razón de ejemplo SIN reentry_ok →
     degradada fail-closed (nunca quality=8.5 prestado).
  7. Sincronía FORMAT_EXAMPLE_REASONS ↔ razones reales del prompt.

Si alguien vuelve a un solo valor ancla en cualquiera de los dos prompts,
estos tests caen a rojo.
"""
import json
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import services.qwen_client as qwen_client
import services.trade_journal as trade_journal
from services.qwen_client import QwenHealthState, query_qwen_unified


@pytest.fixture(autouse=True)
def fresh_qwen_state(monkeypatch):
    """Estado de Qwen aislado por test — el módulo es estado compartido."""
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())


def _capturing_client(canned_content: str):
    """Fake de httpx.AsyncClient que captura el prompt y devuelve JSON válido."""

    body = {"choices": [{"message": {"content": canned_content}}]}
    body_text = json.dumps(body)

    class _Resp:
        status_code = 200
        # Cuerpo crudo: lo lee la traza QWEN-RESPONSE (httpx real siempre
        # expone .text) — el fake debe reflejar la superficie usada por el código.
        text = body_text

        def json(self):
            return body

    class _Client:
        captured: dict = {}

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            _Client.captured["prompt"] = json["messages"][1]["content"]
            return _Resp()

    return _Client


_UNIFIED_CANNED = (
    '{"quality": 6.5, "reason": "mixed signals", "bias": "NEUTRAL", '
    '"confidence_modifier": 1.0, "sl_adjusted": 0.20, "tp_adjusted": 0.40, '
    '"regime": "RANGING"}'
)

_UNIFIED_ARGS = dict(
    symbol="EURUSD", decision="BUY", ml_prob=0.75, rsi=55.0, atr=0.00100,
    atr_pct=0.01, macd_hist=0.0005, bb_pos=0.6, range_pct=0.02, last_ret=0.001,
    hour=10, news="No major news", candle_context="last 10 H1 candles",
    # Paquete 1-4: sin sl_proposed/tp_proposed (contrato sin SL/TP)
    cycle_id="EURUSD_1234567890",
    logger=MagicMock(),
)


async def _unified_prompt() -> str:
    client = _capturing_client(_UNIFIED_CANNED)
    with patch("httpx.AsyncClient", client):
        result = await query_qwen_unified(**_UNIFIED_ARGS)
    assert result["ok"] is True, "canned response debe parsear (setup del test)"
    return client.captured["prompt"]


class TestUnifiedPromptAnchoring:
    """qwen_client.query_qwen_unified — H2 (quality_score decorativo)."""

    @pytest.mark.asyncio
    async def test_two_distinct_example_qualities(self):
        """Debe haber >=2 ejemplos con quality DISTINTO (rompe el ancla 7.5)."""
        prompt = await _unified_prompt()
        qualities = re.findall(r'"quality":\s*([0-9]+(?:\.[0-9]+)?)', prompt)
        assert len(set(qualities)) >= 2, (
            f"examples de quality anclados en un solo valor: {qualities}"
        )

    @pytest.mark.asyncio
    async def test_two_distinct_example_biases(self):
        """Los ejemplos deben cubrir sesgos opuestos (BULLISH vs BEARISH)."""
        prompt = await _unified_prompt()
        biases = re.findall(r'"bias":\s*"([A-Z]+)"', prompt)
        assert len(set(biases)) >= 2, f"biases de ejemplo anclados: {biases}"

    @pytest.mark.asyncio
    async def test_explicit_do_not_copy_instruction(self):
        prompt = await _unified_prompt()
        assert re.search(r"(?i)do not copy", prompt), (
            "falta la instrucción explícita de no copiar los valores de ejemplo"
        )

    @pytest.mark.asyncio
    async def test_quality_rubric_present(self):
        """Rúbrica con tramos separados (7-10 / 4-7 / 1-4) y criterios."""
        prompt = await _unified_prompt()
        assert "7-10" in prompt and "1-4" in prompt, "falta la rúbrica de quality"

    @pytest.mark.asyncio
    async def test_single_json_object_instruction(self):
        """Con dos ejemplos la respuesta debe seguir siendo UN objeto JSON."""
        prompt = await _unified_prompt()
        assert re.search(r"(?i)one json object", prompt), (
            "falta 'ONE JSON object' (riesgo de salida doble → parse degrada)"
        )


_JOURNAL_CANNED = (
    '{"insight":"timing_error","confidence":0.9,'
    '"regime":"RANGING","quality_score":5.0}'
)


async def _journal_prompt() -> str:
    client = _capturing_client(_JOURNAL_CANNED)
    with patch("httpx.AsyncClient", client):
        insight, confidence, regime, quality = (
            await trade_journal._query_qwen_trade_insight(
                symbol="EURUSD",
                direction="SHORT",
                entry_time=datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc),
                exit_time=datetime(2026, 10, 9, 5, 20, tzinfo=timezone.utc),
                pnl=-5.72,
                pnl_pct=-0.0091,
                exit_reason="ea",
                sl_hit=False,
                tp_hit=False,
            )
        )
    assert insight == "timing_error", "canned response debe parsear (setup del test)"
    return client.captured["prompt"]


class TestJournalPromptAnchoring:
    """trade_journal._query_qwen_trade_insight — H9 (insights constantes)."""

    @pytest.mark.asyncio
    async def test_two_distinct_example_quality_scores(self):
        prompt = await _journal_prompt()
        qualities = re.findall(r'"quality_score":\s*([0-9]+(?:\.[0-9]+)?)', prompt)
        assert len(set(qualities)) >= 2, (
            f"quality_score de ejemplo anclada en un solo valor: {qualities}"
        )

    @pytest.mark.asyncio
    async def test_two_distinct_example_confidences(self):
        prompt = await _journal_prompt()
        confs = re.findall(r'"confidence":\s*([0-9]+(?:\.[0-9]+)?)', prompt)
        assert len(set(confs)) >= 2, f"confidence de ejemplo anclada: {confs}"

    @pytest.mark.asyncio
    async def test_two_distinct_example_insights(self):
        prompt = await _journal_prompt()
        insights = re.findall(r'"insight":\s*"([a-z_]+)"', prompt)
        assert len(set(insights)) >= 2, f"insight de ejemplo anclado: {insights}"

    @pytest.mark.asyncio
    async def test_explicit_do_not_copy_instruction(self):
        prompt = await _journal_prompt()
        assert re.search(r"(?i)do not copy", prompt), (
            "falta la instrucción explícita de no copiar los valores de ejemplo"
        )

    @pytest.mark.asyncio
    async def test_single_json_object_instruction(self):
        prompt = await _journal_prompt()
        assert re.search(r"(?i)one json object", prompt), (
            "falta 'ONE JSON object' (json.loads falla con salida doble)"
        )


# ---------------------------------------------------------------------------
# RCA 2026-10-13 — ancla determinista en la variante out-of-window.
#
# Evidencia: 10/10 ciclos sin bloque Reentry devolvieron byte a byte
# `{"quality": 8.5, "reason": "trend and ADX align", ...}` SIN reentry_ok
# (copia del ejemplo FORMAT #1). Replay en vivo 18:04 UTC: mismo servidor,
# variante out-of-window → ancla 100% reproducible; variante in-window →
# juzga bien. La guarda anti-ancla degrada fail-closed esa respuesta.
# ---------------------------------------------------------------------------

_ANCHOR_COPY = (
    '{"quality": 8.5, "reason": "trend and ADX align", "bias": "BULLISH", '
    '"confidence_modifier": 1.3, "regime": "TRENDING"}'
)

_ANCHOR_COPY_WITH_FIELD = (
    '{"quality": 8.5, "reason": "trend and ADX align", "bias": "BULLISH", '
    '"confidence_modifier": 1.3, "reentry_ok": true, "regime": "TRENDING"}'
)


async def _unified_with(canned: str) -> tuple:
    client = _capturing_client(canned)
    with patch("httpx.AsyncClient", client):
        result = await query_qwen_unified(**_UNIFIED_ARGS)
    return result, client.captured["prompt"]


class TestAnchorCopyGuardRCA:
    """Guarda anti-ancla (RCA 2026-10-13) + refuerzo de la variante fina."""

    @pytest.mark.asyncio
    async def test_anchor_copy_without_reentry_is_degraded(self):
        """Copia del ejemplo #1 sin reentry_ok → degradada fail-closed."""
        result, _ = await _unified_with(_ANCHOR_COPY)
        assert result["ok"] is False, (
            "la copia ancla pasó por juicio real: quality=8.5 prestado"
        )
        assert result["quality"] == 0.0, "debe ser quality=0 (fail-closed)"

    @pytest.mark.asyncio
    async def test_anchor_reason_with_reentry_field_not_degraded(self):
        """Especificidad: razón de ejemplo CON el campo → no es copia."""
        result, _ = await _unified_with(_ANCHOR_COPY_WITH_FIELD)
        assert result["ok"] is True, (
            "guarda demasiado agresiva: juzgó con reentry_ok presente"
        )

    @pytest.mark.asyncio
    async def test_genuine_reason_without_reentry_field_not_degraded(self):
        """Razón propia sin reentry_ok → el fail-open documentado sigue vivo."""
        result, _ = await _unified_with(_UNIFIED_CANNED)
        assert result["ok"] is True
        assert result["quality"] == 6.5

    @pytest.mark.asyncio
    async def test_three_format_examples_present(self):
        """Tercer ejemplo con valores distintos (aplana el prior del ancla)."""
        _, prompt = await _unified_with(_UNIFIED_CANNED)
        n_examples = prompt.count('"reentry_ok":')
        assert n_examples >= 3, f"sólo {n_examples} ejemplos FORMAT en el prompt"

    @pytest.mark.asyncio
    async def test_post_example_copy_warning(self):
        """Advertencia de no-copiar POST-EJEMPLOS (antes sólo estaba antes)."""
        _, prompt = await _unified_with(_UNIFIED_CANNED)
        assert re.search(r"(?i)copied from the examples", prompt), (
            "falta la advertencia post-ejemplos (el ancla se copiaba tras leerlas)"
        )

    @pytest.mark.asyncio
    async def test_out_of_window_reentry_section_present(self):
        """La variante fina (reentry=None) mantiene su sección Reentry check."""
        _, prompt = await _unified_with(_UNIFIED_CANNED)
        assert "Reentry check" in prompt, (
            "variante out-of-window sin sección Reentry check (regresión RCA)"
        )

    @pytest.mark.asyncio
    async def test_format_example_reasons_synced_with_prompt(self):
        """FORMAT_EXAMPLE_REASONS debe reflejar las razones EJEMPLO del prompt."""
        _, prompt = await _unified_with(_UNIFIED_CANNED)
        example_reasons = set(re.findall(r'"reason":\s*"([^"]+)"', prompt))
        assert example_reasons == set(qwen_client.FORMAT_EXAMPLE_REASONS), (
            f"desincronizados: prompt={example_reasons} "
            f"const={set(qwen_client.FORMAT_EXAMPLE_REASONS)}"
        )

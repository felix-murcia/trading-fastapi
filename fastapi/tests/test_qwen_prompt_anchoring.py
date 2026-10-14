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
from services.qwen_client import query_qwen_unified


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
            _Client.captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            _Client.captured["prompt"] = json["messages"][1]["content"]
            _Client.captured["payload"] = json
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


async def _unified_with(canned: str, **overrides) -> tuple:
    client = _capturing_client(canned)
    with patch("httpx.AsyncClient", client):
        result = await query_qwen_unified(**{**_UNIFIED_ARGS, **overrides})
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


class TestTemperatureExperimentB:
    """Opción B (orden Félix 2026-10-13): temperatura 0.2 → 0.4 — el modelo
    debe juzgar de verdad (a 0.2 cae en basin determinista de plantilla:
    q=8.5/conf_mod=1.30 constantes en 100% out-of-window)."""

    @pytest.mark.asyncio
    async def test_unified_temperature_is_point_four(self):
        """El payload del unificado fija la temperatura del experimento B."""
        client = _capturing_client(_UNIFIED_CANNED)
        with patch("httpx.AsyncClient", client):
            await query_qwen_unified(**_UNIFIED_ARGS)
        assert client.captured["payload"]["temperature"] == 0.4, (
            "temperatura del experimento B no aplicada"
        )

    @pytest.mark.asyncio
    async def test_temperature_constant_is_used(self):
        """El payload usa la constante QWEN_TEMPERATURE_UNIFIED (single source)."""
        client = _capturing_client(_UNIFIED_CANNED)
        with patch("httpx.AsyncClient", client):
            await query_qwen_unified(**_UNIFIED_ARGS)
        assert (
            client.captured["payload"]["temperature"]
            == qwen_client.QWEN_TEMPERATURE_UNIFIED
        )


# Orden Félix 2026-10-13 (opción C) — guarda anti-parrota: razón idéntica
# ≥3 ciclos consecutivos por símbolo → degradada fail-closed. Evidencia
# viva post-fix (c): q=8.5/conf_mod=1.30 conrazón fija «Trend, ADX, and
# session align with ML HOLD signal» idéntica 5/5 ciclos con datos
# distintos — plantilla parrota, no juicio.

_PARROT_A = (
    '{"quality": 7.0, "reason": "Parrot template Alpha", "bias": "BULLISH", '
    '"confidence_modifier": 1.1, "reentry_ok": true, "regime": "TRENDING"}'
)

_PARROT_B = (
    '{"quality": 6.0, "reason": "Choppy range without momentum follow-through", '
    '"bias": "BEARISH", "confidence_modifier": 0.9, "reentry_ok": true, '
    '"regime": "RANGING"}'
)


class TestParrotGuardRCA:
    """Guarda anti-parrota (opción C, orden 2026-10-13)."""

    # Variantes REALES observadas en vivo 17:10-17:40 (rotación de la
    # plantilla parrota): Jaccard entre ellas 0.58-0.67 ≥ umbral 0.5.
    _LIVE_V1 = (
        '{"quality": 8.5, "reason": "Trend, ADX, session and ML signal align", '
        '"bias": "BULLISH", "confidence_modifier": 1.3, "reentry_ok": true, '
        '"regime": "TRENDING"}'
    )
    _LIVE_V2 = (
        '{"quality": 8.5, "reason": "Trend, ADX, and ML signal align with bullish bias", '
        '"bias": "BULLISH", "confidence_modifier": 1.3, "reentry_ok": true, '
        '"regime": "TRENDING"}'
    )
    _LIVE_V3 = (
        '{"quality": 8.5, "reason": "trend, ADX, session align with ML HOLD signal", '
        '"bias": "NEUTRAL", "confidence_modifier": 1.3, "reentry_ok": true, '
        '"regime": "TRENDING"}'
    )

    @pytest.mark.asyncio
    async def test_two_identical_reasons_still_judged(self):
        """2 razones idénticas: por debajo del límite → ambas juicios."""
        r1, _ = await _unified_with(_PARROT_A)
        r2, _ = await _unified_with(_PARROT_A)
        assert r1["ok"] is True and r2["ok"] is True

    @pytest.mark.asyncio
    async def test_third_identical_reason_degrades(self):
        """3ª razón idéntica consecutiva → degradada fail-closed."""
        await _unified_with(_PARROT_A)
        await _unified_with(_PARROT_A)
        r3, _ = await _unified_with(_PARROT_A)
        assert r3["ok"] is False, "parrota pasó por juicio real"
        assert r3["quality"] == 0.0, "debe ser quality=0 (fail-closed)"

    @pytest.mark.asyncio
    async def test_parrot_degrade_persists(self):
        """La racha no se resetea al degradar: la 4ª también degrada."""
        await _unified_with(_PARROT_A)
        await _unified_with(_PARROT_A)
        r3, _ = await _unified_with(_PARROT_A)
        r4, _ = await _unified_with(_PARROT_A)
        assert r3["ok"] is False and r4["ok"] is False

    @pytest.mark.asyncio
    async def test_different_reason_resets_streak(self):
        """Razón disímil resetea la racha (A,A,B,A,A,A → degrada en la 6ª)."""
        results = []
        for canned in (_PARROT_A, _PARROT_A, _PARROT_B,
                       _PARROT_A, _PARROT_A, _PARROT_A):
            r, _ = await _unified_with(canned)
            results.append(r["ok"])
        assert results == [True, True, True, True, True, False], results

    @pytest.mark.asyncio
    async def test_live_rotating_variants_degrade_at_third(self):
        """C+: las 3 variantes REALES de la plantilla rotada (Jaccard
        ≥0.5 entre adyacentes) degradan en el 3er ciclo — el escape
        observado en vivo queda cubierto."""
        r1, _ = await _unified_with(self._LIVE_V1)
        r2, _ = await _unified_with(self._LIVE_V2)
        r3, _ = await _unified_with(self._LIVE_V3)
        assert r1["ok"] is True and r2["ok"] is True
        assert r3["ok"] is False, "la rotación de variantes escapó a la guarda C+"
        assert r3["quality"] == 0.0

    @pytest.mark.asyncio
    async def test_live_variants_pairwise_similar(self):
        """Fijar la separación medida: variantes vivas ≥0.5, genuinas <0.5."""
        v1 = qwen_client._reason_tokens("Trend, ADX, session and ML signal align")
        v2 = qwen_client._reason_tokens("Trend, ADX, and ML signal align with bullish bias")
        v3 = qwen_client._reason_tokens("trend, ADX, session align with ML HOLD signal")
        gen1 = qwen_client._reason_tokens("Mixed trend and range signals; momentum fading")
        gen2 = qwen_client._reason_tokens("Breakout in downtrend with weak volume")
        assert qwen_client._jaccard(v1, v2) >= 0.5
        assert qwen_client._jaccard(v2, v3) >= 0.5
        assert qwen_client._jaccard(gen1, gen2) < 0.5

    @pytest.mark.asyncio
    async def test_streak_is_per_symbol(self):
        """Rachas independientes por símbolo (preparado para G2)."""
        await _unified_with(_PARROT_A)                              # EURUSD ×1
        await _unified_with(_PARROT_A)                              # EURUSD ×2
        rg, _ = await _unified_with(_PARROT_A, symbol="GBPUSD")     # GBPUSD ×1
        re_, _ = await _unified_with(_PARROT_A)                     # EURUSD ×3 → degrade
        assert rg["ok"] is True, "la racha de EURUSD contaminó GBPUSD"
        assert re_["ok"] is False

    @pytest.mark.asyncio
    async def test_empty_reason_not_counted(self):
        """Razón vacía no cuenta racha (parse incompleto ≠ parrota)."""
        no_reason = (
            '{"quality": 6.0, "bias": "NEUTRAL", '
            '"confidence_modifier": 1.0, "reentry_ok": true, "regime": "RANGING"}'
        )
        r1, _ = await _unified_with(no_reason)
        r2, _ = await _unified_with(no_reason)
        r3, _ = await _unified_with(no_reason)
        assert r1["ok"] is True and r2["ok"] is True and r3["ok"] is True

    # ── S1 (orden Félix 2026-10-14): el disparador exige quality CONSTANTE ──
    # Razones genuinas del 8B en vivo (21:15/21:20, quality variable 6-9)
    # con Jaccard 0.50/0.60 ≥ umbral → la guarda solo-por-texto degradaba
    # juicio real (falso positivo medido: 2/12 ciclos, 21:25/21:30).
    _S1_VARIED_1 = (
        '{"quality": 8.0, "reason": "TRENDING setup with strong ADX and '
        'session context", "bias": "BULLISH", "confidence_modifier": 1.2, '
        '"reentry_ok": true, "regime": "TRENDING"}'
    )
    _S1_VARIED_2 = (
        '{"quality": 7.0, "reason": "TRENDING signal with ADX and session '
        'aligned", "bias": "BULLISH", "confidence_modifier": 1.2, '
        '"reentry_ok": true, "regime": "TRENDING"}'
    )
    _S1_VARIED_3 = (
        '{"quality": 6.0, "reason": "TRENDING setup with ADX and session '
        'aligned in trend", "bias": "BULLISH", "confidence_modifier": 1.2, '
        '"reentry_ok": true, "regime": "TRENDING"}'
    )

    @pytest.mark.asyncio
    async def test_similar_reasons_with_varied_quality_not_degraded(self):
        """S1: razón similar (Jaccard ≥0.5) PERO quality variable = juicio
        genuino (patrón del 8B: q 6.0-9.0 con razones trending similares),
        NO parrota — el disparador exige quality SIN variar (el 4B parroteaba
        q=8.5 byte a byte). Regresión del falso positivo medido 21:25/21:30."""
        # Verificar las condiciones del fixture: similar por texto…
        t1 = qwen_client._reason_tokens("TRENDING setup with strong ADX and session context")
        t2 = qwen_client._reason_tokens("TRENDING signal with ADX and session aligned")
        t3 = qwen_client._reason_tokens("TRENDING setup with ADX and session aligned in trend")
        assert qwen_client._jaccard(t1, t2) >= qwen_client.PARROT_SIMILARITY_THRESHOLD
        assert qwen_client._jaccard(t2, t3) >= qwen_client.PARROT_SIMILARITY_THRESHOLD
        # …pero con quality distinto en cada ciclo → los 3 son juicio.
        for canned in (self._S1_VARIED_1, self._S1_VARIED_2, self._S1_VARIED_3):
            r, _ = await _unified_with(canned)
            assert r["ok"] is True, (
                "falso positivo S1: quality variable degradó juicio real"
            )
            assert r["quality"] != 0.0

    @pytest.mark.asyncio
    async def test_same_quality_and_similar_reason_still_degrades(self):
        """S1: el disparador sigue activo para la firma parrota real —
        razón similar Y quality constante ≥3 ciclos → degradada."""
        # _PARROT_A = q 7.0 constante con la misma razón (firma parrota).
        await _unified_with(_PARROT_A)
        await _unified_with(_PARROT_A)
        r3, _ = await _unified_with(_PARROT_A)
        assert r3["ok"] is False and r3["quality"] == 0.0


class TestLLM8BLatencyBudget:
    """Orden Félix 2026-10-13: el Jetson sirve ahora Llama-3.1-8B-Instruct
    (sustituye al Qwen3-4B). Latencia medida 36-39s por llamada idle
    (vs 11-17s del 4B). Presupuesto: backend 45s < EA WebRequest 60s —
    con el timeout viejo (backend 20s / EA 30s) el 8B quedaba 'unavailable'
    en cada ciclo (fail-closed, veto de calidad 0)."""

    @pytest.mark.asyncio
    async def test_unified_timeout_covers_8b_latency(self):
        """El cliente httpx del unificado espera QWEN_TIMEOUT_UNIFIED (45s)."""
        client = _capturing_client(_UNIFIED_CANNED)
        with patch("httpx.AsyncClient", client):
            await query_qwen_unified(**_UNIFIED_ARGS)
        assert (
            client.captured["client_kwargs"]["timeout"]
            == qwen_client.QWEN_TIMEOUT_UNIFIED
            == 45.0
        ), "timeout del unificado no cubre la latencia del 8B (~39s)"

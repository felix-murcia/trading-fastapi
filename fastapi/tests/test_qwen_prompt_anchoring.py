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

S2 (orden Félix 2026-10-14, retirada de la guarda runtime anti-ancla):
la guarda (c) que degradaba reason==ejemplo SIN reentry_ok se retiró —
con el Llama-3.1-8B nunca copió el ejemplo (0 disparos desde 20:47 UTC
del 10-13; el 4B@temp0.2 era el que anclaba). Se conservan: el DISEÑO
del prompt (H2/H9, puntos 1-5) y el test del fail-open R1 documentado.

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
    '{"quality": 6.5, "reason": "mixed signals, ADX 18 flat", "bias": "NEUTRAL", '
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
        """T-31 (2026-10-19): el ancla de calidad venía de los EJEMPLOS.
        Ya no hay ejemplos con literales → este test fija que NO se
        reintroduzcan (ver `test_no_copyable_literal_examples`)."""
        prompt = await _unified_prompt()
        qualities = re.findall(r'"quality":\s*([0-9]+(?:\.[0-9]+)?)', prompt)
        assert not qualities, (
            f"el prompt volvió a traer qualities literales: {qualities}"
        )

    @pytest.mark.asyncio
    async def test_two_distinct_example_biases(self):
        """T-31: igual que el anterior pero para el sesgo (el monocultivo
        BULLISH venía del ejemplo #1 BULLISH)."""
        prompt = await _unified_prompt()
        biases = re.findall(r'"bias":\s*"(BULLISH|BEARISH|NEUTRAL)"', prompt)
        assert not biases, (
            f"el prompt volvió a traer sesgos literales: {biases}"
        )

    @pytest.mark.asyncio
    async def test_explicit_do_not_copy_instruction(self):
        prompt = await _unified_prompt()
        assert re.search(r"(?i)do not copy", prompt), (
            "falta la instrucción explícita de no copiar los valores de ejemplo"
        )

    @pytest.mark.asyncio
    async def test_quality_rubric_present(self):
        """T-31: la rúbrica se reescribió en prosa (bandas alta/media/baja)
        para que no contenga eslóganes copiables. Se fija el contenido
        nuevo, no el texto antiguo."""
        prompt = await _unified_prompt()
        assert "quality 0-10" in prompt, "falta la escala de quality"
        assert "high band" in prompt and "low band" in prompt, (
            "faltan las bandas de la rúbrica"
        )

    @pytest.mark.asyncio
    async def test_single_json_object_instruction(self):
        """Con dos ejemplos la respuesta debe seguir siendo UN objeto JSON."""
        prompt = await _unified_prompt()
        assert re.search(r"(?i)one json object", prompt), (
            "falta 'ONE JSON object' (riesgo de salida doble → parse degrada)"
        )

    @pytest.mark.asyncio
    async def test_no_copyable_literal_examples(self):
        """T-31 (2026-10-19) — RCA MEDIDA: el ejemplo #1 era BULLISH con
        razón positiva («trend and ADX align») y el 8B lo copiaba literal en
        289/319 ciclos (90,6 %) — de los cuales 274 BULLISH (94,8 %). El
        monocultivo BULLISH era la PLANTILLA, no un juicio.

        2ª medición: con ejemplos sin «premio», el modelo copió el ejemplo
        #3 íntegro (quality 7.0, bias_prob 0.68, conf_mod 1.2). A este
        modelo (8B Q2_K, temp 0.2) un ejemplo con LITERALES es una
        plantilla, sea cual sea su contenido.

        ⇒ REGRESIÓN QUE ESTE TEST BLINDA: el prompt NO puede contener
        ejemplos con valores literales (ni sesgo, ni quality, ni reason
        reutilizable). Sólo la FORMA con marcadores de posición."""
        prompt = await _unified_prompt()
        # Literales = valores REALES (número o sesgo cerrado). Los
        # marcadores (<number 0-10>, "<BULLISH|BEARISH|NEUTRAL>") no son
        # literales copiables: no hay nada que reproducir.
        literal_quality = re.findall(r'"quality":\s*[0-9]', prompt)
        literal_bias = re.findall(r'"bias":\s*"(?:BULLISH|BEARISH|NEUTRAL)"', prompt)
        literal_prob = re.findall(r'"bias_prob":\s*[0-9]', prompt)
        assert not (literal_quality or literal_bias or literal_prob), (
            f"el prompt tiene ejemplos con valores copiables: "
            f"q={literal_quality} bias={literal_bias} prob={literal_prob}"
        )
        for banned in ("trend and adx align", "trend, adx and session align",
                       "clean continuation", "range chop against signal",
                       "mixed trend and momentum signals", "no clear edge here",
                       "chop against the signal"):
            assert banned not in prompt.lower(), (
                f"razón de ejemplo reutilizable presente: '{banned}'"
            )

    @pytest.mark.asyncio
    async def test_schema_placeholders_present(self):
        """Sin ejemplos, la FORMA debe quedar explícita: las 7 claves con sus
        rangos. Sin esto el modelo puede devolver un JSON sin `bias_prob`."""
        prompt = await _unified_prompt()
        for key in ("quality", "reason", "bias", "bias_prob",
                    "confidence_modifier", "reentry_ok", "regime"):
            assert f'"{key}"' in prompt, f"falta la clave {key} en la forma del prompt"
        assert "0.00-1.00" in prompt or "0-1" in prompt, "falta el rango de bias_prob"

    @pytest.mark.asyncio
    async def test_evidence_first_instruction_present(self):
        """T-31: la razón debe citar un NÚMERO del contexto (evidencia primero).
        Sin esto el modelo vuelve a la plantilla genérica."""
        prompt = await _unified_prompt()
        assert re.search(r"(?i)evidence first", prompt), "falta la instrucción EVIDENCE FIRST"
        assert re.search(r"(?i)number", prompt), "la instrucción debe exigir citar un número"

    @pytest.mark.asyncio
    async def test_no_slogan_in_rubric(self):
        """La rúbrica vieja («trend/ADX/session agree») era EL eslogan que el
        modelo copiaba verbatim — no puede seguir en el prompt."""
        prompt = (await _unified_prompt()).lower()
        assert "trend/adx/session agree" not in prompt, (
            "la rúbrica conserva el eslogan copiable"
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
# (copia del ejemplo FORMAT #1) — comportamiento del Qwen3-4B@temp 0.2.
# S2 (orden Félix 2026-10-14): la guarda runtime anti-ancla se RETIRÓ
# (con el Llama-3.1-8B, 0 disparos desde 20:47 UTC del 10-13) y con ella
# las fixtures `_ANCHOR_COPY` / `_ANCHOR_COPY_WITH_FIELD` y su test de
# sincronía con la constante FORMAT_EXAMPLE_REASONS (también retirada).
# El diseño del prompt (3 ejemplos + advertencia) se conserva abajo.
# ---------------------------------------------------------------------------


async def _unified_with(canned: str, **overrides) -> tuple:
    client = _capturing_client(canned)
    with patch("httpx.AsyncClient", client):
        result = await query_qwen_unified(**{**_UNIFIED_ARGS, **overrides})
    return result, client.captured["prompt"]


class TestPromptAntiAnchorDesign:
    """Diseño anti-ancla del prompt (H2/H9 + RCA 2026-10-13) — SE CONSERVA.

    S2 (orden Félix 2026-10-14): la guarda runtime anti-ancla (c) se retiró
    (0 disparos con el 8B); estos tests protegen el DISEÑO del prompt y el
    fail-open R1 documentado, ambos intactos."""

    @pytest.mark.asyncio
    async def test_genuine_reason_without_reentry_field_not_degraded(self):
        """Razón propia sin reentry_ok → el fail-open R1 documentado sigue vivo.

        Preservado por decisión aparte (S2 no toca R1): la ausencia del campo
        emite WARNING y asume true — veto sólo por juicio explícito."""
        result, _ = await _unified_with(_UNIFIED_CANNED)
        assert result["ok"] is True
        assert result["quality"] == 6.5

    @pytest.mark.asyncio
    async def test_three_format_examples_present(self):
        """T-31 (2026-10-19): ya NO hay ejemplos FORMAT con literales — el
        prompt es la FORMA con marcadores de posición. La forma debe
        aparecer UNA vez y traer las 7 claves (nada que copiar)."""
        _, prompt = await _unified_with(_UNIFIED_CANNED)
        assert prompt.count('"reentry_ok":') == 1, (
            "el prompt debe traer la forma UNA vez (sin ejemplos repetidos)"
        )
        for key in ("quality", "reason", "bias", "bias_prob",
                    "confidence_modifier", "reentry_ok", "regime"):
            assert f'"{key}"' in prompt, f"falta {key} en la forma"

    @pytest.mark.asyncio
    async def test_post_example_copy_warning(self):
        """T-31: sin ejemplos, la advertencia pasa a "no hay ejemplos que
        imitar" + "EVIDENCE FIRST" (la instrucción que SÍ manda)."""
        _, prompt = await _unified_with(_UNIFIED_CANNED)
        assert re.search(r"(?i)no examples to imitate", prompt), (
            "falta la instrucción de que no hay ejemplos (el modelo debe "
            "inventar sus valores, no copiar)"
        )
        assert re.search(r"(?i)evidence first", prompt), (
            "falta EVIDENCE FIRST (la razón debe citar un número)"
        )

    @pytest.mark.asyncio
    async def test_out_of_window_reentry_section_present(self):
        """La variante fina (reentry=None) mantiene su sección Reentry check."""
        _, prompt = await _unified_with(_UNIFIED_CANNED)
        assert "Reentry check" in prompt, (
            "variante out-of-window sin sección Reentry check (regresión RCA)"
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
    '{"quality": 7.0, "reason": "trend and ADX align, SMA200 -1.7%", "bias": "BULLISH", '
    '"confidence_modifier": 1.1, "reentry_ok": true, "regime": "TRENDING"}'
)

_PARROT_B = (
    '{"quality": 6.0, "reason": "choppy range, ATR 0.0012 flat", '
    '"bias": "BEARISH", "confidence_modifier": 0.9, "reentry_ok": true, '
    '"regime": "RANGING"}'
)


class TestParrotGuardRCA:
    """Guarda anti-parrota (opción C/C+, orden 2026-10-13; S1 2026-10-14)."""

    # S3 (orden Félix 2026-10-14): variantes CONSTRUIDAS que replican el
    # patrón del parrota del 4B (rotación de la plantilla con jitter de
    # palabras: Jaccard ≥0.5 entre todas las parejas, quality byte a byte
    # fija). Sustituyen a las 3 citas reales del 4B (17:10-17:40), que
    # quedaban sobreajustadas a un modelo retirado.
    _ROT_V1 = (
        '{"quality": 8.0, "reason": "Trend and ADX align with the ML signal 1.16", '
        '"bias": "BULLISH", "confidence_modifier": 1.3, "reentry_ok": true, '
        '"regime": "TRENDING"}'
    )
    _ROT_V2 = (
        '{"quality": 8.0, "reason": "Trend and ADX align with ML signal context 1.16", '
        '"bias": "BULLISH", "confidence_modifier": 1.3, "reentry_ok": true, '
        '"regime": "TRENDING"}'
    )
    _ROT_V3 = (
        '{"quality": 8.0, "reason": "Trend and ADX and session align with ML 1.16", '
        '"bias": "BULLISH", "confidence_modifier": 1.3, "reentry_ok": true, '
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
        """3ª razón idéntica consecutiva → ABSTENCIÓN (T-31).

        Antes: degradada fail-closed (quality 0 → veto de calidad → HOLD,
        es decir parálisis total por una plantilla repetida). Ahora: el juez
        se aparta y deja decidir al PPO. Declarado, nunca silencioso."""
        await _unified_with(_PARROT_A)
        await _unified_with(_PARROT_A)
        r3, _ = await _unified_with(_PARROT_A)
        assert r3["ok"] is True, "la parrota no es un fallo técnico"
        assert r3.get("abstained") is True, "la parrota debe declararse abstención"
        assert r3["quality"] == 5.0, "abstención = calidad neutral (no bloquea)"

    @pytest.mark.asyncio
    async def test_parrot_degrade_persists(self):
        """La racha no se resetea al abstener: la 4ª también se abstiene."""
        await _unified_with(_PARROT_A)
        await _unified_with(_PARROT_A)
        r3, _ = await _unified_with(_PARROT_A)
        r4, _ = await _unified_with(_PARROT_A)
        assert r3.get("abstained") is True and r4.get("abstained") is True

    @pytest.mark.asyncio
    async def test_different_reason_resets_streak(self):
        """Razón disímil resetea la racha (A,A,B,A,A,A → abstiene en la 6ª)."""
        results = []
        for canned in (_PARROT_A, _PARROT_A, _PARROT_B,
                       _PARROT_A, _PARROT_A, _PARROT_A):
            r, _ = await _unified_with(canned)
            results.append(r.get("abstained", False))
        assert results == [False, False, False, False, False, True], results

    @pytest.mark.asyncio
    async def test_rotated_variants_constant_quality_degrade_at_third(self):
        """C+: variantes ROTADAS (Jaccard ≥ umbral entre todas las parejas,
        calidad constante = firma parrota) se abstienen en el 3er ciclo — el
        escape por rotación observado con el 4B queda cubierto sin depender
        de sus strings (S3: fixtures sintéticas auto-verificadas)."""
        variants = (self._ROT_V1, self._ROT_V2, self._ROT_V3)
        tokens = [
            qwen_client._reason_tokens(json.loads(v)["reason"]) for v in variants
        ]
        thr = qwen_client.PARROT_SIMILARITY_THRESHOLD
        assert qwen_client._jaccard(tokens[0], tokens[1]) >= thr
        assert qwen_client._jaccard(tokens[1], tokens[2]) >= thr
        assert qwen_client._jaccard(tokens[0], tokens[2]) >= thr

        r1, _ = await _unified_with(self._ROT_V1)
        r2, _ = await _unified_with(self._ROT_V2)
        r3, _ = await _unified_with(self._ROT_V3)
        assert r1["ok"] is True and r2["ok"] is True
        assert r3.get("abstained") is True, "la rotación de variantes escapó a la guarda C+"
        assert r3["quality"] == 5.0

    @pytest.mark.asyncio
    async def test_streak_is_per_symbol(self):
        """Rachas independientes por símbolo (preparado para G2)."""
        await _unified_with(_PARROT_A)                              # EURUSD ×1
        await _unified_with(_PARROT_A)                              # EURUSD ×2
        rg, _ = await _unified_with(_PARROT_A, symbol="GBPUSD")     # GBPUSD ×1
        re_, _ = await _unified_with(_PARROT_A)                     # EURUSD ×3 → abstiene
        assert rg.get("abstained") is False, "la racha de EURUSD contaminó GBPUSD"
        assert re_.get("abstained") is True

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
        # T-31: la razón vacía no aporta evidencia → abstención declarada
        # (antes: entraba como juicio con quality 6.0).
        assert all(r.get("abstained") is True for r in (r1, r2, r3))

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
        """S1 + T-31: el disparador sigue activo para la firma parrota real —
        razón similar Y quality constante ≥3 ciclos → ABSTENCIÓN.

        T-31 (2026-10-19): la parrota ya no degrada a fail-closed (quality 0
        → HOLD, o sea parálisis total); el juez se APARTA y deja decidir al
        PPO. Declarado en la respuesta (`abstained=True`), nunca silencioso.
        """
        # _PARROT_A = q 7.0 constante con la misma razón (firma parrota).
        await _unified_with(_PARROT_A)
        await _unified_with(_PARROT_A)
        r3, _ = await _unified_with(_PARROT_A)
        assert r3["ok"] is True, "la parrota ya no es un fallo técnico"
        assert r3.get("abstained") is True, "la parrota debe declararse abstención"
        assert r3["quality"] == 5.0, "abstención = calidad neutral (no bloquea)"
        assert r3["bias"] == "NEUTRAL", "abstención = sin voto direccional"


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

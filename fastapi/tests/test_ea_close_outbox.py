"""
Contrato EA (fuente MQL5): outbox de cierres — P0-4 EA-side.

Si el POST /api/v1/ai/trade/filled falla (servidor caído durante un cierre),
el cuerpo debe persistirse en disco y reintentarse en cada ciclo. El servidor
deduplica por deal_ticket, por lo que el reintento es seguro.
"""
import os
import re


def _ea_source() -> str:
    candidates = (
        "/mql5/AI_Quant_Terminal.mq5",  # Docker: volumen de solo lectura
        os.path.join(os.path.dirname(__file__), "..", "..", "mql5", "AI_Quant_Terminal.mq5"),
    )
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read()
    raise AssertionError(f"Fuente del EA no encontrada. Probado: {candidates}")


class TestCloseOutbox:
    def test_failure_branch_enqueues_to_outbox(self):
        src = _ea_source()
        # La rama else de "Error reportando trade cerrado" debe encolar el body
        assert "SaveToOutbox(body)" in src, "El cierre fallido no se persiste en outbox"
        error_idx = src.index("Error reportando trade cerrado")
        enqueue_idx = src.index("SaveToOutbox(body)")
        assert enqueue_idx > error_idx, "SaveToOutbox debe estar tras el log de error del POST"

    def test_outbox_flush_runs_every_cycle_before_predict(self):
        src = _ea_source()
        assert "FlushTradeOutbox();" in src, "Flush en OnTick ausente"
        flush_idx = src.index("FlushTradeOutbox();")  # solo aparece la llamada
        predict_idx = src.index('"/api/v1/ai/predict"')
        assert flush_idx < predict_idx, "El outbox debe reintentarse ANTES del predict del ciclo"

    def test_outbox_persists_to_disk_line_per_close(self):
        src = _ea_source()
        assert '"trade_filled_outbox.jsonl"' in src
        assert "FileOpen(OUTBOX_FILE" in src
        assert "FileWrite(h, body)" in src
        assert "FileReadString(h)" in src

    def test_outbox_bounded_per_flush(self):
        src = _ea_source()
        assert "OUTBOX_MAX_PER_FLUSH" in src, "Falta límite de reintentos por ciclo"
        assert "attempted < OUTBOX_MAX_PER_FLUSH" in src

    def test_permanent_link_conflicts_are_quarantined(self):
        src = _ea_source()
        assert re.search(
            r'#define\s+OUTBOX_REJECTED_FILE\s+"trade_filled_conflicts\.jsonl"',
            src,
        )
        assert "if(res == 409)" in src
        assert "SaveRejectedToOutbox(lines[i])" in src
        assert "attempted++;" in src

    def test_outbox_rewrite_preserves_original_until_replacement_is_ready(self):
        src = _ea_source()
        flush = src[src.index("void FlushTradeOutbox"):src.index("void OnTick")]
        assert "string temporaryFile = OUTBOX_FILE + \".tmp\";" in flush
        assert flush.index("FileWrite(hw, remaining[k])") < flush.index("FileClose(hw)")
        assert flush.index("FileClose(hw)") < flush.index(
            "FileMove(temporaryFile, 0, OUTBOX_FILE, FILE_REWRITE)"
        )
        assert "el outbox original se conserva" in flush

    def test_ea_version_bumped_to_14_3(self):
        src = _ea_source()
        # v13.5: Safe-4 (Félix 2026-10-15) — lock +SL_LockSafetyNetEur
        # (4€) antes de 1R; breakeven deja de moverse a la entrada tal cual.
        # v13.6: se elimina un ``` suelto (artefacto markdown) que rompía la
        # compilación MQL5 — A2 (cierre de ficheros pendientes, 2026-10-17).
        # v13.7: G2-2 — input PollOffsetSeconds (escalonado de sondeo por
        # instancia; EURUSD :00/:05 vs GBPUSD :02/:07).
        # v13.9: rename genérico AI_Quant_Terminal.mq5 (el mismo EA sirve
        # todos los pares — Symbol() en todo; orden Félix 2026-10-18).
        # v14.0: guard de CLOSE 0,03 → 0,01 + diagnóstico de magic
        # (posición USDCHF 58986641010 con magic 90002, 2026-10-21).
        # v14.2: el order_db_id del gate queda en el comentario de apertura y
        # se propaga al cierre para enlazar el outcome con la orden exacta.
        # v14.3: conflictos permanentes HTTP 409 se apartan del outbox activo.
        assert re.search(r'#property\s+version\s+"14\.3"', src), "Falta bump a 14.3"
        assert re.search(r'#define\s+EA_VERSION\s+"14\.3"', src)

    def test_ea_filters_opening_deals_v13_2(self):
        # v13.2: OnTradeTransaction descarta DEAL_ENTRY_IN — el deal de
        # apertura (pnl 0, entry==exit) no es un cierre y duplicaba cada
        # posición en trade_outcomes. El backend tiene el guard equivalente.
        src = _ea_source()
        assert "if(currentEntry == DEAL_ENTRY_IN)" in src
        assert "long currentEntry = -1;" in src

    def test_order_id_is_carried_from_gate_to_close_webhook(self):
        src = _ea_source()
        assert 'GetJsonValue(r, "order_db_id")' in src
        assert 'StringFormat("AIQ:%I64d", orderDbId)' in src
        assert 'HistoryDealGetString(openingDeal, DEAL_COMMENT)' in src
        assert '\\"order_db_id\\"' in src


class TestPredictLatencyBudget:
    def test_predict_webrequest_timeout_covers_llama8b(self):
        """Orden Félix 2026-10-13: Llama-3.1-8B tarda ~36-39s por llamada —
        el WebRequest del predict debe esperar 60s (backend: 45s). Con 30s
        el EA cortaba antes que el backend y el ciclo quedaba 'unavailable'."""
        src = _ea_source()
        line = next(
            l for l in src.splitlines() if '"/api/v1/ai/predict"' in l
        )
        assert ", 60000," in line, (
            f"timeout del WebRequest de predict debe ser 60000ms: {line.strip()}"
        )

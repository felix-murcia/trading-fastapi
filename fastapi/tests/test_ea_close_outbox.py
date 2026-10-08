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
        "/mql5/AI_Quant_Terminal_v3.mq5",  # Docker: volumen de solo lectura
        os.path.join(os.path.dirname(__file__), "..", "..", "mql5", "AI_Quant_Terminal_v3.mq5"),
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

    def test_ea_version_bumped_to_13_3(self):
        src = _ea_source()
        # v13.3: P1 intraposición — ManageBreakeven (SL→entrada al +1R)
        assert re.search(r'#property\s+version\s+"13\.3"', src), "Falta bump a 13.3"
        assert re.search(r'#define\s+EA_VERSION\s+"13\.3"', src)

    def test_ea_filters_opening_deals_v13_2(self):
        # v13.2: OnTradeTransaction descarta DEAL_ENTRY_IN — el deal de
        # apertura (pnl 0, entry==exit) no es un cierre y duplicaba cada
        # posición en trade_outcomes. El backend tiene el guard equivalente.
        src = _ea_source()
        assert "if(currentEntry == DEAL_ENTRY_IN)" in src
        assert "long currentEntry = -1;" in src

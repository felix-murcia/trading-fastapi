#!/usr/bin/env python3
"""Identidad del prompt LLM — P0.3 §10.

El prompt de Qwen no es un fichero de texto: se construye en
`services/qwen_client.py`. Por tanto su identidad se sella con tres hashes
reproducibles (cualquier cambio en uno invalida el sello del pre-registro):

  1. `source_sha256`          — fichero completo `services/qwen_client.py`
                                (bytes exactos en disco).
  2. `user_template_sha256`   — bloque de construcción del prompt de usuario,
                                desde `    context_lines = [` hasta
                                `    prompt = "\\n".join(context_lines)`
                                (inclusive), normalizado a LF.
  3. `system_prompt_sha256`   — literal exacto del mensaje `system`.

Además extrae del código los parámetros de inferencia reales y falla si no
puede determinarlos (sin defaults inventados).

Uso:  python3 scripts/prompt_identity_p03.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "services" / "qwen_client.py"

SYSTEM_LITERAL = "You are a quantitative trading analyst. Always respond in valid JSON."
START_MARKER = "    context_lines = [\n"
END_MARKER = '    prompt = "\\n".join(context_lines)\n'


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _one(pattern: str, text: str, label: str) -> str:
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        raise RuntimeError(f"{label}: se esperaba 1 coincidencia, encontradas {len(matches)}")
    return matches[0]


def main() -> int:
    raw = SRC.read_bytes()
    text = raw.decode("utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    system_marker = f'"content": "{SYSTEM_LITERAL}"'
    if normalized.count(system_marker) != 1:
        raise RuntimeError("mensaje de sistema no localizado de forma única")

    start = normalized.find(START_MARKER)
    end = normalized.find(END_MARKER, start)
    if start < 0 or end < 0:
        raise RuntimeError("bloque del prompt de usuario no localizado")
    end += len(END_MARKER)
    user_template = normalized[start:end]

    temperature_const = _one(
        r"QWEN_TEMPERATURE_UNIFIED = ([0-9.]+)", normalized, "temperatura"
    )
    timeout_const = _one(r"QWEN_TIMEOUT_UNIFIED = ([0-9.]+)", normalized, "timeout")

    # El fichero tiene DOS llamadas al LLM (unified y validador SL/TP). El
    # protocolo H=20 consume sólo la unified: los parámetros se extraen de su
    # bloque, nunca del fichero entero (el validador usa 150 tokens).
    unified_start = normalized.find("async def query_qwen_unified(")
    unified_end = normalized.find("async def query_qwen_sltp_validator(")
    if unified_start < 0 or unified_end < 0 or unified_end <= unified_start:
        raise RuntimeError("no se pudo aislar el bloque de query_qwen_unified")
    unified = normalized[unified_start:unified_end]

    max_tokens = _one(r'"max_tokens": ([0-9]+)', unified, "max_tokens (unified)")
    response_format = _one(
        r'"response_format": (\{"type": "[a-z_]+"\})', unified, "response_format (unified)"
    )
    url_default = _one(r'QWEN_URL = os\.getenv\("QWEN_URL", "([^"]+)"\)', normalized, "QWEN_URL")

    # El prompt de usuario se envía con `temperature=QWEN_TEMPERATURE_UNIFIED`.
    if '"temperature": QWEN_TEMPERATURE_UNIFIED' not in unified:
        raise RuntimeError("el payload no usa QWEN_TEMPERATURE_UNIFIED (temperatura no verificable)")

    # Frontera de ventana de contexto del servidor (llm-8b.service): 2048 tokens.
    n_ctx = 2048
    prompt_tokens_upper = None

    result = {
        "prompt_version": "0.1",
        "source_file": str(SRC.relative_to(SRC.parents[1])),
        "source_sha256": _sha256(raw),
        "system_prompt": SYSTEM_LITERAL,
        "system_prompt_sha256": _sha256(SYSTEM_LITERAL.encode("utf-8")),
        "user_template_sha256": _sha256(user_template.encode("utf-8")),
        "user_template_lines": user_template.count("\n"),
        "inference_params": {
            "temperature": float(temperature_const),
            "max_tokens": int(max_tokens),
            "response_format": json.loads(response_format),
            "timeout_s": float(timeout_const),
            "endpoint_default": url_default,
        },
        "server_context_window_tokens": n_ctx,
    }
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

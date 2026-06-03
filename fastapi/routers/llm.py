import json as _json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.llm_client import call_llm
from .deps import verify_token

logger = logging.getLogger(__name__)


class LLMSignalRequest(BaseModel):
    agent: str
    prompt: str


class LLMSignalResponse(BaseModel):
    agent: str
    signal: str
    confidence: float


router = APIRouter(prefix="/v1/llm", tags=["llm"])


@router.post("/signal", response_model=LLMSignalResponse)
async def get_signal(
    req: LLMSignalRequest,
    _: None = Depends(verify_token),
) -> LLMSignalResponse:
    # LLM call — error de red, timeout, 5xx del proveedor → 502
    try:
        raw = await call_llm(req.prompt)
    except Exception as exc:
        logger.error("LLM call failed for agent=%s: %s", req.agent, exc)
        raise HTTPException(
            status_code=502,
            detail=f"llm_call_failed:{req.agent}:{exc}",
        )

    # Parse JSON — modelo devolvió basura → 422
    m = re.search(r"\{[\s\S]*?\}", raw)
    if not m:
        logger.error("LLM parse failed for agent=%s — raw: %r", req.agent, raw[:200])
        raise HTTPException(
            status_code=422,
            detail=f"llm_parse_failed:{req.agent}:no_json_block",
        )

    try:
        p = _json.loads(m.group(0))
    except _json.JSONDecodeError as exc:
        logger.error("LLM JSON decode failed for agent=%s — raw: %r", req.agent, raw[:200])
        raise HTTPException(
            status_code=422,
            detail=f"llm_parse_failed:{req.agent}:{exc}",
        )

    signal = p.get("signal", "")
    if signal not in ("buy", "sell", "neutral"):
        logger.error("LLM invalid signal=%r for agent=%s", signal, req.agent)
        raise HTTPException(
            status_code=422,
            detail=f"llm_invalid_signal:{req.agent}:{signal!r}",
        )

    confidence = max(0.0, min(1.0, float(p.get("confidence", 0))))
    return LLMSignalResponse(agent=req.agent, signal=signal, confidence=confidence)

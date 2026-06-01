import json as _json
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.llm_client import call_llm
from .deps import verify_token


class LLMSignalRequest(BaseModel):
    agent: str
    prompt: str


class LLMSignalResponse(BaseModel):
    agent: str
    signal: str
    confidence: float
    parse_error: bool


router = APIRouter(prefix="/v1/llm", tags=["llm"])


@router.post("/signal", response_model=LLMSignalResponse)
async def get_signal(
    req: LLMSignalRequest,
    _: None = Depends(verify_token),
) -> LLMSignalResponse:
    try:
        raw = await call_llm(req.prompt)
    except Exception:
        return LLMSignalResponse(agent=req.agent, signal="neutral", confidence=0.0, parse_error=True)

    try:
        m = re.search(r"\{[\s\S]*?\}", raw)
        if not m:
            raise ValueError("no json block")
        p = _json.loads(m.group(0))
        signal = p.get("signal", "neutral")
        if signal not in ("buy", "sell", "neutral"):
            signal = "neutral"
        confidence = max(0.0, min(1.0, float(p.get("confidence", 0))))
        return LLMSignalResponse(agent=req.agent, signal=signal, confidence=confidence, parse_error=False)
    except Exception:
        return LLMSignalResponse(agent=req.agent, signal="neutral", confidence=0.0, parse_error=True)
